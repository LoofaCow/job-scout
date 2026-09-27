"""
Source dispatcher — picks the right scrape handler for a Source row.

Decision order:
    1. Hostile aggregator domains (Glassdoor, Indeed, etc.) — return None,
       these need browser tier (Pass 8) at minimum.
    2. ATS-hosted URL pattern (Greenhouse, Lever) — use the ATS handler.
    3. RSS feed available (hint or explicit type) — use the RSS handler.
    4. JSON API hint — best-effort generic JSON handler.
    5. Fall through — None. Source gets logged as "no handler" and skipped.

The dispatcher is a pure function of `Source` — no network calls. The
handler it returns is awaitable; the caller passes a PoliteFetcher.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from app.buildings.job_scout.scrapers.ats import detect_ats_handler
from app.buildings.job_scout.scrapers.feed import feed_scrape
from app.buildings.job_scout.scrapers.json_api import json_api_scrape
from app.buildings.job_scout.scrapers.outcome import ScrapeOutcome
from app.buildings.sourcing.http_client import PoliteFetcher
from app.buildings.sourcing.models import Source, SourceType

logger = logging.getLogger(__name__)


# Aggregators and login-walled boards that we cannot scrape with simple
# HTTP. Polling them just produces blocked-poll observations and burns
# rate limit. They stay in the registry but get skipped by dispatch.
HOSTILE_DOMAINS: frozenset[str] = frozenset({
    "glassdoor.com",
    "indeed.com",
    "www.indeed.com",
    "linkedin.com",
    "www.linkedin.com",
    "ziprecruiter.com",
    "www.ziprecruiter.com",
    "monster.com",
    "www.monster.com",
    "simplyhired.com",
})


Handler = Callable[[Source, PoliteFetcher], Awaitable[ScrapeOutcome]]


class DispatchDecision:
    """Tiny enum-ish — what the dispatcher decided about a source."""
    HANDLER = "handler"
    SKIP_HOSTILE = "skip_hostile"
    SKIP_NO_HANDLER = "skip_no_handler"


def dispatch(source: Source) -> tuple[Optional[Handler], str]:
    """
    Return (handler, reason). Handler is None when we're skipping.
    Reason is one of DispatchDecision constants for logging/metrics.
    """
    if source.domain in HOSTILE_DOMAINS:
        return None, DispatchDecision.SKIP_HOSTILE

    ats = detect_ats_handler(source)
    if ats is not None:
        return ats, DispatchDecision.HANDLER

    if _has_rss(source):
        return feed_scrape, DispatchDecision.HANDLER

    if _has_api_endpoint(source):
        return json_api_scrape, DispatchDecision.HANDLER

    return None, DispatchDecision.SKIP_NO_HANDLER


# ============================================================================
# Internals
# ============================================================================


def _has_rss(source: Source) -> bool:
    if source.source_type == SourceType.RSS_FEED:
        return True
    hint = _parse_hint(source.scraper_hint)
    return bool(hint.get("rss_feed_url"))


def _has_api_endpoint(source: Source) -> bool:
    hint = _parse_hint(source.scraper_hint)
    return bool(hint.get("api_endpoint_url"))


def _parse_hint(blob: Optional[str]) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}
