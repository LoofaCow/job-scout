"""
fetch_full_listing — the bread-and-butter research strategy.

Most info gaps Tear Apart flags trace back to one root cause: the source
data is an RSS teaser of 40-60 chars instead of the full job posting.
Fetching the listing URL and extracting the main content fills that gap
in one shot. Pass 7b will add SearXNG-based company-lookup and other
strategies; for v1 this single strategy handles ~90% of the queued rows.

Returns a `FetchResult` with either a clean text body or an error reason.
The workflow translates this into complete_research / fail_research calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import trafilatura

from app.buildings.sourcing.http_client import PoliteFetcher

logger = logging.getLogger(__name__)


# Anything shorter than this after extraction is treated as "extraction failed"
# rather than a real listing body. Most real job posts are at least 300 chars.
MIN_USEFUL_BODY_CHARS = 300

# trafilatura sometimes returns gigabyte-sized strings on aggregator pages
# that mistakenly include their entire archive. Hard cap.
MAX_BODY_CHARS = 30_000


@dataclass
class FetchResult:
    """Outcome of fetch_full_listing for one URL."""
    success: bool
    body: Optional[str] = None
    error: Optional[str] = None
    source_label: str = "fetched_full_page"


async def fetch_full_listing(url: str, fetcher: PoliteFetcher) -> FetchResult:
    """
    Fetch the listing's URL and extract main content.

    The PoliteFetcher handles robots.txt, per-domain rate limiting, and
    polite UA. trafilatura handles the messy work of identifying the
    actual job-listing text inside the HTML noise.
    """
    try:
        if not await fetcher.can_fetch(url):
            return FetchResult(success=False, error="robots_disallowed")
    except Exception as e:
        logger.debug(f"fetch_full_listing: robots check raised for {url}: {e}")
        # Don't bail just because the robots check failed; many sites have
        # broken robots.txt. Treat as allow and let the actual fetch reveal.

    response = await fetcher.get(url)
    if response is None:
        return FetchResult(success=False, error="fetch_returned_none")
    if response.status_code == 404:
        return FetchResult(success=False, error="http_404")
    if response.status_code in (401, 403):
        return FetchResult(success=False, error=f"http_{response.status_code}_blocked")
    if response.status_code >= 400:
        return FetchResult(success=False, error=f"http_{response.status_code}")

    html = response.text
    if not html:
        return FetchResult(success=False, error="empty_response")

    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
        no_fallback=False,
    )

    if not extracted or not extracted.strip():
        return FetchResult(success=False, error="extraction_empty")

    body = extracted.strip()[:MAX_BODY_CHARS]
    if len(body) < MIN_USEFUL_BODY_CHARS:
        return FetchResult(
            success=False,
            error=f"extraction_too_short_{len(body)}chars",
            body=body,
        )

    return FetchResult(success=True, body=body, source_label="fetched_full_page")
