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
    strong = ", ".join(PROFILE.strong_skills) or "(none specified)"
    learning = ", ".join(PROFILE.learning_skills) or "(none specified)"
    target_roles = ", ".join(PROFILE.target_roles) or "(none specified)"
    dealbreakers = ", ".join(PROFILE.dealbreaker_keywords) or "(none specified)"

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
- Target roles: {target_roles}
- Strong skills (the user is solid on these): {strong}
- Learning skills (the user is studying these but not mastered): {learning}
- Salary floor: ${PROFILE.salary_floor_usd:,} (avoid scoring high if listing is below this)
- Salary target: ${PROFILE.salary_target_usd:,}
- Acceptable work arrangements: {arrangements}
- Dealbreaker keywords: {dealbreakers}

## Scoring rubric

- **90-100**: Excellent match. Listed responsibilities and requirements align
  closely with the user's strong skills and target roles. Salary meets target.
  Work arrangement acceptable.
- **70-89**: Good match. Most strong skills relevant, role is in the target
  list or a close variant. Salary above floor. Worth applying.
- **50-69**: Mediocre. Some skills overlap but the role is a stretch (too
  senior, wrong stack, or salary is uncertain). Worth a closer look but not a
  priority.
- **30-49**: Weak. Limited overlap, wrong seniority, or wrong field.
- **0-29**: No fit. Wrong industry, wrong role, dealbreaker keywords present,
  or salary far below floor.

## Output requirements

- score: integer 0-100
- matched_skills: only skills from the user's strong_skills or learning_skills
  lists that the job actually requires or would benefit from. Do not invent
  skills the user doesn't have. Empty list is fine.
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
    """
    agent = build_scorer_agent()
    prompt = _format_job_for_scoring(job)
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


def _format_job_for_scoring(job: Job) -> str:
    """Format a Job into a prompt the scorer can evaluate."""
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
    """Return jobs that have no recent evaluation."""
    with get_session() as session:
        # Jobs with their newest evaluation date (NULL if never evaluated)
        # We do this in two passes for clarity rather than one fancy query.
        all_jobs = session.exec(select(Job)).all()

        needs_scoring = []
        for job in all_jobs:
            newest_eval = session.exec(
                select(Evaluation)
                .where(Evaluation.job_id == job.id)
                .order_by(col(Evaluation.evaluated_at).desc())
                .limit(1)
            ).first()

            if newest_eval is None or newest_eval.evaluated_at < cutoff:
                needs_scoring.append(job)

        if limit is not None:
            needs_scoring = needs_scoring[:limit]

        # Detach from session so caller can use them after session closes
        for job in needs_scoring:
            session.expunge(job)

    return needs_scoring


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
            profile_version=PROFILE.__class__.__name__ + ":v1",  # crude version stamp
        )
        session.add(evaluation)
        session.commit()


def _current_model_id() -> str:
    """Best-effort identifier for which model produced the scores."""
    from app.config import settings
    return f"ollama:{settings.MODEL_LOCAL}"