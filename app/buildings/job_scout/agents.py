"""
Scout agents — the LLM-driven part of the job_scout building.

For v1 we have a single agent: the Scorer. Given a raw job listing and the
user's profile, it produces a structured fit assessment (score, rationale,
matched skills). Future iteration may split extraction from scoring; for now
one agent does both because it's simpler and the local model handles it fine.
"""

from pydantic import BaseModel, Field

from agno.agent import Agent

from app.profile import PROFILE
from app.spine.models import Tier, get_model
from app.spine.storage import Job


# ============================================================================
# Structured output schema — what the scorer must return
# ============================================================================


class JobAssessment(BaseModel):
    """The scorer's structured output for a single job."""

    score: int = Field(
        ge=0,
        le=100,
        description="Fit score from 0 (terrible match) to 100 (perfect match).",
    )
    matched_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Skills from the user's profile that this job genuinely requires "
            "or would benefit from. Empty if none match."
        ),
    )
    rationale: str = Field(
        description=(
            "2-3 sentence explanation of the score in plain English. "
            "Mention specific job requirements, not generic praise."
        ),
    )
    dealbreaker_hit: str | None = Field(
        default=None,
        description=(
            "If this job hit one of the user's dealbreaker keywords, name it. "
            "Otherwise null."
        ),
    )


# ============================================================================
# The Scorer agent
# ============================================================================


def build_scorer_agent() -> Agent:
    """
    Construct the scorer agent. Called fresh per scoring task so each agent
    instance has clean state — we don't want Agent #50 carrying memory from
    Agent #1's conversation.
    """
    return Agent(
        model=get_model(Tier.LOCAL),
        instructions=_build_instructions(),
        output_schema=JobAssessment,
        # use_json_mode forces the local model into structured-output mode
        # via Ollama's format=json parameter; required for output_schema with
        # most local models
        use_json_mode=True,
    )


def _build_instructions() -> str:
    """
    The system-level instructions baked into every scorer agent.
    Built from PROFILE so it auto-updates when you edit your profile.
    """
    strong = "\n  - " + "\n  - ".join(PROFILE.strong_skills) if PROFILE.strong_skills else "(none specified)"
    learning = "\n  - " + "\n  - ".join(PROFILE.learning_skills) if PROFILE.learning_skills else "(none specified)"
    domains = "\n  - " + "\n  - ".join(PROFILE.domain_expertise) if PROFILE.domain_expertise else "(none specified)"
    target_roles = "\n  - " + "\n  - ".join(PROFILE.target_roles) if PROFILE.target_roles else "(none specified)"
    dealbreakers = ", ".join(PROFILE.dealbreaker_keywords) or "(none specified)"
    seniority = ", ".join(PROFILE.target_seniority) or "(none specified)"

    arrangement_parts = []
    if PROFILE.remote_ok:
        arrangement_parts.append("remote")
    if PROFILE.hybrid_ok:
        arrangement_parts.append("hybrid")
    if PROFILE.onsite_ok:
        arrangement_parts.append("onsite")
    arrangements = ", ".join(arrangement_parts) or "(none specified)"

    return f"""\
You are a job-fit scoring agent. Your job is to evaluate how well a given
job listing matches the user's profile and return a structured assessment.

## User profile

- Name: {PROFILE.name}
- Location: {PROFILE.location_city} (open to relocation: {PROFILE.open_to_relocation})
- Target seniority: {seniority}
- Salary floor: ${PROFILE.salary_floor_usd:,} (avoid scoring high if listing is below this)
- Salary target: ${PROFILE.salary_target_usd:,}
- Acceptable work arrangements: {arrangements}
- Dealbreaker keywords: {dealbreakers}

### Background

{PROFILE.background_summary}

### Target roles (priority-ordered, earlier = higher pull)
{target_roles}

### Strong skills (productive without much reference)
{strong}

### Learning skills (studying or willing to ramp; can speak to but lean on docs/AI)
{learning}

### Domain expertise (areas of unusually deep knowledge)
{domains}

## Scoring rubric

- **90-100**: Excellent match. Role is squarely in the target list, multiple
  strong skills are explicitly required, salary meets target, work arrangement
  acceptable. Reserve for genuinely uncommon-quality fits.
- **70-89**: Good match. Role is a target or close variant, strong-skill
  overlap is substantial, salary above floor. Worth applying.
- **50-69**: Mediocre. Partial overlap — some required skills land in
  strong/learning, but the role is a stretch (seniority gap, missing one
  required tech, or salary uncertain). Worth a closer look.
- **30-49**: Weak. Limited overlap, wrong seniority, or wrong field, but not
  obviously disqualifying. Surface for triage.
- **0-29**: No fit. Wrong industry, wrong role, dealbreaker keywords present,
  far below salary floor, or seniority requires years of experience the user
  cannot honestly claim.

## Calibration notes

- The user is entry-to-junior in the software/AI/IT track. Senior, Staff,
  Principal, or Lead titles requiring 5+ years should not score above 49 even
  when stacks match — gracefully cap with a "too senior" rationale.
- The user has substantial hands-on technical breadth (hardware repair, QC,
  retail-tech advisory) that maps to non-software roles. Don't penalize a
  hardware/repair/QC role just because it isn't software.
- AI/agent/LLM-related roles where the listing emphasizes Ollama, multi-agent
  systems, prompt engineering, FastAPI, or local-inference design should
  score notably higher when paired with junior/associate seniority — that's
  the user's strongest current edge.
- Quality-control and manufacturing-process roles are valid matches; treat
  the user's Andersen experience as real quality-engineering background.
- Customer-facing technical-support roles (especially involving consumer
  electronics, batteries, or repair) should score well — the user has years
  of high-volume direct experience.

## Output requirements

- score: integer 0-100
- matched_skills: only skills from the user's strong_skills, learning_skills,
  or domain_expertise lists that the job actually requires or would benefit
  from. Do not invent skills the user doesn't have. Empty list is fine.
- rationale: 2-3 plain-English sentences. Reference specific requirements
  from the listing, not generic statements. If the user's experience is too
  junior or too senior for the role, say so explicitly.
- dealbreaker_hit: if you see any of the dealbreaker keywords in the listing,
  name the specific keyword. Otherwise null.

Be honest. The user wants accurate scoring, not optimistic scoring. A 45 with
a clear "you're too junior for this senior role" rationale is more valuable
than a 75 with vague praise.
"""


# ============================================================================
# Public scoring function
# ============================================================================


async def score_job(job: Job) -> JobAssessment:
    """
    Score a single job. Returns the structured assessment.

    Builds a fresh agent per call so concurrent scoring runs don't share state.

    If a ListingAnalysis row exists for this job, the scorer prefers its
    normalized fields and full_description over the raw scrape — that's the
    whole point of running Tear Apart before scoring.
    """
    # Lazy import to avoid a circular dependency: tear_apart imports
    # spine.storage; spine.storage doesn't know about tear_apart.
    from app.buildings.tear_apart.registry import get_analysis_for_job
    from app.buildings.tear_apart.models import ListingClass

    analysis = get_analysis_for_job(job.id) if job.id is not None else None

    # Short-circuit obvious non-jobs the Tear Apart classifier already caught.
    if analysis is not None and analysis.listing_class in (
        ListingClass.NOT_A_JOB,
        ListingClass.EXPIRED,
        ListingClass.AGGREGATED_LIST,
    ):
        return JobAssessment(
            score=0,
            matched_skills=[],
            rationale=(
                f"Tear Apart classified this listing as "
                f"{analysis.listing_class.value} — not a real open job. "
                f"Auto-scored 0."
            ),
            dealbreaker_hit=None,
        )

    agent = build_scorer_agent()
    prompt = _format_job_for_scoring(job, analysis=analysis)
    response = await agent.arun(prompt)

    # Agno returns a RunResponse; with output_schema=JobAssessment + use_json_mode,
    # response.content is parsed into a JobAssessment instance. Assert to satisfy
    # the type checker AND to surface a real error if Agno ever returns something
    # else (e.g., model output failed schema validation and got returned raw).
    assessment = response.content
    if not isinstance(assessment, JobAssessment):
        raise RuntimeError(
            f"Scorer returned unexpected content type: {type(assessment).__name__}"
        )
    return assessment


def _format_job_for_scoring(job: Job, *, analysis=None) -> str:
    """
    Format a Job (and its ListingAnalysis if present) into a scorer prompt.

    Without analysis: uses raw scrape fields (legacy behavior).
    With analysis: uses normalized title/company/salary/arrangement and the
    fetched full_description when available.
    """
    if analysis is None:
        salary_line = job.salary_text or "Not specified"
        arrangement_parts = []
        if job.is_remote:
            arrangement_parts.append("remote")
        if job.is_hybrid:
            arrangement_parts.append("hybrid")
        arrangement = ", ".join(arrangement_parts) or "Not specified"

        return f"""\
Evaluate this job listing.

Title: {job.title}
Company: {job.company}
Location: {job.location}
Salary: {salary_line}
Work arrangement: {arrangement}

Description:
{job.description}
"""

    # === Tear Apart-aware format ===
    from app.buildings.tear_apart.registry import matched_skills_from_analysis

    title = analysis.title_normalized or job.title
    company = analysis.company_normalized or job.company
    location = analysis.location_normalized or job.location

    # Salary line: prefer normalized USD range, fall back to scraped text
    if analysis.salary_min_usd or analysis.salary_max_usd:
        cur = analysis.salary_currency or "USD"
        if analysis.salary_min_usd and analysis.salary_max_usd:
            salary_line = f"{cur} ${analysis.salary_min_usd:,} - ${analysis.salary_max_usd:,}"
        elif analysis.salary_min_usd:
            salary_line = f"{cur} from ${analysis.salary_min_usd:,}"
        else:
            salary_line = f"{cur} up to ${analysis.salary_max_usd:,}"
    else:
        salary_line = job.salary_text or "Not specified"

    arrangement = analysis.work_arrangement.value if analysis.work_arrangement else "unknown"
    employment_type = analysis.employment_type.value if analysis.employment_type else "unknown"
    seniority = analysis.seniority.value if analysis.seniority else "unknown"

    required, nice = matched_skills_from_analysis(analysis)
    required_str = ", ".join(required) or "(not stated)"
    nice_str = ", ".join(nice) or "(not stated)"

    description = analysis.full_description or job.description
    description_source = (
        analysis.full_description_source
        if analysis.full_description
        else "rss_summary (raw scrape)"
    )

    visa_line = (
        "yes" if analysis.visa_sponsorship_offered is True
        else "no" if analysis.visa_sponsorship_offered is False
        else "not stated"
    )

    return f"""\
Evaluate this job listing.

Title: {title}
Company: {company}
Location: {location}
Salary: {salary_line}
Work arrangement: {arrangement}
Employment type: {employment_type}
Seniority: {seniority}
Visa sponsorship: {visa_line}

Skills required: {required_str}
Skills nice-to-have: {nice_str}

Tear Apart classification: {analysis.listing_class.value} (confidence {analysis.is_real_job_confidence}/100)
Description quality score: {analysis.description_quality_score}/100

Description (source: {description_source}):
{description}
"""

# ============================================================================
# Batch scoring — score many jobs and persist evaluations
# ============================================================================


import json
import logging
from datetime import datetime, timedelta

from sqlmodel import col, select

from app.spine.storage import Evaluation, get_session

logger = logging.getLogger(__name__)


async def score_unscored_jobs(
    *,
    max_jobs: int | None = None,
    rescore_after_days: int = 7,
) -> tuple[int, int]:
    """
    Score every job that needs scoring, write Evaluations to the DB.

    A job needs scoring if:
        - It has no Evaluation yet, OR
        - Its most recent Evaluation is older than `rescore_after_days`

    Args:
        max_jobs: Cap on how many jobs to score this run. None = unlimited.
            Useful for testing and for keeping nightly runtime bounded.
        rescore_after_days: Re-score jobs whose newest evaluation is older
            than this many days. Default 7.

    Returns:
        (scored_count, failed_count)
    """
    cutoff = datetime.utcnow() - timedelta(days=rescore_after_days)
    jobs_to_score = _find_jobs_needing_scoring(cutoff=cutoff, limit=max_jobs)

    logger.info(f"Found {len(jobs_to_score)} jobs needing scoring")

    scored = 0
    failed = 0
    model_id = _current_model_id()

    for i, job in enumerate(jobs_to_score, start=1):
        logger.info(f"[{i}/{len(jobs_to_score)}] Scoring: {job.title[:60]} @ {job.company}")
        try:
            assessment = await score_job(job)
            _persist_evaluation(job=job, assessment=assessment, model_used=model_id)
            scored += 1
            logger.info(f"  -> {assessment.score}/100")
        except Exception as e:
            failed += 1
            logger.warning(f"  -> FAILED: {e}")
            continue

    logger.info(f"Batch complete: {scored} scored, {failed} failed")
    return scored, failed


def _find_jobs_needing_scoring(*, cutoff: datetime, limit: int | None) -> list[Job]:
    """
    Return jobs whose newest evaluation is missing or older than `cutoff`.

    Single SQL: jobs LEFT JOIN (latest_evaluation_per_job) WHERE the latest
    is NULL or older than cutoff. Replaces an old per-row lookup that
    issued 3800+ round-trips on the current DB.
    """
    from sqlalchemy import func, or_

    with get_session() as session:
        latest_eval_subq = (
            select(
                Evaluation.job_id,
                func.max(Evaluation.evaluated_at).label("latest_at"),
            )
            .group_by(col(Evaluation.job_id))
            .subquery()
        )

        stmt = (
            select(Job)
            .select_from(Job)
            .outerjoin(
                latest_eval_subq,
                col(latest_eval_subq.c.job_id) == col(Job.id),
            )
            .where(
                or_(
                    col(latest_eval_subq.c.latest_at).is_(None),
                    col(latest_eval_subq.c.latest_at) < cutoff,
                )
            )
            .order_by(col(Job.id))
        )
        if limit is not None:
            stmt = stmt.limit(limit)

        jobs = session.exec(stmt).all()
        for job in jobs:
            session.expunge(job)
        return list(jobs)


def _persist_evaluation(*, job: Job, assessment: JobAssessment, model_used: str) -> None:
    """Write one Evaluation row for a job + its assessment."""
    assert job.id is not None, "Job from DB must have an id"
    with get_session() as session:
        evaluation = Evaluation(
            job_id=job.id,
            score=assessment.score,
            matched_skills=json.dumps(assessment.matched_skills),
            rationale=assessment.rationale,
            model_used=model_used,
            profile_version=PROFILE.__class__.__name__ + ":v2",  # bumped 2026-05-10 (resume-bible profile)
        )
        session.add(evaluation)
        session.commit()


def _current_model_id() -> str:
    """Best-effort identifier for which model produced the scores."""
    from app.config import settings
    return f"ollama:{settings.MODEL_LOCAL}"