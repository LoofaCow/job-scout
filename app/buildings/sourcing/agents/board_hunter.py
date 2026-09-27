"""
BoardHunter — runs every board strategy, verifies candidates, persists sources.

Two run modes:

    Single-pass (default):
        Run every strategy once, verify all new candidates (up to max_candidates),
        return summary, exit. This is what you want for dev cycles.

    Continuous (with time_limit_seconds):
        Loop until the deadline. Each cycle re-runs strategies (search_rotation
        samples different queries each cycle), dedups against the registry,
        verifies new candidates, then loops. Designed for overnight saturation
        runs where the goal is "discover as many sources as possible in N hours".

The hunter is not an LLM agent itself. It's an orchestrator. LLM work happens
inside the verifier helper, not here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

from app.buildings.sourcing.agents.helpers.verifier import verify_source_candidate
from app.buildings.sourcing.http_client import PoliteFetcher
from app.buildings.sourcing.models import SourcePipeline, SourceStatus, SourceType
from app.buildings.sourcing.registry import (
    filter_already_known_urls,
    update_source_status,
    upsert_source,
)
from app.buildings.sourcing.strategies.base import SourceCandidate
from app.buildings.sourcing.strategies.registry import get_board_strategies, get_strategy

logger = logging.getLogger(__name__)


DEFAULT_MAX_CANDIDATES = 100   # cap per run/cycle; pass -1 to disable
CONTINUOUS_CYCLE_DELAY = 5.0   # seconds between cycles in continuous mode


async def run_board_hunter(
    *,
    strategy_filter: Optional[str] = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    time_limit_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """
    Execute one BoardHunter run.

    Args:
        strategy_filter: If set, run only the strategy with this name.
        max_candidates: Cap LLM verifications per cycle. -1 disables cap.
            In continuous mode this is a *per-cycle* cap, not total.
        time_limit_seconds: If set, run continuously until this many seconds
            have elapsed. Each cycle re-runs strategies and verifies new
            findings. None = single-pass mode (existing behavior).

    Returns:
        A summary dict. In continuous mode, totals across all cycles.
    """
    strategies = _resolve_strategies(strategy_filter)
    if not strategies:
        logger.warning("BoardHunter: no strategies registered")
        return _empty_summary()

    if time_limit_seconds is None:
        return await _run_single_pass(strategies, max_candidates)

    return await _run_continuous(strategies, max_candidates, time_limit_seconds)


# ============================================================================
# Single-pass mode (the original behavior)
# ============================================================================


async def _run_single_pass(
    strategies: list,
    max_candidates: int,
) -> dict[str, Any]:
    """Run each strategy once, verify, persist, exit."""
    logger.info(
        f"BoardHunter starting (single-pass) — {len(strategies)} strategies, "
        f"max_candidates={max_candidates}"
    )

    async with PoliteFetcher() as fetcher:
        cycle = await _run_cycle(strategies, fetcher, max_candidates)

    summary = _cycle_to_summary(cycle, strategies, mode="single_pass")
    logger.info(f"BoardHunter complete: {summary}")
    return summary


# ============================================================================
# Continuous mode (overnight saturation)
# ============================================================================


async def _run_continuous(
    strategies: list,
    max_candidates: int,
    time_limit_seconds: float,
) -> dict[str, Any]:
    """
    Loop strategies → verify → repeat until time budget is spent.

    Each cycle is a complete strategy pass. Most candidates from cycle N+1
    are already-known and get filtered cheaply via the registry; only
    truly-new candidates pay the verification cost. search_rotation samples
    different queries each cycle, so over many cycles we cover the full
    query space.

    Stops when the deadline is reached. If a cycle is mid-verification
    when the deadline hits, we let the current candidate finish (so we
    don't corrupt the DB) and stop before the next.
    """
    deadline = time.monotonic() + time_limit_seconds
    logger.info(
        f"BoardHunter starting (continuous) — {len(strategies)} strategies, "
        f"max_candidates_per_cycle={max_candidates}, "
        f"time_limit={time_limit_seconds:.0f}s "
        f"(~{time_limit_seconds / 3600:.1f}h)"
    )

    totals = {
        "discovered": 0,
        "skipped_already_known": 0,
        "new_candidates": 0,
        "verified_attempted": 0,
        "verified_real": 0,
        "persisted_new": 0,
        "rejected": 0,
        "skipped_over_cap": 0,
    }
    cycles_completed = 0

    # One fetcher across the whole run — preserves robots cache and rate
    # limits across cycles, which matters for the awesome-list and
    # directory-crawl seeds we hit every cycle.
    async with PoliteFetcher() as fetcher:
        while time.monotonic() < deadline:
            cycle_num = cycles_completed + 1
            remaining = deadline - time.monotonic()
            logger.info(
                f"=== Cycle {cycle_num} starting "
                f"(remaining: {remaining:.0f}s / {remaining / 3600:.2f}h) ==="
            )

            cycle = await _run_cycle(
                strategies,
                fetcher,
                max_candidates,
                deadline=deadline,
            )

            cycles_completed += 1
            for k in totals:
                totals[k] += cycle.get(k, 0)

            logger.info(
                f"=== Cycle {cycle_num} complete: "
                f"discovered={cycle['discovered']}, "
                f"persisted_new={cycle['persisted_new']}, "
                f"running totals: persisted={totals['persisted_new']} ==="
            )

            # If the deadline passed during this cycle, stop now.
            if time.monotonic() >= deadline:
                logger.info("Deadline reached; ending continuous run.")
                break

            # Brief pause between cycles so we don't hammer seed pages back-to-back.
            await asyncio.sleep(CONTINUOUS_CYCLE_DELAY)

    elapsed = time_limit_seconds - max(0.0, deadline - time.monotonic())
    summary = {
        "strategies_run": [s.name for s in strategies],
        "mode": "continuous",
        "cycles_completed": cycles_completed,
        "elapsed_seconds": round(elapsed, 1),
        **totals,
    }
    logger.info(f"BoardHunter complete: {summary}")
    return summary


# ============================================================================
# A single cycle (one full strategies-then-verify pass)
# ============================================================================


async def _run_cycle(
    strategies: list,
    fetcher: PoliteFetcher,
    max_candidates: int,
    *,
    deadline: Optional[float] = None,
) -> dict[str, int]:
    """
    Run every strategy once, verify new candidates, persist. Returns counts.

    If deadline is given (continuous mode), we abort the verification loop
    when time.monotonic() >= deadline. Discovery itself is fast enough that
    we always let it finish.
    """
    candidates = await _discover_candidates(strategies, fetcher)
    discovered = len(candidates)
    logger.info(f"Discovery: {discovered} unique candidates")

    # === Cross-run dedup ===
    all_urls = [_normalize_url(c.url) for _, c in candidates]
    unknown_urls = filter_already_known_urls(all_urls)
    new_candidates = [
        (sn, c) for sn, c in candidates
        if _normalize_url(c.url) in unknown_urls
    ]
    skipped_known = discovered - len(new_candidates)
    if skipped_known:
        logger.info(
            f"Cross-run dedup: skipping {skipped_known} candidates "
            f"already in the registry"
        )

    if max_candidates < 0:
        candidates_to_verify = new_candidates
    else:
        candidates_to_verify = new_candidates[:max_candidates]
        if len(new_candidates) > max_candidates:
            logger.info(
                f"Capping verification at {max_candidates} "
                f"(found {len(new_candidates)} new)"
            )

    verified, persisted, rejected = await _verify_and_persist(
        candidates_to_verify, fetcher, deadline=deadline
    )

    return {
        "discovered": discovered,
        "skipped_already_known": skipped_known,
        "new_candidates": len(new_candidates),
        "verified_attempted": len(candidates_to_verify),
        "verified_real": verified,
        "persisted_new": persisted,
        "rejected": rejected,
        "skipped_over_cap": (
            0 if max_candidates < 0
            else max(0, len(new_candidates) - max_candidates)
        ),
    }


# ============================================================================
# Stages
# ============================================================================


async def _discover_candidates(
    strategies: list,
    fetcher: PoliteFetcher,
) -> list[tuple[str, SourceCandidate]]:
    """Run every strategy's discover() and gather SourceCandidates."""
    seen_urls: set[str] = set()
    candidates: list[tuple[str, SourceCandidate]] = []

    for strategy in strategies:
        try:
            async for item in strategy.discover(fetcher):
                if not isinstance(item, SourceCandidate):
                    logger.warning(
                        f"Strategy {strategy.name} yielded non-source item; skipping"
                    )
                    continue

                key = _normalize_url(item.url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                candidates.append((strategy.name, item))
        except Exception as e:
            logger.exception(f"Strategy {strategy.name} crashed: {e}")
            continue

    return candidates


async def _verify_and_persist(
    candidates: list[tuple[str, SourceCandidate]],
    fetcher: PoliteFetcher,
    *,
    deadline: Optional[float] = None,
) -> tuple[int, int, int]:
    """Verify each candidate and persist the real ones. Honors deadline."""
    verified_real = 0
    persisted_new = 0
    rejected = 0

    for i, (strategy_name, candidate) in enumerate(candidates, start=1):
        if deadline is not None and time.monotonic() >= deadline:
            logger.info(
                f"Deadline reached mid-verification "
                f"({i - 1}/{len(candidates)} processed); stopping cycle"
            )
            break

        logger.info(
            f"[{i}/{len(candidates)}] Verifying ({strategy_name}): "
            f"{candidate.url}"
        )

        result = await verify_source_candidate(candidate, fetcher)
        if not result.is_real_source:
            rejected += 1
            logger.info(f"  -> rejected: {result.rejection_reason or 'unknown'}")
            continue

        verified_real += 1
        logger.info(
            f"  -> verified ({result.quality_score}/100): "
            f"{result.suggested_name or candidate.name or candidate.url}"
        )

        scraper_hint: dict[str, Any] = {}
        if result.rss_feed_url:
            scraper_hint["rss_feed_url"] = result.rss_feed_url
        if result.api_endpoint_url:
            scraper_hint["api_endpoint_url"] = result.api_endpoint_url

        normalized = _normalize_url(candidate.url)
        source, was_created = upsert_source(
            url=normalized,
            name=result.suggested_name or candidate.name or candidate.url,
            source_type=result.suggested_source_type or SourceType.STRUCTURED_BOARD,
            pipeline=SourcePipeline.CAREER,
            discovered_by="board_hunter",
            discovered_strategy=strategy_name,
            scraper_hint=scraper_hint or None,
            notes=result.rationale,
        )
        if was_created:
            persisted_new += 1
            update_source_status(
                source.id,  # type: ignore[arg-type]
                SourceStatus.QUARANTINE,
                note=f"Verified by board_hunter via {strategy_name}",
            )

    return verified_real, persisted_new, rejected


# ============================================================================
# Helpers
# ============================================================================


def _resolve_strategies(strategy_filter: Optional[str]) -> list:
    """Pick which strategies to run based on the filter."""
    if strategy_filter is not None:
        strategy = get_strategy(strategy_filter)
        if strategy is None:
            raise ValueError(f"No strategy registered with name {strategy_filter!r}")
        if strategy.target_pipeline != SourcePipeline.CAREER:
            raise ValueError(
                f"Strategy {strategy_filter!r} is for pipeline "
                f"{strategy.target_pipeline.value}, not career"
            )
        return [strategy]
    return list(get_board_strategies())


def _normalize_url(url: str) -> str:
    """
    Cheap URL normalization for in-run dedup AND for storage.
    Lowercase scheme+host, strip 'www.', strip trailing slash from path.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return url
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{netloc}{path}"


def _cycle_to_summary(
    cycle: dict[str, int],
    strategies: list,
    *,
    mode: str,
) -> dict[str, Any]:
    """Wrap a single cycle's counts into the summary shape callers expect."""
    return {
        "strategies_run": [s.name for s in strategies],
        "mode": mode,
        **cycle,
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "strategies_run": [],
        "mode": "empty",
        "discovered": 0,
        "skipped_already_known": 0,
        "new_candidates": 0,
        "verified_attempted": 0,
        "verified_real": 0,
        "persisted_new": 0,
        "rejected": 0,
        "skipped_over_cap": 0,
    }