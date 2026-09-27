"""
ATS (Applicant Tracking System) scrapers — Greenhouse, Lever.

These cloud ATS platforms host hundreds of companies' careers pages on a
predictable URL pattern with a documented JSON API. When the verifier
discovers `boards.greenhouse.io/{slug}` or `jobs.lever.co/{slug}`, we
can scrape the full job listings without any HTML parsing.

Today's registry has very few ATS-hosted entries (because the discovery
strategies look for boards, not company careers pages). This handler is
still here for two reasons:
    1. A handful of registry sources will be covered correctly today.
    2. Future sourcing passes (especially direct company seeding) will
       discover many more, and this handler will pick them up automatically.

Detection priority is by URL pattern, before generic feed/JSON handlers.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from app.buildings.job_scout.scrapers.outcome import ScrapeOutcome
from app.buildings.sourcing.http_client import PoliteFetcher
from app.buildings.sourcing.models import Source
from app.spine.storage import Job

logger = logging.getLogger(__name__)


# ============================================================================
# Detection
# ============================================================================


_GREENHOUSE_PATH = re.compile(r"^/(?:embed/job_board\?for=)?([A-Za-z0-9_\-]+)/?")
_LEVER_PATH = re.compile(r"^/([A-Za-z0-9_\-]+)/?")


def detect_ats_handler(
    source: Source,
) -> Optional[Callable[[Source, PoliteFetcher], Awaitable[ScrapeOutcome]]]:
    """
    Return the ATS handler to use for a source, or None if no match.

    Detection is purely from the source URL — fast, no network call.
    """
    parsed = urlparse(source.url)
    host = parsed.netloc.lower()

    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        slug = _extract_first_path_segment(parsed.path)
        if slug:
            return greenhouse_scrape

    if host == "jobs.lever.co":
        slug = _extract_first_path_segment(parsed.path)
        if slug:
            return lever_scrape

    return None


def _extract_first_path_segment(path: str) -> Optional[str]:
    """Pull the first non-empty path segment, e.g. '/acme/foo' -> 'acme'."""
    parts = [p for p in path.split("/") if p]
    return parts[0] if parts else None


# ============================================================================
# Greenhouse
# ============================================================================

GREENHOUSE_HANDLER_NAME = "ats:greenhouse"


async def greenhouse_scrape(source: Source, fetcher: PoliteFetcher) -> ScrapeOutcome:
    """
    Greenhouse public board API. Endpoint:
        https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

    Response shape:
        {"jobs": [{"id", "title", "absolute_url", "location": {"name"},
                   "content" (HTML), "updated_at", ...}]}
    """
    started = time.monotonic()

    parsed = urlparse(source.url)
    slug = _extract_first_path_segment(parsed.path)
    if not slug:
        return ScrapeOutcome.fetch_failed(
            "could not extract slug from greenhouse URL",
            handler_name=GREENHOUSE_HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    api_url = (
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    )

    response = await fetcher.get(api_url)
    if response is None:
        return ScrapeOutcome.fetch_failed(
            "greenhouse fetch returned None",
            handler_name=GREENHOUSE_HANDLER_NAME,
            duration_ms=_ms_since(started),
        )
    if response.status_code >= 400:
        return ScrapeOutcome.http_error(
            response.status_code,
            handler_name=GREENHOUSE_HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    try:
        payload = response.json()
    except ValueError as e:
        return ScrapeOutcome.fetch_failed(
            f"greenhouse non-JSON response: {e}",
            handler_name=GREENHOUSE_HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        job = _greenhouse_to_job(raw, source)
        if job is not None:
            jobs.append(job)

    return ScrapeOutcome.found(
        jobs,
        handler_name=GREENHOUSE_HANDLER_NAME,
        duration_ms=_ms_since(started),
    )


def _greenhouse_to_job(raw: dict[str, Any], source: Source) -> Optional[Job]:
    title = (raw.get("title") or "").strip()
    job_id = raw.get("id")
    url = (raw.get("absolute_url") or "").strip()
    if not title or not job_id or not url:
        return None

    location = ""
    loc_obj = raw.get("location")
    if isinstance(loc_obj, dict):
        location = (loc_obj.get("name") or "").strip()

    description = (raw.get("content") or "").strip()
    posted_at = _parse_iso(raw.get("updated_at"))

    # Greenhouse exposes the company name only on the board metadata,
    # not on each job. The board slug is a reasonable proxy.
    parsed = urlparse(source.url)
    slug = _extract_first_path_segment(parsed.path) or source.name
    company = source.name or slug.replace("-", " ").title()

    return Job(
        source=source.domain,
        source_job_id=str(job_id),
        title=title,
        company=company,
        location=location or "Not specified",
        url=url,
        description=description,
        salary_text=None,
        salary_min_usd=None,
        salary_max_usd=None,
        is_remote=("remote" in location.lower()) if location else None,
        is_hybrid=("hybrid" in location.lower()) if location else None,
        posted_at=posted_at,
    )


# ============================================================================
# Lever
# ============================================================================

LEVER_HANDLER_NAME = "ats:lever"


async def lever_scrape(source: Source, fetcher: PoliteFetcher) -> ScrapeOutcome:
    """
    Lever public postings API. Endpoint:
        https://api.lever.co/v0/postings/{slug}?mode=json

    Response shape: top-level array of
        [{"id", "text", "categories": {"team","commitment","location"},
          "descriptionPlain", "hostedUrl", "createdAt" (ms epoch)}]
    """
    started = time.monotonic()

    parsed = urlparse(source.url)
    slug = _extract_first_path_segment(parsed.path)
    if not slug:
        return ScrapeOutcome.fetch_failed(
            "could not extract slug from lever URL",
            handler_name=LEVER_HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"

    response = await fetcher.get(api_url)
    if response is None:
        return ScrapeOutcome.fetch_failed(
            "lever fetch returned None",
            handler_name=LEVER_HANDLER_NAME,
            duration_ms=_ms_since(started),
        )
    if response.status_code >= 400:
        return ScrapeOutcome.http_error(
            response.status_code,
            handler_name=LEVER_HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    try:
        payload = response.json()
    except ValueError as e:
        return ScrapeOutcome.fetch_failed(
            f"lever non-JSON response: {e}",
            handler_name=LEVER_HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    raw_jobs = payload if isinstance(payload, list) else []
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        job = _lever_to_job(raw, source, slug)
        if job is not None:
            jobs.append(job)

    return ScrapeOutcome.found(
        jobs,
        handler_name=LEVER_HANDLER_NAME,
        duration_ms=_ms_since(started),
    )


def _lever_to_job(raw: dict[str, Any], source: Source, slug: str) -> Optional[Job]:
    title = (raw.get("text") or "").strip()
    posting_id = raw.get("id")
    url = (raw.get("hostedUrl") or "").strip()
    if not title or not posting_id or not url:
        return None

    categories = raw.get("categories") or {}
    if not isinstance(categories, dict):
        categories = {}
    location = (categories.get("location") or "").strip()
    commitment = (categories.get("commitment") or "").strip()

    description_html = (raw.get("description") or "").strip()
    description_plain = (raw.get("descriptionPlain") or "").strip()
    description = description_plain or description_html

    posted_at = _ms_to_datetime(raw.get("createdAt"))

    company = source.name or slug.replace("-", " ").title()

    is_remote = "remote" in location.lower() if location else None
    is_hybrid = "hybrid" in location.lower() if location else None

    return Job(
        source=source.domain,
        source_job_id=str(posting_id),
        title=title,
        company=company,
        location=location or commitment or "Not specified",
        url=url,
        description=description,
        salary_text=None,
        salary_min_usd=None,
        salary_max_usd=None,
        is_remote=is_remote,
        is_hybrid=is_hybrid,
        posted_at=posted_at,
    )


# ============================================================================
# Helpers
# ============================================================================


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ms_to_datetime(v: Any) -> Optional[datetime]:
    if not isinstance(v, (int, float)):
        return None
    try:
        return datetime.utcfromtimestamp(v / 1000)
    except (OSError, ValueError):
        return None


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
