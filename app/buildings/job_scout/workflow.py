"""
Job scout workflow — the end-to-end pipeline for one nightly run.

Pipeline stages:
    1. Start a ScoutRun row to track this execution
    2. Run all enabled scrapers (in parallel) -> raw Job objects
    3. Upsert into DB, dedup by (source, source_job_id)
    4. Score everything that needs scoring (sequential, local model)
    5. Mark the ScoutRun finished with final counts

The workflow is the *only* code that knows about all of these stages
together. Scrapers don't know about scoring; the scorer doesn't know
about scrapers; storage doesn't know about either. Workflow = orchestration.
"""

import asyncio
import logging
from datetime import datetime

from sqlmodel import col, select

from app.buildings.job_scout.agents import score_unscored_jobs
from app.buildings.job_scout.scrapers.registry import all_enabled_scrapers
from app.profile import PROFILE
from app.spine.storage import (
    Job,
    ScoutRun,
    get_session,
    upsert_jobs,
)

logger = logging.getLogger(__name__)


async def run_scout(
    *,
    max_jobs_to_score: int | None = None,
) -> ScoutRun:
    """
    Execute one full scout run. Returns the ScoutRun row with final counts.

    Args:
        max_jobs_to_score: Cap on scoring this run (passed through to
            score_unscored_jobs). None = score everything that needs it.
            Useful for dev/testing; production should leave it None.
    """
    run = _start_run()
    # _start_run refreshes the row, so id is always set by this point.
    # Asserting once narrows run.id to int for every use below.
    assert run.id is not None, "_start_run must return a run with an id"
    logger.info(f"=== Scout run #{run.id} started at {run.started_at} ===")

    try:
        # === Stage 1: scrape ===
        all_jobs = await _scrape_all_sources()
        logger.info(f"Scraped {len(all_jobs)} total jobs across all sources")

        # === Stage 2: upsert (dedup) ===
        inserted, skipped = upsert_jobs(all_jobs)
        logger.info(f"Storage: inserted {inserted}, skipped {skipped} (already known)")

        # === Stage 3: score ===
        scored, failed = await score_unscored_jobs(max_jobs=max_jobs_to_score)
        logger.info(f"Scoring: {scored} scored, {failed} failed")

        # === Stage 4: count surfaced jobs ===
        # "Surfaced" = jobs whose latest evaluation meets the profile threshold
        surfaced = _count_surfaced_jobs()
        logger.info(f"Surfaced (>= {PROFILE.minimum_score_to_surface}): {surfaced}")

        # === Finalize the run row ===
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

    # Re-fetch the finalized run to return it with all counts populated
    return _fetch_run(run.id)


# ============================================================================
# Stages
# ============================================================================


async def _scrape_all_sources() -> list[Job]:
    """Run every enabled scraper in parallel, gather all returned jobs."""
    scrapers = all_enabled_scrapers(PROFILE.enabled_sources)
    if not scrapers:
        logger.warning("No scrapers enabled in PROFILE.enabled_sources")
        return []

    logger.info(f"Running {len(scrapers)} scrapers: {[s.source_name for s in scrapers]}")

    # Parallel because scrapers are I/O bound and independent
    results = await asyncio.gather(
        *(scraper.fetch() for scraper in scrapers),
        return_exceptions=True,  # one scraper failing shouldn't kill the rest
    )

    all_jobs: list[Job] = []
    for scraper, result in zip(scrapers, results):
        # gather(return_exceptions=True) yields BaseException, not Exception,
        # so we must check the broader type for the type narrowing to stick.
        if isinstance(result, BaseException):
            logger.error(f"Scraper {scraper.source_name} failed: {result}")
            continue
        all_jobs.extend(result)

    return all_jobs


# ============================================================================
# Run row helpers
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
    with get_session() as session:
        run = session.get(ScoutRun, run_id)
        if run is None:
            return
        run.error = error[:1000]  # truncate huge tracebacks
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
        # Get latest evaluation per job, count how many are >= threshold
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