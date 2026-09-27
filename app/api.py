"""
FastAPI app — exposes the job_scout building over HTTP.

Endpoints:
    GET  /health                  — liveness check
    GET  /jobs/surfaced           — jobs at or above the surfacing threshold
    GET  /jobs/{job_id}           — single job with its latest evaluation
    POST /jobs/{job_id}/status    — update application status
    GET  /runs                    — recent scout runs for the dashboard header
    POST /scout/run               — trigger a run manually (background task)
    GET  /scout/status            — is a run currently in progress?

The desktop app and Android app both hit these endpoints. CORS is open to
localhost so the Tauri dev server can call us during development.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import and_, func, or_
from sqlmodel import col, select

from app.buildings.job_scout.workflow import run_scout
from app.buildings import sourcing  # noqa: F401  -- register sourcing tables with SQLModel
from app.buildings import tear_apart  # noqa: F401  -- register tear_apart tables with SQLModel
from app.profile import PROFILE
from app.spine.storage import (
    ApplicationStatus,
    Evaluation,
    Job,
    ScoutRun,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)


# ============================================================================
# In-process state for the "is scout running?" check
# ============================================================================


class ScoutState:
    """Tracks whether a scout run is currently in progress."""
    is_running: bool = False
    current_run_id: Optional[int] = None


SCOUT_STATE = ScoutState()


# ============================================================================
# App lifecycle
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the DB on startup."""
    logger.info("API starting up — initializing database")
    init_db()
    yield
    logger.info("API shutting down")


app = FastAPI(
    title="Job Scout API",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow the Tauri dev server (and any local browser tab) to call us.
# In production we'll lock this down, but for desktop-app-on-localhost it's fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# Response models
# ============================================================================


class SurfacedJob(BaseModel):
    """A job + its latest evaluation, shaped for the dashboard."""
    id: int
    source: str
    title: str
    company: str
    location: str
    url: str
    salary_text: Optional[str]
    is_remote: Optional[bool]
    is_hybrid: Optional[bool]
    posted_at: Optional[datetime]
    first_seen_at: datetime
    status: ApplicationStatus

    # From the latest evaluation
    score: int
    matched_skills: list[str]
    rationale: str
    evaluated_at: datetime


class JobDetail(SurfacedJob):
    """Like SurfacedJob but includes the full description."""
    description: str


class RunSummary(BaseModel):
    """One ScoutRun for the dashboard header."""
    id: int
    started_at: datetime
    finished_at: Optional[datetime]
    jobs_found: int
    jobs_new: int
    jobs_evaluated: int
    jobs_surfaced: int
    error: Optional[str]


class StatusUpdate(BaseModel):
    status: ApplicationStatus


class ScoutStatus(BaseModel):
    is_running: bool
    current_run_id: Optional[int]


# ============================================================================
# Endpoints
# ============================================================================


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "job_scout_api"}


class SurfacedPage(BaseModel):
    """Paginated surfaced jobs + total count for the dashboard counter."""
    total: int
    jobs: list[SurfacedJob]


@app.get("/jobs/surfaced", response_model=SurfacedPage)
async def get_surfaced_jobs(
    limit: int = 200,
    offset: int = 0,
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "score",
) -> SurfacedPage:
    """
    Return jobs at or above the surfacing threshold, with optional filters.

    Returns the matching page plus the total count so the dashboard can
    show "showing 200 of 955 jobs" without a second request.

    Filters:
        min_score: floor for latest score (default = profile threshold)
        max_score: ceiling for latest score
        source:    exact-match Job.source (use /jobs/sources for the list)
        search:    case-insensitive substring across title and company
        sort:      "score" (default, desc) or "newest" (first_seen_at desc)

    A single SQL query joins each job to its latest evaluation, so the
    cost is O(jobs above threshold), not O(all jobs).
    """
    threshold = (
        min_score if min_score is not None
        else PROFILE.minimum_score_to_surface
    )

    with get_session() as session:
        base = _build_surfaced_query(
            threshold=threshold,
            max_score=max_score,
            source=source,
            search=search,
        )

        # Total count for "showing X of Y" — same filters, no pagination.
        total = session.exec(
            select(func.count()).select_from(base.subquery())
        ).one()

        if sort == "newest":
            ordered = base.order_by(
                col(Job.first_seen_at).desc(),
                col(Evaluation.score).desc(),
            )
        else:
            ordered = base.order_by(
                col(Evaluation.score).desc(),
                col(Job.first_seen_at).desc(),
            )

        rows = session.exec(ordered.offset(offset).limit(limit)).all()
        jobs = [_to_surfaced_job(job, evaluation) for job, evaluation in rows]

    return SurfacedPage(total=total, jobs=jobs)


@app.get("/jobs/sources", response_model=list[str])
async def get_distinct_sources() -> list[str]:
    """
    Distinct Job.source values among non-archived jobs that meet the
    profile's surfacing threshold. Drives the dashboard's source filter.
    """
    threshold = PROFILE.minimum_score_to_surface

    with get_session() as session:
        base = _build_surfaced_query(threshold=threshold)
        stmt = (
            select(Job.source)
            .select_from(base.subquery())
            .distinct()
            .order_by(col(Job.source))
        )
        return [row for row in session.exec(stmt).all() if row]


def _build_surfaced_query(
    *,
    threshold: int,
    max_score: Optional[int] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
):
    """
    Shared core query for the surfaced view: jobs joined to their most
    recent evaluation, filtered by score, source, and search. Used by
    both /jobs/surfaced and /jobs/sources.
    """
    latest_eval_subq = (
        select(
            Evaluation.job_id,
            func.max(Evaluation.evaluated_at).label("latest_at"),
        )
        .group_by(col(Evaluation.job_id))
        .subquery()
    )

    base = (
        select(Job, Evaluation)
        .select_from(Job)
        .join(latest_eval_subq, col(latest_eval_subq.c.job_id) == col(Job.id))
        .join(
            Evaluation,
            and_(
                col(Evaluation.job_id) == col(latest_eval_subq.c.job_id),
                col(Evaluation.evaluated_at) == col(latest_eval_subq.c.latest_at),
            ),
        )
        .where(Job.status != ApplicationStatus.ARCHIVED)
        .where(col(Evaluation.score) >= threshold)
    )

    if max_score is not None:
        base = base.where(col(Evaluation.score) <= max_score)
    if source:
        base = base.where(Job.source == source)
    if search:
        pattern = f"%{search.strip()}%"
        base = base.where(
            or_(
                col(Job.title).ilike(pattern),
                col(Job.company).ilike(pattern),
            )
        )

    return base


@app.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: int) -> JobDetail:
    """Full detail for a single job, including the description."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

        latest_eval = session.exec(
            select(Evaluation)
            .where(Evaluation.job_id == job_id)
            .order_by(col(Evaluation.evaluated_at).desc())
            .limit(1)
        ).first()

        if latest_eval is None:
            raise HTTPException(
                status_code=404,
                detail=f"Job {job_id} has no evaluation yet",
            )

        surfaced = _to_surfaced_job(job, latest_eval)

    return JobDetail(**surfaced.model_dump(), description=job.description)


@app.post("/jobs/{job_id}/status")
async def update_job_status(job_id: int, update: StatusUpdate) -> dict:
    """Update the application status (interested, applied, archived, etc.)."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        job.status = update.status
        session.add(job)
        session.commit()
    return {"ok": True, "job_id": job_id, "status": update.status.value}


@app.get("/runs", response_model=list[RunSummary])
async def get_runs(limit: int = 10) -> list[RunSummary]:
    """Recent scout runs, newest first. Header of the dashboard."""
    with get_session() as session:
        runs = session.exec(
            select(ScoutRun).order_by(col(ScoutRun.started_at).desc()).limit(limit)
        ).all()
        return [RunSummary(**r.model_dump()) for r in runs]


@app.get("/scout/status", response_model=ScoutStatus)
async def scout_status() -> ScoutStatus:
    """Is a scout run currently in progress?"""
    return ScoutStatus(
        is_running=SCOUT_STATE.is_running,
        current_run_id=SCOUT_STATE.current_run_id,
    )


@app.post("/scout/run")
async def trigger_scout_run() -> dict:
    """
    Kick off a scout run in the background. Returns immediately; the dashboard
    polls /scout/status to know when it's done.
    """
    if SCOUT_STATE.is_running:
        raise HTTPException(
            status_code=409,
            detail="A scout run is already in progress",
        )

    asyncio.create_task(_run_scout_background())
    return {"ok": True, "message": "Scout run started in background"}


# ============================================================================
# Helpers
# ============================================================================


async def _run_scout_background() -> None:
    """Wrap run_scout with state tracking so the dashboard can show progress."""
    SCOUT_STATE.is_running = True
    SCOUT_STATE.current_run_id = None
    try:
        run = await run_scout()
        SCOUT_STATE.current_run_id = run.id
        logger.info(f"Background run completed: #{run.id}")
    except Exception as e:
        logger.exception(f"Background scout run failed: {e}")
    finally:
        SCOUT_STATE.is_running = False


def _to_surfaced_job(job: Job, evaluation: Evaluation) -> SurfacedJob:
    """Combine a Job and its Evaluation into the dashboard-shaped response."""
    assert job.id is not None, "Job from DB must have an id"
    return SurfacedJob(
        id=job.id,
        source=job.source,
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        salary_text=job.salary_text,
        is_remote=job.is_remote,
        is_hybrid=job.is_hybrid,
        posted_at=job.posted_at,
        first_seen_at=job.first_seen_at,
        status=job.status,
        score=evaluation.score,
        matched_skills=json.loads(evaluation.matched_skills),
        rationale=evaluation.rationale,
        evaluated_at=evaluation.evaluated_at,
    )