"""
Tear Apart storage — the ListingAnalysis table and its enums.

ListingAnalysis is one-per-Job. Re-running Tear Apart on a job updates the
existing analysis row in place rather than creating a new one — Tear Apart
is the canonical-current normalization, not a history log. The Researcher
also writes back to this row when it completes its work.

If we ever want history, we'll add a TearApartRunLog sibling table.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


# ============================================================================
# Enums
# ============================================================================


class ListingClass(str, Enum):
    """What kind of artifact we're actually looking at."""
    REAL_JOB = "real_job"                 # an actual job posting
    NOT_A_JOB = "not_a_job"               # podcast, article, login wall, etc.
    AGGREGATED_LIST = "aggregated_list"   # "10 best jobs" listicle
    EXPIRED = "expired"                   # explicitly closed/filled
    AMBIGUOUS = "ambiguous"               # too thin to tell — bias REAL_JOB on retry


class WorkArrangement(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class Seniority(str, Enum):
    INTERN = "intern"
    ENTRY = "entry"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    UNKNOWN = "unknown"


class ResearchStatus(str, Enum):
    """Where this row sits in the researcher's queue."""
    NOT_NEEDED = "not_needed"        # listing was complete enough; skip research
    QUEUED = "queued"                # Tear Apart found gaps worth filling
    IN_PROGRESS = "in_progress"      # researcher claimed it
    COMPLETED = "completed"          # researcher finished (gaps filled or unfillable)
    FAILED = "failed"                # researcher couldn't make progress (HTTP, parse, etc.)


# ============================================================================
# ListingAnalysis — the row Tear Apart writes
# ============================================================================


class ListingAnalysis(SQLModel, table=True):
    """
    Structured normalization of a single Job listing.

    One row per Job. Re-runs UPDATE in place; analyzed_at tracks recency.
    The Researcher writes back to fields here once it completes its work,
    flipping research_status to COMPLETED.

    The scorer reads this when present and falls back to Job fields when not,
    so partial Tear Apart coverage is fine — what's analyzed gets the better
    signal, what isn't keeps the existing behavior.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True, unique=True)

    # === Classification ===
    listing_class: ListingClass = Field(default=ListingClass.AMBIGUOUS, index=True)
    is_real_job_confidence: int = 50         # 0-100

    # === Normalized core fields (None means missing/unknown) ===
    title_normalized: Optional[str] = None
    company_normalized: Optional[str] = None
    seniority: Seniority = Field(default=Seniority.UNKNOWN, index=True)
    work_arrangement: WorkArrangement = Field(default=WorkArrangement.UNKNOWN, index=True)
    employment_type: EmploymentType = Field(default=EmploymentType.UNKNOWN)
    salary_min_usd: Optional[int] = None
    salary_max_usd: Optional[int] = None
    salary_currency: Optional[str] = None    # "USD", "EUR", etc.
    location_normalized: Optional[str] = None
    visa_sponsorship_offered: Optional[bool] = None

    # === Skills (JSON-encoded lists for SQLite simplicity) ===
    required_skills_json: str = "[]"
    nice_to_have_skills_json: str = "[]"

    # === Quality and gaps ===
    description_quality_score: int = 0       # 0-100, how rich was the source data
    info_gaps_json: str = "[]"               # [{"field": "...", "why": "..."}, ...]
    rationale: str = ""

    # === Researcher-fillable fields ===
    full_description: Optional[str] = None   # populated by researcher when it fetches the page
    full_description_source: Optional[str] = None  # "rss_summary", "fetched_full_page", etc.

    # === Research queue ===
    research_status: ResearchStatus = Field(default=ResearchStatus.NOT_NEEDED, index=True)
    research_priority: int = 0               # higher = research sooner (set from job score later)
    research_attempts: int = 0
    research_last_error: Optional[str] = None

    # === Provenance ===
    analyzed_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    model_used: str = ""
    teardown_version: str = "v1"
    researched_at: Optional[datetime] = None
    researcher_model_used: Optional[str] = None
