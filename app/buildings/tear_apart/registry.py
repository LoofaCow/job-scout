"""
Tear Apart registry — DB read/write for ListingAnalysis rows.

The agent and workflow modules call into here; they do not write SQL
themselves. Keeps data access auditable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlmodel import col, select

from app.buildings.tear_apart.models import (
    ListingAnalysis,
    ListingClass,
    ResearchStatus,
)
from app.spine.storage import Evaluation, Job, get_session

logger = logging.getLogger(__name__)


# ============================================================================
# Reads
# ============================================================================


def get_jobs_needing_analysis(
    *,
    limit: Optional[int] = None,
    min_score: Optional[int] = None,
) -> list[Job]:
    """
    Return Job rows that don't have a ListingAnalysis yet.

    Args:
        limit: cap on rows returned. None = no cap.
        min_score: only return jobs whose latest evaluation score is at or
            above this. None = no score filter. Useful to focus Tear Apart
            effort on jobs that already cleared scoring (the rest is noise).
    """
    from sqlalchemy import and_, func

    with get_session() as session:
        # Subquery: job_ids that already have an analysis row.
        # Use the bare Select (NOT wrapped in .subquery() then another select()) —
        # `not_in` accepts a Select directly, and double-wrapping triggers
        # SQLAlchemy ArgumentError in 2.x.
        analyzed_ids_subq = select(ListingAnalysis.job_id)

        stmt = select(Job).select_from(Job).where(
            col(Job.id).not_in(analyzed_ids_subq)
        )

        if min_score is not None:
            # Join to the most recent Evaluation per job and filter on its score.
            latest_eval_subq = (
                select(
                    Evaluation.job_id,
                    func.max(Evaluation.evaluated_at).label("latest_at"),
                )
                .group_by(col(Evaluation.job_id))
                .subquery()
            )
            stmt = (
                stmt
                .join(latest_eval_subq, col(latest_eval_subq.c.job_id) == col(Job.id))
                .join(
                    Evaluation,
                    and_(
                        col(Evaluation.job_id) == col(latest_eval_subq.c.job_id),
                        col(Evaluation.evaluated_at) == col(latest_eval_subq.c.latest_at),
                    ),
                )
                .where(col(Evaluation.score) >= min_score)
            )

        stmt = stmt.order_by(col(Job.id))
        if limit is not None:
            stmt = stmt.limit(limit)

        jobs = session.exec(stmt).all()
        for j in jobs:
            session.expunge(j)
        return list(jobs)


def get_analysis_for_job(job_id: int) -> Optional[ListingAnalysis]:
    """Fetch one analysis by job_id, detached from session."""
    with get_session() as session:
        analysis = session.exec(
            select(ListingAnalysis).where(ListingAnalysis.job_id == job_id)
        ).first()
        if analysis is not None:
            session.expunge(analysis)
        return analysis


def get_queued_for_research(limit: Optional[int] = None) -> list[tuple[Job, ListingAnalysis]]:
    """
    Return (job, analysis) pairs where analysis.research_status == QUEUED.

    Higher research_priority comes first; ties broken by analyzed_at asc
    (older queue entries get worked first).
    """
    with get_session() as session:
        stmt = (
            select(Job, ListingAnalysis)
            .join(ListingAnalysis, col(ListingAnalysis.job_id) == col(Job.id))
            .where(ListingAnalysis.research_status == ResearchStatus.QUEUED)
            .order_by(
                col(ListingAnalysis.research_priority).desc(),
                col(ListingAnalysis.analyzed_at),
            )
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


# ============================================================================
# Writes
# ============================================================================


def upsert_analysis(
    *,
    job_id: int,
    listing_class: ListingClass,
    is_real_job_confidence: int,
    title_normalized: Optional[str],
    company_normalized: Optional[str],
    seniority,
    work_arrangement,
    employment_type,
    salary_min_usd: Optional[int],
    salary_max_usd: Optional[int],
    salary_currency: Optional[str],
    location_normalized: Optional[str],
    visa_sponsorship_offered: Optional[bool],
    required_skills: list[str],
    nice_to_have_skills: list[str],
    description_quality_score: int,
    info_gaps: list[dict[str, str]],
    rationale: str,
    research_status: ResearchStatus,
    research_priority: int,
    model_used: str,
    teardown_version: str = "v1",
) -> ListingAnalysis:
    """
    Insert or update the ListingAnalysis row for a job. Returns the saved row.
    """
    with get_session() as session:
        existing = session.exec(
            select(ListingAnalysis).where(ListingAnalysis.job_id == job_id)
        ).first()

        if existing is None:
            row = ListingAnalysis(job_id=job_id)
            session.add(row)
        else:
            row = existing

        row.listing_class = listing_class
        row.is_real_job_confidence = is_real_job_confidence
        row.title_normalized = title_normalized
        row.company_normalized = company_normalized
        row.seniority = seniority
        row.work_arrangement = work_arrangement
        row.employment_type = employment_type
        row.salary_min_usd = salary_min_usd
        row.salary_max_usd = salary_max_usd
        row.salary_currency = salary_currency
        row.location_normalized = location_normalized
        row.visa_sponsorship_offered = visa_sponsorship_offered
        row.required_skills_json = json.dumps(required_skills)
        row.nice_to_have_skills_json = json.dumps(nice_to_have_skills)
        row.description_quality_score = description_quality_score
        row.info_gaps_json = json.dumps(info_gaps)
        row.rationale = rationale
        # Don't trample research_status if researcher already finished;
        # only set it if it hasn't been worked yet or was previously NOT_NEEDED.
        if existing is None or existing.research_status in (
            ResearchStatus.NOT_NEEDED,
            ResearchStatus.QUEUED,
        ):
            row.research_status = research_status
            row.research_priority = research_priority
        row.analyzed_at = datetime.utcnow()
        row.model_used = model_used
        row.teardown_version = teardown_version

        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def claim_for_research(analysis_id: int) -> bool:
    """
    Atomically transition QUEUED -> IN_PROGRESS so two researcher runs
    don't double-process the same row.

    Uses a conditional UPDATE so the queued-check and the status flip
    happen in a single SQL statement. Read-then-write would race on
    concurrent runners (SQLite default isolation lets both readers see
    QUEUED before either writes).

    Returns True if this caller claimed the row; False if it was already
    claimed, completed, failed, or doesn't exist.
    """
    from sqlalchemy import update

    with get_session() as session:
        stmt = (
            update(ListingAnalysis)
            .where(ListingAnalysis.id == analysis_id)
            .where(ListingAnalysis.research_status == ResearchStatus.QUEUED)
            .values(research_status=ResearchStatus.IN_PROGRESS)
        )
        result = session.exec(stmt)  # type: ignore[arg-type]
        session.commit()
        # SQLAlchemy 2.x: rowcount tells us whether the WHERE matched.
        return getattr(result, "rowcount", 0) > 0


def complete_research(
    *,
    analysis_id: int,
    full_description: Optional[str],
    full_description_source: Optional[str],
    researcher_model_used: str,
) -> None:
    """
    Mark research COMPLETED and store the fetched description body.
    Tear Apart will re-run on this richer text in the next analyze pass.
    """
    with get_session() as session:
        row = session.get(ListingAnalysis, analysis_id)
        if row is None:
            logger.warning(f"complete_research: no analysis with id={analysis_id}")
            return
        row.full_description = full_description
        row.full_description_source = full_description_source
        row.research_status = ResearchStatus.COMPLETED
        row.researched_at = datetime.utcnow()
        row.researcher_model_used = researcher_model_used
        row.research_attempts = (row.research_attempts or 0) + 1
        session.add(row)
        session.commit()


def fail_research(*, analysis_id: int, error: str) -> None:
    """Record a researcher failure; the row stays at FAILED until manually retried."""
    with get_session() as session:
        row = session.get(ListingAnalysis, analysis_id)
        if row is None:
            return
        row.research_status = ResearchStatus.FAILED
        row.research_attempts = (row.research_attempts or 0) + 1
        row.research_last_error = error[:500]
        row.researched_at = datetime.utcnow()
        session.add(row)
        session.commit()


def matched_skills_from_analysis(analysis: ListingAnalysis) -> tuple[list[str], list[str]]:
    """Decode the JSON skill lists. Helper for the scorer."""
    try:
        required = json.loads(analysis.required_skills_json) or []
    except (json.JSONDecodeError, TypeError):
        required = []
    try:
        nice = json.loads(analysis.nice_to_have_skills_json) or []
    except (json.JSONDecodeError, TypeError):
        nice = []
    return list(required), list(nice)


def info_gaps_from_analysis(analysis: ListingAnalysis) -> list[dict[str, Any]]:
    try:
        gaps = json.loads(analysis.info_gaps_json) or []
    except (json.JSONDecodeError, TypeError):
        gaps = []
    return list(gaps)
