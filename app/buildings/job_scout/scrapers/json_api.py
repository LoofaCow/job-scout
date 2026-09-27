"""
Generic JSON-API scraper — best-effort handler for sources whose
`scraper_hint` includes an `api_endpoint_url`.

Reality check: only ~14 sources in the registry have a JSON hint, and
many of those hints look brittle (search query templates, widget URLs).
This handler does its best — fetches, attempts to find a list of
job-shaped dicts, extracts a few common fields. Sources that don't
conform get logged and skipped.

For known-pattern ATS endpoints (Greenhouse, Lever), the dispatcher
routes to ats.py instead, which has structure-specific extractors.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from app.buildings.job_scout.scrapers.outcome import ScrapeOutcome
from app.buildings.sourcing.http_client import PoliteFetcher
from app.buildings.sourcing.models import Source
from app.spine.storage import Job

logger = logging.getLogger(__name__)

HANDLER_NAME = "json_api"

# Common field-name guesses for the "list of jobs" wrapper key.
LIST_KEY_CANDIDATES = [
    "jobs", "data", "results", "items", "postings", "listings",
    "openings", "positions", "vacancies",
]


async def json_api_scrape(source: Source, fetcher: PoliteFetcher) -> ScrapeOutcome:
    """Poll the source's API endpoint and best-effort extract jobs."""
    started = time.monotonic()

    api_url = _resolve_api_url(source)
    if api_url is None:
        return ScrapeOutcome.fetch_failed(
            "no API endpoint URL on source",
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    response = await fetcher.get(api_url)
    if response is None:
        return ScrapeOutcome.fetch_failed(
            f"fetch returned None for {api_url}",
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )
    if response.status_code >= 400:
        return ScrapeOutcome.http_error(
            response.status_code,
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as e:
        return ScrapeOutcome.fetch_failed(
            f"non-JSON response: {e}",
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    raw_listings = _find_listing_array(payload)
    if not raw_listings:
        logger.debug(
            f"json_api: could not locate listings array in payload from {api_url}"
        )
        return ScrapeOutcome.empty(
            handler_name=HANDLER_NAME,
            duration_ms=_ms_since(started),
        )

    jobs: list[Job] = []
    for raw in raw_listings:
        if not isinstance(raw, dict):
            continue
        try:
            job = _generic_to_job(raw, source)
            if job is not None:
                jobs.append(job)
        except Exception as e:
            logger.debug(f"json_api: failed to parse one record: {e}")
            continue

    return ScrapeOutcome.found(
        jobs,
        handler_name=HANDLER_NAME,
        duration_ms=_ms_since(started),
    )


# ============================================================================
# Internals
# ============================================================================


def _resolve_api_url(source: Source) -> Optional[str]:
    """Extract and absolutize the api_endpoint_url hint."""
    hint = _parse_hint(source.scraper_hint)
    raw = hint.get("api_endpoint_url")
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return raw
    return urljoin(source.url, raw)


def _parse_hint(blob: Optional[str]) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _find_listing_array(payload: Any) -> list[Any]:
    """
    Locate the array of job records in the JSON payload.

    Heuristic search:
        - If payload is a list, assume that's it.
        - If payload is a dict, look at common wrapper keys.
        - Walk one level deep through dict values to find the longest list
          of dicts (last-resort guess).
    """
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    for key in LIST_KEY_CANDIDATES:
        value = payload.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value

    # Last-resort: walk the dict's first level and pick the longest list of
    # dicts. Catches APIs with non-standard wrapper keys.
    best: list[Any] = []
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if len(value) > len(best):
                best = value
    return best


def _generic_to_job(raw: dict[str, Any], source: Source) -> Optional[Job]:
    """Best-effort field extraction from a generic JSON record."""
    title = _first_str(raw, ("title", "position", "name", "job_title"))
    if not title:
        return None

    url = _first_str(raw, ("url", "absolute_url", "apply_url", "link", "hostedUrl", "jobUrl"))
    if not url:
        return None

    source_job_id = _first_str(raw, ("id", "job_id", "uid", "slug")) or url
    company = _first_str(raw, ("company", "company_name", "employer", "organization")) or source.name
    location = _first_str(raw, ("location", "city", "place")) or _read_nested_location(raw) or "Not specified"
    description = _first_str(raw, ("description", "content", "descriptionPlain", "body", "summary")) or ""

    salary_text = _first_str(raw, ("salary", "salary_text", "compensation"))
    salary_min, salary_max = _parse_salary_range(raw)

    is_remote = _first_bool(raw, ("remote", "is_remote", "fully_remote"))

    posted_at = _first_datetime(raw, ("posted_at", "created_at", "published_at", "date", "createdAt"))

    return Job(
        source=source.domain,
        source_job_id=str(source_job_id),
        title=str(title),
        company=str(company),
        location=str(location),
        url=str(url),
        description=str(description),
        salary_text=str(salary_text) if salary_text else None,
        salary_min_usd=salary_min,
        salary_max_usd=salary_max,
        is_remote=is_remote,
        is_hybrid=None,
        posted_at=posted_at,
    )


def _first_str(d: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    """Return the first non-empty string-coercible value found at any key."""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return None


def _first_bool(d: dict[str, Any], keys: tuple[str, ...]) -> Optional[bool]:
    for k in keys:
        if k in d and isinstance(d[k], bool):
            return d[k]
    return None


def _read_nested_location(raw: dict[str, Any]) -> Optional[str]:
    """Some APIs nest location under {location: {name: ...}}."""
    loc = raw.get("location")
    if isinstance(loc, dict):
        return _first_str(loc, ("name", "city", "label"))
    return None


def _parse_salary_range(raw: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    smin = raw.get("salary_min") or raw.get("min_salary")
    smax = raw.get("salary_max") or raw.get("max_salary")
    return _coerce_int(smin), _coerce_int(smax)


def _coerce_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _first_datetime(d: dict[str, Any], keys: tuple[str, ...]) -> Optional[datetime]:
    for k in keys:
        v = d.get(k)
        if not v:
            continue
        if isinstance(v, (int, float)):
            try:
                return datetime.utcfromtimestamp(int(v) / (1000 if v > 1e11 else 1))
            except (TypeError, ValueError, OSError):
                continue
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
