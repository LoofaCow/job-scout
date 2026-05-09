"""
Sourcing registry — the only module that writes sourcing tables.

Hunters, verifiers, and the pattern_tracker call into here. They do not write
SQL themselves. This keeps the building's data access in one auditable place.
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from sqlmodel import col, select

from app.spine.storage import get_session

from app.buildings.sourcing.models import (
    Gig,
    ObservationEvent,
    Source,
    SourceObservation,
    SourcePipeline,
    SourceStatus,
    SourceType,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Source CRUD
# ============================================================================


def upsert_source(
    *,
    url: str,
    name: str,
    source_type: SourceType,
    pipeline: SourcePipeline,
    discovered_by: str,
    discovered_strategy: Optional[str] = None,
    scraper_hint: Optional[dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> tuple[Source, bool]:
    """
    Insert or fetch a source by (source_type, url).

    Identity is (source_type, url). The same URL may be registered as multiple
    types if it's accessible different ways (HTML page + RSS feed + JSON API).

    Returns:
        (source, was_created) — was_created is True if we just inserted it,
        False if it already existed.
    """
    domain = urlparse(url).netloc.lower()

    with get_session() as session:
        existing = session.exec(
            select(Source).where(
                Source.source_type == source_type,
                Source.url == url,
            )
        ).first()

        if existing is not None:
            session.expunge(existing)
            return existing, False

        source = Source(
            url=url,
            domain=domain,
            name=name,
            source_type=source_type,
            pipeline=pipeline,
            discovered_by=discovered_by,
            discovered_strategy=discovered_strategy,
            scraper_hint=json.dumps(scraper_hint) if scraper_hint else None,
            notes=notes,
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        session.expunge(source)
        return source, True


def get_source(source_id: int) -> Optional[Source]:
    """Fetch one source by id, detached from session."""
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is not None:
            session.expunge(source)
        return source


def get_due_sources(now: Optional[datetime] = None, limit: int = 50) -> list[Source]:
    """
    Return ACTIVE/QUARANTINE sources whose next_poll_at has passed.

    Used by the future scheduler to pull a batch of sources to poll. Sources
    with next_poll_at IS NULL are also returned — they've never been polled
    and need their first poll.
    """
    now = now or datetime.utcnow()
    pollable = (SourceStatus.ACTIVE, SourceStatus.QUARANTINE)

    with get_session() as session:
        sources = session.exec(
            select(Source)
            .where(col(Source.status).in_(pollable))
            .where(
                (col(Source.next_poll_at).is_(None))
                | (col(Source.next_poll_at) <= now)
            )
            .order_by(col(Source.next_poll_at))
            .limit(limit)
        ).all()
        for s in sources:
            session.expunge(s)
        return list(sources)


def update_source_status(
    source_id: int,
    new_status: SourceStatus,
    *,
    note: Optional[str] = None,
) -> None:
    """Transition a source's status and stamp the change time."""
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            logger.warning(f"update_source_status: no source with id={source_id}")
            return
        source.status = new_status
        source.status_updated_at = datetime.utcnow()
        if note:
            source.notes = (source.notes + "\n" if source.notes else "") + note
        session.add(source)
        session.commit()


def mark_source_alive(source_id: int) -> None:
    """Record that a source returned real listings just now."""
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            return
        source.last_alive_at = datetime.utcnow()
        source.consecutive_empty_polls = 0
        session.add(source)
        session.commit()


def increment_empty_polls(source_id: int) -> int:
    """Bump the empty-poll counter and return the new value."""
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            return 0
        source.consecutive_empty_polls += 1
        session.add(source)
        session.commit()
        return source.consecutive_empty_polls


def schedule_next_poll(source_id: int, cadence_seconds: int) -> None:
    """Set next_poll_at = now + cadence_seconds. Also stores cadence on the row."""
    from datetime import timedelta
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            return
        source.learned_cadence_seconds = cadence_seconds
        source.next_poll_at = datetime.utcnow() + timedelta(seconds=cadence_seconds)
        session.add(source)
        session.commit()


# ============================================================================
# Observation log
# ============================================================================


def log_observation(
    source_id: int,
    event_type: ObservationEvent,
    *,
    listings_count: Optional[int] = None,
    new_listings_count: Optional[int] = None,
    browser_tier_used: Optional[str] = None,
    response_hash: Optional[str] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Append a row to SourceObservation for pattern_tracker to read later."""
    with get_session() as session:
        obs = SourceObservation(
            source_id=source_id,
            event_type=event_type,
            listings_count=listings_count,
            new_listings_count=new_listings_count,
            browser_tier_used=browser_tier_used,
            response_hash=response_hash,
            error_message=error_message,
            duration_ms=duration_ms,
        )
        session.add(obs)
        session.commit()


# ============================================================================
# Gig CRUD
# ============================================================================


def save_gig(gig: Gig) -> tuple[Gig, bool]:
    """
    Insert a gig if we don't already have it.

    Identity is (source_id, external_id) when external_id is set, else url.
    Returns (gig, was_created).
    """
    with get_session() as session:
        if gig.external_id is not None and gig.source_id is not None:
            existing = session.exec(
                select(Gig).where(
                    Gig.source_id == gig.source_id,
                    Gig.external_id == gig.external_id,
                )
            ).first()
        elif gig.url is not None:
            existing = session.exec(
                select(Gig).where(Gig.url == gig.url)
            ).first()
        else:
            existing = None

        if existing is not None:
            session.expunge(existing)
            return existing, False

        session.add(gig)
        session.commit()
        session.refresh(gig)
        session.expunge(gig)
        return gig, True