"""
Job scout workflow — the end-to-end pipeline for one nightly run.

Pipeline stages:
    1. Start a ScoutRun row to track this execution
    2. Pull every QUARANTINE/ACTIVE career source from the registry
    3. Dispatch each through the scraper handler that fits its source_type
       and scraper_hint, with bounded concurrency and per-domain politeness
    4. Log a SourceObservation row per poll attempt (event type + duration)
    5. Upsert returned jobs into the DB, dedup by (source.domain, source_job_id)
    6. Score everything that needs scoring (sequential, local model)
    7. Mark the ScoutRun finished with final counts

The workflow is the *only* code that knows about all of these stages
together. Scrapers don't know about scoring; the scorer doesn't know
about scrapers; storage doesn't know about either. Workflow = orchestration.

Concurrency model: one PoliteFetcher across the whole run (so robots cache
and per-domain rate limits are shared), one asyncio.Semaphore bounding the
number of in-flight scrape calls, and asyncio.gather to fan out. Most
sources are RSS feeds returning in ~1-3 seconds, so the bottleneck is the
politeness floor (5s default between same-domain requests) rather than CPU.
"""

import asyncio
import logging
from typing import Optional

from sqlmodel import col, select

from app.buildings.job_scout.agents import score_unscored_jobs
from app.buildings.job_scout.scrapers.dispatcher import (
    DispatchDecision,
    dispatch,
)
from app.buildings.job_scout.scrapers.outcome import ScrapeOutcome
from app.buildings.sourcing.http_client import PoliteFetcher
from app.buildings.sourcing.models import (
    ObservationEvent,
    Source,
)
from app.buildings.sourcing.registry import (
    get_pollable_career_sources,
    increment_empty_polls,
    log_observation,
    mark_source_alive,
)
from app.profile import PROFILE
from app.spine.storage import (
    Job,
    ScoutRun,
    get_session,
    upsert_jobs,
)

logger = logging.getLogger(__name__)


# How many sources to scrape concurrently. Per-domain rate limits are still
# enforced by PoliteFetcher; this just caps total in-flight HTTP work so we
# don't open hundreds of sockets at once.
SCRAPE_CONCURRENCY = 8

# Default cap on sources per run. Overrideable via run_scout(max_sources=...).
# None = scrape every pollable career source.
DEFAULT_MAX_SOURCES = None


async def run_scout(
    *,
    max_jobs_to_score: Optional[int] = None,
    max_sources: Optional[int] = DEFAULT_MAX_SOURCES,
) -> ScoutRun:
    """
    Execute one full scout run. Returns the ScoutRun row with final counts.

    Args:
        max_jobs_to_score: Cap on scoring this run. None = score everything
            that needs scoring. Useful for first runs where the scoring
            queue is huge; the next run picks up where this one left off.
        max_sources: Cap on how many registry sources to poll. None = poll
            every pollable career source. Useful for testing.
    """
    run = _start_run()
    assert run.id is not None, "_start_run must return a run with an id"
    logger.info(f"=== Scout run #{run.id} started at {run.started_at} ===")

    try:
        # === Stage 1: scrape ===
        all_jobs = await _scrape_registry(max_sources=max_sources)
        logger.info(f"Scraped {len(all_jobs)} total jobs across registry sources")

        # === Stage 2: upsert (dedup) ===
        inserted, skipped = upsert_jobs(all_jobs)
        logger.info(f"Storage: inserted {inserted}, skipped {skipped} (already known)")

        # === Stage 3: score ===
        scored, failed = await score_unscored_jobs(max_jobs=max_jobs_to_score)
        logger.info(f"Scoring: {scored} scored, {failed} failed")

        # === Stage 4: count surfaced jobs ===
        surfaced = _count_surfaced_jobs()
        logger.info(f"Surfaced (>= {PROFILE.minimum_score_to_surface}): {surfaced}")

        # === Finalize ===
        _finalize_run(
            run_id=run.id,
            jobs_found=len(all_jobs),
            jobs_new=inserted,
            jobs_evaluated=scored,
            jobs_surfaced=surfaced,
        )

    except Exception as e:
        logger.exception(f"Scout run #{run.id} failed")
        _mark_run_failed(run_id=run.id, error=str(e))
        raise

    return _fetch_run(run.id)


# ============================================================================
# Stage 1 — registry-driven scraping
# ============================================================================


async def _scrape_registry(*, max_sources: Optional[int]) -> list[Job]:
    """
    Pull every pollable career source, dispatch each to its handler,
    collect all returned Jobs.

    - Sources without a handler (hostile aggregators, no RSS/JSON/ATS)
      are logged and skipped without an HTTP call.
    - Sources with a handler are polled with bounded concurrency.
    - Every poll attempt logs a SourceObservation for pattern_tracker.
    - Per-domain rate limits live in PoliteFetcher and persist across
      the whole run.
    """
    sources = get_pollable_career_sources(limit=max_sources)
    if not sources:
        logger.warning(
            "No pollable career sources in the registry. Run BoardHunter "
            "first: python -m app.buildings.sourcing run board_hunter"
        )
        return []

    handler_buckets, skip_counts = _bucket_by_dispatch(sources)
    logger.info(
        f"Dispatch: {sum(len(v) for v in handler_buckets.values())} sources "
        f"have a handler, {skip_counts['hostile']} hostile-skipped, "
        f"{skip_counts['no_handler']} no-handler-skipped "
        f"(of {len(sources)} total)"
    )

    semaphore = asyncio.Semaphore(SCRAPE_CONCURRENCY)
    all_jobs: list[Job] = []

    async with PoliteFetcher() as fetcher:
        coros = []
        for source, handler in _iter_dispatched(handler_buckets):
            coros.append(_run_one_with_semaphore(source, handler, fetcher, semaphore))

        # gather collects results in order; each call also logs its own
        # observation, so a failure in one source can't lose data for another.
        results = await asyncio.gather(*coros, return_exceptions=True)

    handler_stats: dict[str, int] = {}
    for result in results:
        if isinstance(result, BaseException):
            logger.error(f"Scrape coroutine raised: {result}")
            continue
        outcome: ScrapeOutcome = result
        all_jobs.extend(outcome.jobs)
        handler_stats[outcome.handler_name] = (
            handler_stats.get(outcome.handler_name, 0) + len(outcome.jobs)
        )

    if handler_stats:
        breakdown = ", ".join(
            f"{name}={count}" for name, count in sorted(handler_stats.items())
        )
        logger.info(f"Scrape breakdown by handler: {breakdown}")

    return all_jobs


def _bucket_by_dispatch(
    sources: list[Source],
) -> tuple[dict[str, list[tuple[Source, object]]], dict[str, int]]:
    """
    Split the source list into (sources-to-poll grouped by handler) and
    (skip counts). Sources with no handler are silently dropped — we don't
    want to waste a SourceObservation row on something we never polled.
    """
    buckets: dict[str, list[tuple[Source, object]]] = {}
    skip_counts = {"hostile": 0, "no_handler": 0}

    for source in sources:
        handler, decision = dispatch(source)
        if decision == DispatchDecision.HANDLER and handler is not None:
            buckets.setdefault(handler.__name__, []).append((source, handler))
        elif decision == DispatchDecision.SKIP_HOSTILE:
            skip_counts["hostile"] += 1
        else:
            skip_counts["no_handler"] += 1

    return buckets, skip_counts


def _iter_dispatched(
    buckets: dict[str, list[tuple[Source, object]]],
):
    """Flatten the per-handler buckets back into (source, handler) pairs."""
    for items in buckets.values():
        for source, handler in items:
            yield source, handler


async def _run_one_with_semaphore(
    source: Source,
    handler,
    fetcher: PoliteFetcher,
    semaphore: asyncio.Semaphore,
) -> ScrapeOutcome:
    """
    Run a single source's handler under the concurrency cap, then translate
    its outcome into the registry's observation log + alive/empty counters.

    Failures here become SOURCE_BLOCKED-style observations rather than
    propagating exceptions — one bad source must not kill the whole run.
    """
    async with semaphore:
        try:
            outcome = await handler(source, fetcher)
        except Exception as e:
            logger.warning(f"Handler crashed on {source.url}: {e}")
            outcome = ScrapeOutcome.fetch_failed(
                f"handler exception: {e}",
                handler_name=getattr(handler, "__name__", "unknown"),
                duration_ms=0,
            )

    if source.id is not None:
        try:
            log_observation(
                source.id,
                outcome.event,
                listings_count=len(outcome.jobs),
                error_message=outcome.error_message,
                duration_ms=outcome.duration_ms,
            )
            if outcome.event == ObservationEvent.LISTINGS_FOUND:
                mark_source_alive(source.id)
            elif outcome.event == ObservationEvent.NO_LISTINGS:
                increment_empty_polls(source.id)
        except Exception as e:
            # Observation logging must never break a scrape.
            logger.warning(f"Failed to log observation for {source.url}: {e}")

    if outcome.error_message:
        logger.info(
            f"  [{outcome.handler_name}] {source.domain}: "
            f"{outcome.event.value} ({outcome.error_message})"
        )
    else:
        logger.info(
            f"  [{outcome.handler_name}] {source.domain}: "
            f"{len(outcome.jobs)} jobs ({outcome.duration_ms}ms)"
        )

    return outcome


# ============================================================================
# Run row helpers (unchanged)
# ============================================================================


def _start_run() -> ScoutRun:
    """Create the ScoutRun row at the start of a run, return it with id set."""
    with get_session() as session:
        run = ScoutRun()
        session.add(run)
        session.commit()
        session.refresh(run)
        session.expunge(run)
    return run


def _finalize_run(
    *,
    run_id: int,
    jobs_found: int,
    jobs_new: int,
    jobs_evaluated: int,
    jobs_surfaced: int,
) -> None:
    """Update the ScoutRun row with final counts and finished_at."""
    from datetime import datetime
    with get_session() as session:
        run = session.get(ScoutRun, run_id)
        if run is None:
            logger.error(f"Cannot finalize run #{run_id}: not found")
            return
        run.jobs_found = jobs_found
        run.jobs_new = jobs_new
        run.jobs_evaluated = jobs_evaluated
        run.jobs_surfaced = jobs_surfaced
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()


def _mark_run_failed(*, run_id: int, error: str) -> None:
    """Record an error on the ScoutRun row when the pipeline blows up."""
    from datetime import datetime
    with get_session() as session:
        run = session.get(ScoutRun, run_id)
        if run is None:
            return
        run.error = error[:1000]
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()


def _fetch_run(run_id: int) -> ScoutRun:
    """Return the run row, detached from the session for safe use by caller."""
    with get_session() as session:
        run = session.get(ScoutRun, run_id)
        if run is None:
            raise RuntimeError(f"ScoutRun #{run_id} disappeared between writes")
        session.expunge(run)
    return run


def _count_surfaced_jobs() -> int:
    """
    Count jobs whose latest evaluation meets the surfacing threshold.
    This is what the dashboard's morning brief will show.
    """
    from app.spine.storage import Evaluation

    threshold = PROFILE.minimum_score_to_surface
    with get_session() as session:
        all_jobs = session.exec(select(Job)).all()
        count = 0
        for job in all_jobs:
            latest = session.exec(
                select(Evaluation)
                .where(Evaluation.job_id == job.id)
                .order_by(col(Evaluation.evaluated_at).desc())
                .limit(1)
            ).first()
            if latest is not None and latest.score >= threshold:
                count += 1
    return count
