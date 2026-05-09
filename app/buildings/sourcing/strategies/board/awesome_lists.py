"""
Awesome-lists strategy — parses curated GitHub README files for board links.

GitHub "awesome lists" are community-maintained markdown documents that
catalog tools/sites/resources for a topic. For job hunting, they're a
gold mine: each list has dozens of pre-vetted board URLs.

This strategy fetches the raw markdown of seed lists, extracts the
[name](url) link references, filters obvious non-source links (GitHub,
Twitter, donate pages), and emits each remaining link as a candidate.
The verifier handles the actual is-this-a-real-board judgment.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, AsyncIterator
from urllib.parse import urlparse

from app.buildings.sourcing.models import SourcePipeline, SourceType
from app.buildings.sourcing.strategies.base import GigCandidate, SourceCandidate

if TYPE_CHECKING:
    from app.buildings.sourcing.http_client import PoliteFetcher

logger = logging.getLogger(__name__)


# Seed lists. Add more entries to broaden coverage. raw_url must point at
# raw.githubusercontent.com so we get plain markdown without HTML rendering.
SEED_LISTS: list[dict[str, str]] = [
    {
        "name": "lukasz-madon/awesome-remote-job",
        "raw_url": (
            "https://raw.githubusercontent.com/"
            "lukasz-madon/awesome-remote-job/master/README.md"
        ),
        "context": "Curated list of remote-friendly job boards",
    },
]


# Markdown link pattern: [text](url) — captures both groups
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


# Domains we skip — these are link-list infrastructure, not job sources
SKIP_DOMAINS: set[str] = {
    "github.com",
    "raw.githubusercontent.com",
    "gist.github.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "facebook.com",
    "youtube.com",
    "reddit.com",
    "patreon.com",
    "ko-fi.com",
    "buymeacoffee.com",
    "paypal.com",
    "wikipedia.org",
}


class AwesomeListsStrategy:
    name = "awesome_lists"
    target_pipeline = SourcePipeline.CAREER

    async def discover(
        self,
        fetcher: "PoliteFetcher",
    ) -> AsyncIterator[SourceCandidate | GigCandidate]:
        """Fetch each seed list, parse links, yield candidates."""
        for seed in SEED_LISTS:
            logger.info(f"awesome_lists: fetching {seed['name']}")
            response = await fetcher.get(seed["raw_url"], respect_robots=False)
            # respect_robots=False because raw.githubusercontent.com has no
            # public robots.txt for this path and the file is explicitly public.

            if response is None or response.status_code != 200:
                logger.warning(
                    f"awesome_lists: failed to fetch {seed['name']} "
                    f"(status={response.status_code if response else 'none'})"
                )
                continue

            markdown = response.text
            yielded = 0
            seen_urls: set[str] = set()

            for match in LINK_PATTERN.finditer(markdown):
                link_text = match.group(1).strip()
                link_url = match.group(2).strip().rstrip(".,)")  # trim trailing punct

                # In-list dedup; the hunter dedups across strategies separately
                if link_url in seen_urls:
                    continue
                seen_urls.add(link_url)

                if not _is_plausible_board_url(link_url):
                    continue

                yield SourceCandidate(
                    url=link_url,
                    name=link_text,
                    suggested_type=SourceType.STRUCTURED_BOARD,
                    target_pipeline=SourcePipeline.CAREER,
                    discovery_context=(
                        f"Listed in {seed['name']} as '{link_text}'. "
                        f"{seed['context']}."
                    ),
                    raw_evidence={
                        "seed_list": seed["name"],
                        "link_text": link_text,
                    },
                )
                yielded += 1

            logger.info(f"awesome_lists: yielded {yielded} candidates from {seed['name']}")


def _is_plausible_board_url(url: str) -> bool:
    """Cheap pre-filter — drops obvious non-boards before verifier sees them."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False

    domain = parsed.netloc.lower()
    # Strip leading "www." for matching
    if domain.startswith("www."):
        domain = domain[4:]

    # Drop infrastructure links
    for skip in SKIP_DOMAINS:
        if domain == skip or domain.endswith("." + skip):
            return False

    # Drop file downloads and image links
    path = parsed.path.lower()
    if path.endswith((".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".zip")):
        return False

    return True