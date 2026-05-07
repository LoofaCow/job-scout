"""
Application storage — SQLite via SQLModel for jobs, evaluations, and runs.

This is for *application state* (the actual job listings, scores, application
tracking). Agent memory and session state live in a separate Agno-managed
database — see app/spine/agno_storage.py.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Engine
from sqlmodel import Field, Relationship, Session, SQLModel, create_engine

from app.config import PROJECT_ROOT, settings


# ============================================================================
# Models — the database schema
# ============================================================================


class ApplicationStatus(str, Enum):
    """Where you are in the application process for a given job."""
    NOT_APPLIED = "not_applied"
    INTERESTED = "interested"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    REJECTED = "rejected"
    OFFER = "offer"
    WITHDRAWN = "withdrawn"
    ARCHIVED = "archived"  # not interested, hide from dashboard


class Job(SQLModel, table=True):
    """
    A job listing as it exists in the world. Immutable after first scrape —
    we don't update jobs, we just observe them.
    """
    id: Optional[int] = Field(default=None, primary_key=True)

    # Source identity — uniquely identifies this listing across re-scrapes
    source: str = Field(index=True)         # "indeed", "remoteok", etc.
    source_job_id: str = Field(index=True)  # the board's own ID for the listing

    # Core listing data
    title: str
    company: str
    location: str
    url: str
    description: str  # full HTML/text body, used for scoring

    # Optional fields the scraper may or may not extract
    salary_text: Optional[str] = None      # raw "$60k-80k" string from the listing
    salary_min_usd: Optional[int] = None   # parsed integer; null if unavailable
    salary_max_usd: Optional[int] = None
    is_remote: Optional[bool] = None
    is_hybrid: Optional[bool] = None

    # Provenance
    first_seen_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    posted_at: Optional[datetime] = None  # when the board says it was posted

    # Application tracking — this IS mutable, it's about your relationship to the job
    status: ApplicationStatus = Field(default=ApplicationStatus.NOT_APPLIED, index=True)
    notes: Optional[str] = None  # free-text notes you write yourself

    # Relationship — one job, many evaluations over time
    evaluations: list["Evaluation"] = Relationship(back_populates="job")


class Evaluation(SQLModel, table=True):
    """
    What the scoring agent thought of a job at a particular moment.
    Multiple evaluations per job over time = scoring history.
    """
    id: Optional[int] = Field(default=None, primary_key=True)

    job_id: int = Field(foreign_key="job.id", index=True)
    job: Job = Relationship(back_populates="evaluations")

    # Scoring output
    score: int  # 0-100
    matched_skills: str  # JSON-encoded list, kept as string for SQLite simplicity
    rationale: str       # the scorer's natural-language explanation

    # Provenance
    evaluated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    model_used: str      # which model produced this score (for later analysis)
    profile_version: str = "v1"  # bumped when you materially edit profile.py


class ScoutRun(SQLModel, table=True):
    """
    A single execution of the scout pipeline. Useful for the dashboard
    ("last run was 6 hours ago, found 12 jobs, surfaced 4").
    """
    id: Optional[int] = Field(default=None, primary_key=True)

    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    finished_at: Optional[datetime] = None

    # Counters — what happened during this run
    jobs_found: int = 0       # raw listings the scrapers returned
    jobs_new: int = 0         # how many were new (not previously in DB)
    jobs_evaluated: int = 0   # how many got a score
    jobs_surfaced: int = 0    # how many made it past the threshold

    # If something blew up
    error: Optional[str] = None


# ============================================================================
# Engine + session helpers
# ============================================================================


def _build_engine() -> Engine:
    """
    Create the SQLAlchemy engine. We resolve the SQLite path against the
    project root so the DB always lands in the same place regardless of
    where you run commands from.
    """
    db_url = settings.DATABASE_URL

    # If it's a SQLite URL with a relative path, anchor it to PROJECT_ROOT
    if db_url.startswith("sqlite:///./"):
        rel_path = db_url.replace("sqlite:///./", "")
        absolute_path = PROJECT_ROOT / rel_path
        db_url = f"sqlite:///{absolute_path}"

    return create_engine(db_url, echo=False)


# Module-level singleton so every import shares one connection pool
engine = _build_engine()


def init_db() -> None:
    """Create all tables. Idempotent — safe to call on every startup."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    """
    Return a new database session. Caller is responsible for closing it
    (use as a context manager: `with get_session() as s:`).
    """
    return Session(engine)