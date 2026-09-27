"""
Researcher workflow — work the QUEUED items in the ListingAnalysis queue.

The researcher claims rows atomically (QUEUED -> IN_PROGRESS), runs the
fetch_full_listing strategy, writes either a complete_research or
fail_research outcome, then loops.

Concurrency: bounded asyncio.Semaphore. The PoliteFetcher inside the run
enforces per-domain politeness; the semaphore caps total in-flight HTTP
work so we don't open hundreds of sockets.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.buildings.researcher.strategies.fetch_full_listing import (
    FetchResult,
    fetch_full_listing,
)
from app.buildings.sourcing.http_client import PoliteFetcher
from app.buildings.tear_apart.models import ListingAnalysis
from app.buildings.tear_apart.registry import (
    claim_for_research,
    complete_research,
    fail_research,
    get_queued_for_research,
)
from app.config import settings
from app.spine.storage import Job

logger = logging.getLogger(__name__)


# How many fetches in flight at once. PoliteFetcher caps per-domain rate,
# so the semaphore is mostly a socket-budget guardrail.
RESEARCH_CONCURRENCY = 6


async def research_queued_listings(
    *,
    max_jobs: Optional[int] = None,
) -> tuple[int, int, int]:
    """
    Walk the QUEUED rows and run the fetch_full_listing strategy on each.

    Args:
        max_jobs: Cap on rows processed this invocation. None = no cap.

    Returns:
        (researched, failed, skipped) — `researched` = COMPLETED writes,
        `failed` = FAILED writes, `skipped` = couldn't claim (lost race).
    """
    queue = get_queued_for_research(limit=max_jobs)
    if not queue:
        logger.info("Researcher: no QUEUED rows to work.")
        return 0, 0, 0

    logger.info(f"Researcher: working {len(queue)} queued listings")

    semaphore = asyncio.Semaphore(RESEARCH_CONCURRENCY)
    model_label = _researcher_label()
    counts = {"researched": 0, "failed": 0, "skipped": 0}

    async with PoliteFetcher() as fetcher:
        coros = [
            _research_one(job, analysis, fetcher, semaphore, model_label, counts)
            for job, analysis in queue
        ]
        await asyncio.gather(*coros, return_exceptions=False)

    logger.info(
        f"Researcher batch complete: "
        f"{counts['researched']} researched, "
        f"{counts['failed']} failed, "
        f"{counts['skipped']} skipped (claim race)"
    )
    return counts["researched"], counts["failed"], counts["skipped"]


# ============================================================================
# Internals
# ============================================================================


async def _research_one(
    job: Job,
    analysis: ListingAnalysis,
    fetcher: PoliteFetcher,
    semaphore: asyncio.Semaphore,
    model_label: str,
    counts: dict[str, int],
) -> None:
    """Claim, fetch, persist outcome for a single (job, analysis) pair."""
    if analysis.id is None:
        return
    if not claim_for_research(analysis.id):
        # Another runner already grabbed it (or status changed under us)
        counts["skipped"] += 1
        return

    async with semaphore:
        try:
            result: FetchResult = await fetch_full_listing(job.url, fetcher)
        except Exception as e:
            logger.warning(f"Researcher crashed on {job.url}: {e}")
            fail_research(analysis_id=analysis.id, error=f"strategy_exception:{e}")
            counts["failed"] += 1
            return

    if not result.success:
        logger.info(
            f"  [{job.source}] research failed: {result.error} "
            f"({job.title[:50]})"
        )
        fail_research(analysis_id=analysis.id, error=result.error or "unknown")
        counts["failed"] += 1
        return

    body = result.body or ""
    logger.info(
        f"  [{job.source}] research ok: {len(body)} chars ({job.title[:50]})"
    )
    complete_research(
        analysis_id=analysis.id,
        full_description=body,
        full_description_source=result.source_label,
        researcher_model_used=model_label,
    )
    counts["researched"] += 1


def _researcher_label() -> str:
    """Identifier for the researcher tier — currently no LLM, just trafilatura."""
    return f"trafilatura+httpx (no-LLM); local model fallback {settings.MODEL_LOCAL}"
