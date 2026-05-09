"""
Sourcing department storage — the source registry, observation log, and gig table.

Three tables live here:
    Source             — every place we know how to look for opportunities.
                         Owned by sourcing, consumed by the scrapers building.
    SourceObservation  — polling history per source, used by pattern_tracker
                         to learn cadence and detect decay.
    Gig                — direct opportunities found by WildHunter, separate
                         from Job because the lifecycle and shape differ
                         (poster, contact, urgency, one-off pay).
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


# ============================================================================
# Enums
# ============================================================================


class SourceType(str, Enum):
    """How a source delivers its content. Drives scraper dispatch."""
    STRUCTURED_BOARD = "structured_board"
    JSON_API = "json_api"
    RSS_FEED = "rss_feed"
    REDDIT_SUBREDDIT = "reddit_subreddit"
    HN_THREAD_PATTERN = "hn_thread_pattern"
    FORUM = "forum"
    BLOG_WATCHER = "blog_watcher"
    TWITTER_SEARCH = "twitter_search"
    GOV_RFP = "gov_rfp"
    OTHER = "other"


class SourceStatus(str, Enum):
    """Lifecycle of a source. Set by hunters, verifier, and pattern_tracker."""
    CANDIDATE = "candidate"      # discovered, awaiting verification
    QUARANTINE = "quarantine"    # verified real, gathering pattern data
    ACTIVE = "active"            # graduated, on learned cadence
    STALE = "stale"              # consecutive empty polls, on probation
    DEAD = "dead"                # 404 / sub closed / confirmed gone, keep row, stop polling
    BLOCKED = "blocked"          # anti-bot wall we haven't beaten yet


class SourcePipeline(str, Enum):
    """Which downstream pipeline this source feeds."""
    CAREER = "career"   # career-track listings -> main dashboard
    GIG = "gig"         # quick-cash opportunities -> gig dashboard
    MIXED = "mixed"     # produces both; classification at Tear Apart


class ObservationEvent(str, Enum):
    """What happened on a single poll attempt."""
    POLL_ATTEMPTED = "poll_attempted"
    LISTINGS_FOUND = "listings_found"
    NO_LISTINGS = "no_listings"
    SOURCE_CHANGED = "source_changed"   # response hash differs but no parseable listings
    SOURCE_BLOCKED = "source_blocked"   # 403, captcha, rate limit
    SOURCE_404 = "source_404"
    CAPTCHA_HIT = "captcha_hit"
    TIER_ESCALATED = "tier_escalated"   # browser ladder moved up


class GigStatus(str, Enum):
    """Application lifecycle for a discovered gig."""
    DISCOVERED = "discovered"
    INTERESTED = "interested"
    CONTACTED = "contacted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class GigPayType(str, Enum):
    ONE_TIME = "one_time"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    PROJECT = "project"
    UNKNOWN = "unknown"


# ============================================================================
# Source — the registry row
# ============================================================================


class Source(SQLModel, table=True):
    """
    A place we know how to look for opportunities. Identity is (source_type, url)
    — same URL with different types is allowed (a forum's RSS feed and the
    forum's HTML are two different sources).
    """
    id: Optional[int] = Field(default=None, primary_key=True)

    # === Identity ===
    name: str
    url: str = Field(index=True)
    domain: str = Field(index=True)         # parsed from URL for rate-limit grouping
    source_type: SourceType = Field(index=True)
    pipeline: SourcePipeline = Field(default=SourcePipeline.CAREER, index=True)

    # === Provenance ===
    discovered_by: str                       # "board_hunter", "wild_hunter", "manual"
    discovered_strategy: Optional[str] = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)

    # === Lifecycle ===
    status: SourceStatus = Field(default=SourceStatus.CANDIDATE, index=True)
    status_updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_verified_at: Optional[datetime] = None
    last_alive_at: Optional[datetime] = None
    consecutive_empty_polls: int = 0

    # === Pattern / cadence ===
    learned_cadence_seconds: Optional[int] = None
    next_poll_at: Optional[datetime] = Field(default=None, index=True)

    # === Politeness ===
    robots_crawl_delay_seconds: Optional[float] = None
    last_browser_tier_used: Optional[str] = None

    # === Scraper hand-off ===
    scraper_hint: Optional[str] = None      # JSON blob, shape varies by source_type

    # === Quality + notes ===
    quality_score: Optional[int] = None     # 0-100, set by verifier
    notes: Optional[str] = None

    # Relationships
    observations: list["SourceObservation"] = Relationship(back_populates="source")
    gigs: list["Gig"] = Relationship(back_populates="source")


# ============================================================================
# SourceObservation — pattern data
# ============================================================================


class SourceObservation(SQLModel, table=True):
    """
    A single poll event for a source. pattern_tracker reads these to learn
    cadence, detect decay, and trigger status transitions.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id", index=True)
    source: Source = Relationship(back_populates="observations")

    observed_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    event_type: ObservationEvent
    listings_count: Optional[int] = None
    new_listings_count: Optional[int] = None
    browser_tier_used: Optional[str] = None
    response_hash: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None


# ============================================================================
# Gig — direct opportunities found by WildHunter
# ============================================================================


class Gig(SQLModel, table=True):
    """
    A discrete opportunity. Lives separately from Job because the shape differs
    and the dashboard split (career vs gig) is real.
    """
    id: Optional[int] = Field(default=None, primary_key=True)

    # === Provenance ===
    source_id: Optional[int] = Field(default=None, foreign_key="source.id", index=True)
    source: Optional[Source] = Relationship(back_populates="gigs")
    discovered_by: str
    discovered_strategy: Optional[str] = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    # === Identity ===
    external_id: Optional[str] = Field(default=None, index=True)  # reddit comment id, etc.
    url: Optional[str] = None

    # === Content ===
    title: str
    poster: Optional[str] = None
    description: str
    location: Optional[str] = None
    is_remote: Optional[bool] = None

    # === Pay ===
    pay_text: Optional[str] = None
    pay_amount_usd: Optional[int] = None
    pay_type: GigPayType = GigPayType.UNKNOWN

    # === Contact ===
    contact_url: Optional[str] = None
    contact_method: Optional[str] = None     # "reddit_dm", "email", etc.

    # === Timing ===
    posted_at: Optional[datetime] = None
    expiry_at: Optional[datetime] = None
    urgency: Optional[str] = None            # "immediate", "week", "month", "flexible"

    # === Lifecycle ===
    status: GigStatus = Field(default=GigStatus.DISCOVERED, index=True)
    notes: Optional[str] = None

    # === Raw ===
    raw_data: Optional[str] = None           # JSON of the original found object