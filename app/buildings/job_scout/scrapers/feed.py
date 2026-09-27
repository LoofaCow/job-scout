"""
RSS/Atom feed scraper — handles any Source whose listings come from a feed.

This is the highest-value handler in the registry-driven pipeline. ~257 of
the verified sources have either source_type=RSS_FEED or a `rss_feed_url`
hint that points at a feed; this handler covers all of them.

We use feedparser, which tolerates malformed feeds, mixed RSS/Atom, weird
encodings, and redirected feed URLs. It returns a uniform structure
regardless of feed dialect.

Job description quality from RSS varies wildly:
    - Some feeds return the full job posting in entry.summary.
    - Many return a 200-500 char teaser.
    - A few only have title + link.

We score with whatever we get. Pass 6 (Tear Apart) will eventually fetch
the full page to enrich short summaries; for today, summary is enough to
get a baseline score.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import feedparser

from app.buildings.job_scout.scrapers.outcome import ScrapeOutcome
from app.buildings.sourcing.http_client import PoliteFetcher
from app.buildings.sourcing.models import Source, SourceType
from app.spine.storage import Job

logger = logging.getLogger(__name__)

HANDLER_NAME = "feed"

# Cap on entries we'll process from a single feed, to keep first-run volume
# sane. Feeds rarely include more than 50 anyway, but a few aggregator feeds
# return 500+ items.
MAX_ENTRIES_PER_FEED = 100


async def feed_scrape(source: Source, fetcher: PoliteFetcher) -> ScrapeOutcome:
    """Poll a Source's RSS/Atom feed and return parsed Job rows."""
    started = time.monotonic()

    feed_url = _resolve_feed_url(source)
    if feed_url is None:
        return ScrapeOutcome.fetch_failed(
            "no feed URL on source",
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    response = await fetcher.get(feed_url)
    if response is None:
        return ScrapeOutcome.fetch_failed(
            f"fetch returned None for {feed_url}",
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )
    if response.status_code >= 400:
        return ScrapeOutcome.http_error(
            response.status_code,
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    parsed = feedparser.parse(response.text)
    if parsed.bozo and not parsed.entries:
        # Bozo means feedparser flagged a parse problem AND we got nothing
        # useful. Treat as a soft failure rather than crashing.
        return ScrapeOutcome.fetch_failed(
            f"feedparser bozo: {getattr(parsed, 'bozo_exception', 'unknown')}",
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    jobs: list[Job] = []
    for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
        try:
            job = _entry_to_job(entry, source, feed_url)
            if job is not None:
                jobs.append(job)
        except Exception as e:
            logger.debug(
                f"feed: failed to parse one entry from {source.url}: {e}"
            )
            continue

    return ScrapeOutcome.found(
        jobs,
        handler_name=HANDLER_NAME,
        duration_ms=_ms_since(started),
    )


# ============================================================================
# Internals
# ============================================================================


def _resolve_feed_url(source: Source) -> Optional[str]:
    """
    Pick the feed URL to fetch.

    Order of preference:
        1. scraper_hint["rss_feed_url"] — what the verifier extracted
        2. source.url — only if source_type is RSS_FEED

    Hints can be relative (`/feed`); absolutize them against the source URL.
    """
    hint = _parse_hint(source.scraper_hint)
    raw = hint.get("rss_feed_url")
    if raw:
        return _absolutize(raw, source.url)
    if source.source_type == SourceType.RSS_FEED:
        return source.url
    return None


def _parse_hint(blob: Optional[str]) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _absolutize(maybe_relative: str, base_url: str) -> str:
    """Resolve a hint URL against the source URL if it's relative."""
    parsed = urlparse(maybe_relative)
    if parsed.scheme and parsed.netloc:
        return maybe_relative
    return urljoin(base_url, maybe_relative)


def _entry_to_job(
    entry: Any,
    source: Source,
    feed_url: str,
) -> Optional[Job]:
    """Convert one feedparser entry into a Job. Returns None to skip junk."""
    link = (getattr(entry, "link", "") or "").strip()
    title = (getattr(entry, "title", "") or "").strip()
    if not link or not title:
        return None

    # Stable per-source ID. Most feeds set entry.id (an opaque guid). Fall
    # back to link, which is at least URL-stable.
    entry_id = (
        getattr(entry, "id", None)
        or getattr(entry, "guid", None)
        or link
    )
    source_job_id = str(entry_id).strip()
    if not source_job_id:
        return None

    description = _pick_description(entry)
    company = _guess_company(entry, source)
    location = _guess_location(entry) or "Not specified"
    posted_at = _pick_published(entry)

    return Job(
        source=source.domain,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        url=link,
        description=description,
        # RSS feeds rarely expose structured salary or arrangement metadata;
        # the Tear Apart pass will fill these in.
        salary_text=None,
        salary_min_usd=None,
        salary_max_usd=None,
        is_remote=None,
        is_hybrid=None,
        posted_at=posted_at,
    )


def _pick_description(entry: Any) -> str:
    """
    Best-effort description extraction from a feedparser entry.

    Order: content[0].value > summary > description > title (last resort).
    Some feeds put the full body in content[], others in summary.
    """
    content = getattr(entry, "content", None)
    if content:
        try:
            first = content[0]
            value = first.get("value") if isinstance(first, dict) else None
            if value:
                return str(value).strip()
        except (IndexError, AttributeError, TypeError):
            pass

    summary = getattr(entry, "summary", "") or ""
    if summary:
        return str(summary).strip()

    description = getattr(entry, "description", "") or ""
    if description:
        return str(description).strip()

    return getattr(entry, "title", "") or ""


def _guess_company(entry: Any, source: Source) -> str:
    """
    Best-effort company name. Many feeds don't expose a structured
    company field, so we look for common variants and fall back to the
    source name (which may be the board's brand, not the hiring company —
    Tear Apart will improve on this later).
    """
    for attr in ("author", "publisher", "dc_creator"):
        value = getattr(entry, attr, "") or ""
        if value and not _looks_like_email(value):
            return str(value).strip()

    tags = getattr(entry, "tags", None)
    if tags:
        try:
            for tag in tags:
                if isinstance(tag, dict) and tag.get("term"):
                    return str(tag["term"]).strip()
        except (AttributeError, TypeError):
            pass

    return source.name or "Unknown"


def _guess_location(entry: Any) -> Optional[str]:
    """Some feeds use `where`, `location`, or geo extensions. Best-effort."""
    for attr in ("where", "location", "geo_placename"):
        value = getattr(entry, attr, "") or ""
        if value:
            return str(value).strip()
    return None


def _pick_published(entry: Any) -> Optional[datetime]:
    """Convert feedparser's parsed time tuple to a UTC datetime."""
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6])
    except (TypeError, ValueError):
        return None


def _looks_like_email(s: str) -> bool:
    return "@" in s and "." in s.split("@")[-1]


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
