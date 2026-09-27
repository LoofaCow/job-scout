"""
ScrapeOutcome — uniform return shape for every per-source scraper handler.

Handlers return one of these per source. The workflow uses `event` to drive
SourceObservation logging (so pattern_tracker eventually has data) and
`jobs` to feed the upsert/score pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.buildings.sourcing.models import ObservationEvent
from app.spine.storage import Job


@dataclass
class ScrapeOutcome:
    """The result of polling a single Source row."""

    jobs: list[Job] = field(default_factory=list)
    event: ObservationEvent = ObservationEvent.POLL_ATTEMPTED
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    handler_name: str = "unknown"

    @classmethod
    def empty(cls, *, handler_name: str, duration_ms: int) -> "ScrapeOutcome":
        return cls(
            jobs=[],
            event=ObservationEvent.NO_LISTINGS,
            duration_ms=duration_ms,
            handler_name=handler_name,
        )

    @classmethod
    def found(
        cls,
        jobs: list[Job],
        *,
        handler_name: str,
        duration_ms: int,
    ) -> "ScrapeOutcome":
        return cls(
            jobs=jobs,
            event=(
                ObservationEvent.LISTINGS_FOUND if jobs
                else ObservationEvent.NO_LISTINGS
            ),
            duration_ms=duration_ms,
            handler_name=handler_name,
        )

    @classmethod
    def http_error(
        cls,
        status: int,
        *,
        handler_name: str,
        duration_ms: int,
    ) -> "ScrapeOutcome":
        if status == 404:
            event = ObservationEvent.SOURCE_404
        elif status in (401, 403, 429):
            event = ObservationEvent.SOURCE_BLOCKED
        else:
            event = ObservationEvent.POLL_ATTEMPTED
        return cls(
            jobs=[],
            event=event,
            error_message=f"HTTP {status}",
            duration_ms=duration_ms,
            handler_name=handler_name,
        )

    @classmethod
    def fetch_failed(
        cls,
        message: str,
        *,
        handler_name: str,
        duration_ms: int,
    ) -> "ScrapeOutcome":
        return cls(
            jobs=[],
            event=ObservationEvent.POLL_ATTEMPTED,
            error_message=message,
            duration_ms=duration_ms,
            handler_name=handler_name,
        )
