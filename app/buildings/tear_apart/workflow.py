"""
Tear Apart workflow — batch analyze Job rows that need normalization.

Two entry points:

    analyze_unanalyzed_jobs(...)
        Iterates Job rows that don't have a ListingAnalysis yet, runs the
        agent on each, persists. Used for backfill on the existing 3800+
        scraped jobs.

    reanalyze_after_research(...)
        Iterates ListingAnalysis rows where the researcher just completed
        (research_status == COMPLETED) and there's a `full_description` to
        re-tear-apart. Updates the analysis in place with the richer signal.

Both run sequentially. Tear Apart calls are LLM-bound (~3 sec each on
local Qwen 2.5 7B), so concurrency would just queue inside Ollama with no
real throughput gain. Each row commits independently, so kills / restarts
are safe.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlmodel import col, select

from app.buildings.tear_apart.agent import (
    TearApartResult,
    analyze_job,
)
from app.buildings.tear_apart.models import (
    ListingAnalysis,
    ListingClass,
    ResearchStatus,
)
from app.buildings.tear_apart.registry import (
    get_jobs_needing_analysis,
    upsert_analysis,
)
from app.config import settings
from app.spine.storage import Job, get_session

logger = logging.getLogger(__name__)


# Quality-score threshold below which we queue research. The agent's own
# calibration sets a quality_score under 35 for "title + RSS teaser" rows,
# which is exactly the case where fetching the full page helps most.
RESEARCH_QUALITY_THRESHOLD = 50

# Quality-score threshold below which we MUST queue research even if the
# agent didn't list any explicit info_gaps. Belt-and-braces.
RESEARCH_FORCE_THRESHOLD = 30


async def analyze_unanalyzed_jobs(
    *,
    max_jobs: Optional[int] = None,
    min_score: Optional[int] = None,
) -> tuple[int, int, int]:
    """
    Walk Job rows without a ListingAnalysis, run the agent on each, persist.

    Args:
        max_jobs: Cap on jobs analyzed this invocation. None = no cap.
        min_score: If set, only analyze jobs whose latest evaluation score
            is >= this. Lets you focus Tear Apart on jobs that already
            cleared scoring (1843 rows above 30 right now).

    Returns:
        (analyzed, queued_for_research, failed)
    """
    jobs = get_jobs_needing_analysis(limit=max_jobs, min_score=min_score)
    if not jobs:
        logger.info("Tear Apart: no jobs need analysis.")
        return 0, 0, 0

    logger.info(f"Tear Apart: analyzing {len(jobs)} jobs")

    analyzed = 0
    queued = 0
    failed = 0
    model_id = _current_model_id()

    for i, job in enumerate(jobs, start=1):
        logger.info(
            f"[{i}/{len(jobs)}] Tear Apart: {job.title[:60]} @ {job.company}"
        )
        try:
            result = await analyze_job(job)
            research_status, research_priority = _decide_research_status(result)
            assert job.id is not None, "Job from DB must have id"
            upsert_analysis(
                job_id=job.id,
                listing_class=result.listing_class,
                is_real_job_confidence=result.is_real_job_confidence,
                title_normalized=result.title_normalized,
                company_normalized=result.company_normalized,
                seniority=result.seniority,
                work_arrangement=result.work_arrangement,
                employment_type=result.employment_type,
                salary_min_usd=result.salary_min_usd,
                salary_max_usd=result.salary_max_usd,
                salary_currency=result.salary_currency,
                location_normalized=result.location_normalized,
                visa_sponsorship_offered=result.visa_sponsorship_offered,
                required_skills=list(result.required_skills),
                nice_to_have_skills=list(result.nice_to_have_skills),
                description_quality_score=result.description_quality_score,
                info_gaps=[g.model_dump() for g in result.info_gaps],
                rationale=result.rationale,
                research_status=research_status,
                research_priority=research_priority,
                model_used=model_id,
            )
            analyzed += 1
            if research_status == ResearchStatus.QUEUED:
                queued += 1
            logger.info(
                f"  -> {result.listing_class.value} "
                f"(quality {result.description_quality_score}, "
                f"{len(result.info_gaps)} gaps, "
                f"research={research_status.value})"
            )
        except Exception as e:
            failed += 1
            logger.warning(f"  -> FAILED: {e}")
            continue

    logger.info(
        f"Tear Apart batch complete: {analyzed} analyzed, "
        f"{queued} queued for research, {failed} failed"
    )
    return analyzed, queued, failed


async def reanalyze_after_research(
    *,
    max_jobs: Optional[int] = None,
) -> tuple[int, int]:
    """
    Re-run Tear Apart on rows where the researcher just dropped a richer
    full_description. Updates the analysis in place.

    Returns:
        (reanalyzed, failed)
    """
    pairs = _fetch_completed_research(limit=max_jobs)
    if not pairs:
        logger.info("Tear Apart: no completed-research rows to re-analyze.")
        return 0, 0

    logger.info(f"Tear Apart: re-analyzing {len(pairs)} researched listings")

    reanalyzed = 0
    failed = 0
    model_id = _current_model_id()

    for i, (job, analysis) in enumerate(pairs, start=1):
        logger.info(
            f"[{i}/{len(pairs)}] Tear Apart re-analyze: "
            f"{job.title[:60]} @ {job.company}"
        )
        try:
            result = await analyze_job(
                job,
                override_description=analysis.full_description,
            )
            assert job.id is not None
            # Don't re-queue research after a re-analyze — researcher already ran.
            upsert_analysis(
                job_id=job.id,
                listing_class=result.listing_class,
                is_real_job_confidence=result.is_real_job_confidence,
                title_normalized=result.title_normalized,
                company_normalized=result.company_normalized,
                seniority=result.seniority,
                work_arrangement=result.work_arrangement,
                employment_type=result.employment_type,
                salary_min_usd=result.salary_min_usd,
                salary_max_usd=result.salary_max_usd,
                salary_currency=result.salary_currency,
                location_normalized=result.location_normalized,
                visa_sponsorship_offered=result.visa_sponsorship_offered,
                required_skills=list(result.required_skills),
                nice_to_have_skills=list(result.nice_to_have_skills),
                description_quality_score=result.description_quality_score,
                info_gaps=[g.model_dump() for g in result.info_gaps],
                rationale=result.rationale,
                # Will be ignored by upsert because existing.research_status
                # is COMPLETED; passed for shape compatibility.
                research_status=ResearchStatus.COMPLETED,
                research_priority=0,
                model_used=model_id,
            )
            reanalyzed += 1
            logger.info(
                f"  -> {result.listing_class.value} "
                f"(quality {result.description_quality_score})"
            )
        except Exception as e:
            failed += 1
            logger.warning(f"  -> FAILED: {e}")
            continue

    logger.info(f"Tear Apart re-analysis complete: {reanalyzed} done, {failed} failed")
    return reanalyzed, failed


# ============================================================================
# Internals
# ============================================================================


def _decide_research_status(
    result: TearApartResult,
) -> tuple[ResearchStatus, int]:
    """
    Turn the agent's quality assessment + info_gaps into a research-queue
    decision. Priority 0-100, higher = research sooner.

    Rules:
      - NOT_A_JOB / EXPIRED / AGGREGATED_LIST: never queue (waste of time)
      - REAL_JOB / AMBIGUOUS with quality < FORCE_THRESHOLD: ALWAYS queue
      - REAL_JOB / AMBIGUOUS with quality < RESEARCH_QUALITY_THRESHOLD or any
        info_gaps: queue
      - Else: NOT_NEEDED
    """
    if result.listing_class in (
        ListingClass.NOT_A_JOB,
        ListingClass.EXPIRED,
        ListingClass.AGGREGATED_LIST,
    ):
        return ResearchStatus.NOT_NEEDED, 0

    must_research = result.description_quality_score < RESEARCH_FORCE_THRESHOLD
    should_research = (
        result.description_quality_score < RESEARCH_QUALITY_THRESHOLD
        or len(result.info_gaps) > 0
    )

    if must_research or should_research:
        # Priority: thinner data + AMBIGUOUS class get researched first.
        priority = 100 - result.description_quality_score
        if result.listing_class == ListingClass.AMBIGUOUS:
            priority += 20
        return ResearchStatus.QUEUED, max(0, min(100, priority))

    return ResearchStatus.NOT_NEEDED, 0


def _fetch_completed_research(
    limit: Optional[int],
) -> list[tuple[Job, ListingAnalysis]]:
    """
    Pull (job, analysis) for analyses where research is COMPLETED and the
    re-analysis hasn't happened yet (analyzed_at < researched_at).
    """
    with get_session() as session:
        stmt = (
            select(Job, ListingAnalysis)
            .join(ListingAnalysis, col(ListingAnalysis.job_id) == col(Job.id))
            .where(ListingAnalysis.research_status == ResearchStatus.COMPLETED)
            .where(col(ListingAnalysis.full_description).is_not(None))
            .where(col(ListingAnalysis.analyzed_at) < col(ListingAnalysis.researched_at))
            .order_by(col(ListingAnalysis.researched_at))
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = session.exec(stmt).all()
        out: list[tuple[Job, ListingAnalysis]] = []
        for job, analysis in rows:
            session.expunge(job)
            session.expunge(analysis)
            out.append((job, analysis))
        return out


def _current_model_id() -> str:
    """Best-effort identifier for which model produced the analysis."""
    return f"ollama:{settings.MODEL_LOCAL}"
