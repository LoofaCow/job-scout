"""
Source verifier — confirms candidate URLs are real job sources.

Given a SourceCandidate, the verifier:
    1. Fetches the URL with the polite fetcher
    2. Truncates the page content
    3. Asks the local LLM (via the model router) whether this is a real
       source, what kind, what its quality is, and any structured hints
       the eventual scraper will need (RSS feed, API endpoint)

The hunter calls verify_source_candidate() once per candidate. Verification
is the expensive step — one LLM call each — so hunters cap candidate counts
before reaching here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from agno.agent import Agent
from pydantic import BaseModel, Field

from app.buildings.sourcing.models import SourceType
from app.buildings.sourcing.strategies.base import SourceCandidate
from app.spine.models import Tier, get_model

if TYPE_CHECKING:
    from app.buildings.sourcing.http_client import PoliteFetcher

logger = logging.getLogger(__name__)


# Truncate fetched HTML to this many characters before showing it to the LLM.
# Local 7B models have 32K context; we leave headroom for prompt + output.
PAGE_SNIPPET_CHARS = 6000


# ============================================================================
# Structured output — what the verifier LLM must return
# ============================================================================


class VerificationResult(BaseModel):
    """The verifier's structured judgment on a candidate."""

    is_real_source: bool = Field(
        description=(
            "True if this URL is a real job board, careers page, or otherwise "
            "produces ongoing job listings. False if it's a parked domain, "
            "single old listing, login wall, dead site, or unrelated content."
        )
    )
    quality_score: int = Field(
        ge=0,
        le=100,
        description=(
            "0-100 quality assessment. 0-30 = low signal/dying/junk. "
            "31-60 = niche but viable. 61-85 = solid mainstream board. "
            "86-100 = top-tier active source. Use 0 if not a real source."
        ),
    )
    suggested_name: Optional[str] = Field(
        default=None,
        description="Cleaned-up display name for the source.",
    )
    suggested_source_type: Optional[SourceType] = Field(
        default=SourceType.STRUCTURED_BOARD,
        description=(
            "What kind of source this is. STRUCTURED_BOARD for normal HTML "
            "boards, JSON_API if the page exposes one, RSS_FEED if it's "
            "primarily an RSS feed, OTHER if it doesn't fit the categories."
        ),
    )
    rss_feed_url: Optional[str] = Field(
        default=None,
        description="URL of an RSS/Atom feed if you noticed one on the page.",
    )
    api_endpoint_url: Optional[str] = Field(
        default=None,
        description="URL of a JSON/REST API for the listings, if visible.",
    )
    rationale: str = Field(
        description=(
            "2-3 plain-English sentences explaining the score and decision. "
            "Reference what you actually saw on the page."
        )
    )
    rejection_reason: Optional[str] = Field(
        default=None,
        description=(
            "If is_real_source is false, give a short tag like 'parked_domain', "
            "'404', 'dead_listings', 'wrong_kind_of_site'. Null otherwise."
        )
    )


# ============================================================================
# Public API
# ============================================================================


async def verify_source_candidate(
    candidate: SourceCandidate,
    fetcher: "PoliteFetcher",
) -> VerificationResult:
    """
    Verify a single candidate. Always returns a VerificationResult — never
    raises. Network/fetch failures become is_real_source=False with an
    appropriate rejection_reason.
    """
    # === Fetch ===
    if not await fetcher.can_fetch(candidate.url):
        logger.info(f"verify: robots.txt disallows {candidate.url}")
        return _failed_result(
            "robots_disallowed",
            f"robots.txt forbids fetching {candidate.url}",
        )

    response = await fetcher.get(candidate.url)
    if response is None:
        return _failed_result("fetch_failed", "Network/HTTP error during fetch")
    if response.status_code == 404:
        return _failed_result("404", f"Got 404 fetching {candidate.url}")
    if response.status_code >= 400:
        return _failed_result(
            "http_error",
            f"Got HTTP {response.status_code} fetching {candidate.url}",
        )

    snippet = response.text[:PAGE_SNIPPET_CHARS]

    # === LLM call ===
    agent = _build_verifier_agent()
    prompt = _format_verification_prompt(candidate, snippet, response.url)

    try:
        result = await agent.arun(prompt)
    except Exception as e:
        logger.warning(f"verify: LLM call failed for {candidate.url}: {e}")
        return _failed_result("llm_error", f"LLM verification call failed: {e}")

    if not isinstance(result.content, VerificationResult):
        logger.warning(
            f"verify: unexpected content type for {candidate.url}: "
            f"{type(result.content).__name__}"
        )
        return _failed_result(
            "schema_violation",
            f"LLM returned non-conforming output: {type(result.content).__name__}",
        )

    return result.content


# ============================================================================
# Internals
# ============================================================================


def _build_verifier_agent() -> Agent:
    """Construct a fresh verifier agent with the local-tier model."""
    return Agent(
        model=get_model(Tier.LOCAL),
        instructions=_VERIFIER_INSTRUCTIONS,
        output_schema=VerificationResult,
        use_json_mode=True,
    )


_VERIFIER_INSTRUCTIONS = """\
You are a source-verification agent. You receive a URL and a snippet of the
page found at that URL. Your job is to judge whether this URL is a real,
ongoing source of job listings worth scraping repeatedly.

## What counts as a real source

- Job boards with current listings (RemoteOK, We Work Remotely, etc.)
- Companies' /careers or /jobs pages with live openings listed
- Niche/specialty boards (academic-jobs, design-jobs, gov boards, etc.)
- Job-listing RSS feeds and JSON APIs

## What does NOT count

- Parked domains, "for sale" pages, expired/dead sites
- Single individual job postings (those are jobs, not sources)
- Articles ABOUT job hunting that don't host listings
- Login walls with no public listings visible
- Marketing pages with no actual postings
- Generic company homepages with no careers section

## How to score quality (0-100)

- 0-30: Dying, near-empty, low-signal, very niche or low-quality listings
- 31-60: Niche but viable; postings exist but volume is small
- 61-85: Solid mainstream board with regular fresh listings
- 86-100: Top-tier active source with high posting volume and quality

Set quality_score to 0 if is_real_source is false.

## Output rules

- Be honest. We'd rather skip a marginal source than waste scraper effort on it.
- If you see an RSS feed link in the page (look for `<link rel="alternate" type="application/rss+xml">`), put its URL in rss_feed_url.
- If the page references a JSON API endpoint (e.g. `/api/jobs.json`, `/api/v1/listings`), put it in api_endpoint_url.
- rationale should be 2-3 sentences referencing specific evidence from the snippet.
- rejection_reason: short snake_case tag when is_real_source=false, otherwise null.
"""


def _format_verification_prompt(
    candidate: SourceCandidate,
    snippet: str,
    final_url: object,  # httpx.URL but typed loosely to avoid an import
) -> str:
    return f"""\
Verify this candidate source.

Discovery context: {candidate.discovery_context}
Candidate URL (as discovered): {candidate.url}
Final URL after redirects: {final_url}
Suggested name from discovery: {candidate.name or "(none)"}

Page snippet (first {PAGE_SNIPPET_CHARS} chars of HTML):
---
{snippet}
---

Decide whether this is a real, ongoing job source and fill out the schema.
"""


def _failed_result(reason: str, rationale: str) -> VerificationResult:
    """Build a 'not a real source' result with a structured rejection reason."""
    return VerificationResult(
        is_real_source=False,
        quality_score=0,
        suggested_name=None,
        suggested_source_type=SourceType.OTHER,
        rss_feed_url=None,
        api_endpoint_url=None,
        rationale=rationale,
        rejection_reason=reason,
    )