"""
BoardHunter — runs every board strategy, verifies candidates, persists sources.

The hunter is intentionally not an LLM agent itself. It's an orchestrator:
    1. Pulls the registered board strategies
    2. Runs each one's discover() to gather SourceCandidates
    3. Dedupes URLs across strategies
    4. Caps the candidate count (verification = LLM call per candidate)
    5. Verifies each via the verifier helper
    6. Upserts verified candidates into the Source registry

LLM work happens inside the verifier helper, not here. If a future iteration
needs a meta-agent to decide WHICH strategies to run, that goes in here.
"""

from __future__ import annotations

import logging
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


DEFAULT_MAX_CANDIDATES = 100  # cap LLM calls per run; bump via CLI for real runs


async def run_board_hunter(
    *,
    strategy_filter: Optional[str] = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> dict[str, Any]:
    """
    Execute one BoardHunter run.

    Args:
        strategy_filter: If set, run only the strategy with this name.
            Useful for development.
        max_candidates: Cap on how many candidates we send to the verifier.
            Each verification = one LLM call, so this controls runtime cost.

    Returns:
        A summary dict with discovered/verified/persisted/skipped counts.
    """
    # === Resolve which strategies to run ===
    if strategy_filter is not None:
        strategy = get_strategy(strategy_filter)
        if strategy is None:
            raise ValueError(f"No strategy registered with name {strategy_filter!r}")
        if strategy.target_pipeline != SourcePipeline.CAREER:
            raise ValueError(
                f"Strategy {strategy_filter!r} is for pipeline "
                f"{strategy.target_pipeline.value}, not career"
            )
        strategies = [strategy]
    else:
        strategies = get_board_strategies()

    if not strategies:
        logger.warning("BoardHunter: no strategies registered")
        return _empty_summary()

    logger.info(
        f"BoardHunter starting — {len(strategies)} strategies, "
        f"max_candidates={max_candidates}"
    )

    # === Discovery + verification share one fetcher (rate limits + robots cache) ===
    async with PoliteFetcher() as fetcher:
        candidates = await _discover_candidates(strategies, fetcher)
        logger.info(f"Discovery complete: {len(candidates)} unique candidates")

        # === Cross-run dedup: skip URLs already in the registry ===
        all_urls = [_normalize_url(c.url) for _, c in candidates]
        unknown_urls = filter_already_known_urls(all_urls)
        new_candidates = [
            (sn, c) for sn, c in candidates
            if _normalize_url(c.url) in unknown_urls
        ]
        skipped_known = len(candidates) - len(new_candidates)
        if skipped_known:
            logger.info(
                f"Cross-run dedup: skipping {skipped_known} candidates "
                f"already in the registry"
            )

        candidates_to_verify = (
            new_candidates if max_candidates < 0
            else new_candidates[:max_candidates]
        )
        if max_candidates >= 0 and len(new_candidates) > max_candidates:
            logger.info(
                f"Capping verification at {max_candidates} "
                f"(found {len(new_candidates)} new)"
            )

        verified, persisted, rejected = await _verify_and_persist(
            candidates_to_verify, fetcher
        )

    summary = {
        "strategies_run": [s.name for s in strategies],
        "discovered": len(candidates),
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
    logger.info(f"BoardHunter complete: {summary}")
    return summary


# ============================================================================
# Stages
# ============================================================================


async def _discover_candidates(
    strategies: list,
    fetcher: PoliteFetcher,
) -> list[tuple[str, SourceCandidate]]:
    """
    Run every strategy's discover() and gather SourceCandidates.
    Dedupes across strategies by normalized URL.

    Returns a list of (strategy_name, candidate) tuples.
    """
    seen_urls: set[str] = set()
    candidates: list[tuple[str, SourceCandidate]] = []

    for strategy in strategies:
        try:
            async for item in strategy.discover(fetcher):
                # Strategies can yield SourceCandidate or GigCandidate; the
                # board hunter only handles SourceCandidate. Skip anything else.
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
) -> tuple[int, int, int]:
    """
    Verify each candidate and persist the real ones.

    Returns:
        (verified_real, persisted_new, rejected)
    """
    verified_real = 0
    persisted_new = 0
    rejected = 0

    for i, (strategy_name, candidate) in enumerate(candidates, start=1):
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

        # === Persist ===
        scraper_hint: dict[str, Any] = {}
        if result.rss_feed_url:
            scraper_hint["rss_feed_url"] = result.rss_feed_url
        if result.api_endpoint_url:
            scraper_hint["api_endpoint_url"] = result.api_endpoint_url

        source, was_created = upsert_source(
            url=candidate.url,
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
            # Move from CANDIDATE to QUARANTINE — verifier confirmed it's real,
            # pattern_tracker (Pass 3) will graduate to ACTIVE later.
            update_source_status(
                source.id,  # type: ignore[arg-type]  # upserted source always has an id
                SourceStatus.QUARANTINE,
                note=f"Verified by board_hunter via {strategy_name}",
            )

    return verified_real, persisted_new, rejected


# ============================================================================
# Helpers
# ============================================================================


def _normalize_url(url: str) -> str:
    """
    Cheap URL normalization for in-run dedup.
    Lowercase scheme+host, strip trailing slash from path.
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


def _empty_summary() -> dict[str, Any]:
    return {
        "strategies_run": [],
        "discovered": 0,
        "verified_attempted": 0,
        "verified_real": 0,
        "persisted_new": 0,
        "rejected": 0,
        "skipped_over_cap": 0,
    }