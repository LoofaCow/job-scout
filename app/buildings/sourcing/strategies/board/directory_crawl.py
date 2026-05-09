"""
Directory-crawl strategy — extracts job-board URLs from curated directory pages.

Some pages on the web exist specifically to catalog job boards: Wikipedia
articles, list-of-X pages on tech sites, "best job boards 20XX" listicles.
This strategy fetches them, regex-extracts <a href="..."> values, runs the
shared plausibility filter, and yields candidates.

Regex (not bs4) is intentional: we only need the href value itself, the
verifier handles the rest, and we don't want to add a parsing dep yet.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, AsyncIterator

from app.buildings.sourcing.models import SourcePipeline, SourceType
from app.buildings.sourcing.strategies.base import GigCandidate, SourceCandidate
from app.buildings.sourcing.strategies.board._link_filter import is_plausible_board_url

if TYPE_CHECKING:
    from app.buildings.sourcing.http_client import PoliteFetcher

logger = logging.getLogger(__name__)


# Seed directory pages. Each entry is fetched once per run; all <a href>
# values are extracted, filtered, and yielded as candidates.
#
# To add a directory: append a dict here. Same shape, no other changes.
SEED_DIRECTORIES: list[dict[str, str]] = [
    {
        "name": "Wikipedia: Employment website",
        "url": "https://en.wikipedia.org/wiki/Employment_website",
        "context": "Wikipedia article on employment websites; external links section",
    },
]


# Captures href values from <a> tags. Single or double quotes, absolute URLs only.
# Edge cases (href split across lines, exotic quoting) fall through; the plausibility
# filter and verifier handle whatever survives.
HREF_PATTERN = re.compile(
    r'<a[^>]+href=["\'](https?://[^"\']+)["\']',
    re.IGNORECASE,
)


class DirectoryCrawlStrategy:
    name = "directory_crawl"
    target_pipeline = SourcePipeline.CAREER

    async def discover(
        self,
        fetcher: "PoliteFetcher",
    ) -> AsyncIterator[SourceCandidate | GigCandidate]:
        """Fetch each seed directory, extract links, yield candidates."""
        for seed in SEED_DIRECTORIES:
            logger.info(f"directory_crawl: fetching {seed['name']}")
            response = await fetcher.get(seed["url"])

            if response is None or response.status_code != 200:
                status = response.status_code if response else "none"
                logger.warning(
                    f"directory_crawl: failed to fetch {seed['name']} (status={status})"
                )
                continue

            html = response.text
            yielded = 0
            seen_urls: set[str] = set()

            for match in HREF_PATTERN.finditer(html):
                link_url = match.group(1).strip().rstrip(".,)")

                if link_url in seen_urls:
                    continue
                seen_urls.add(link_url)

                if not is_plausible_board_url(link_url):
                    continue

                yield SourceCandidate(
                    url=link_url,
                    name=None,  # we don't try to extract anchor text reliably
                    suggested_type=SourceType.STRUCTURED_BOARD,
                    target_pipeline=SourcePipeline.CAREER,
                    discovery_context=(
                        f"Linked from {seed['name']}. {seed['context']}."
                    ),
                    raw_evidence={
                        "seed_directory": seed["name"],
                        "seed_url": seed["url"],
                    },
                )
                yielded += 1

            logger.info(
                f"directory_crawl: yielded {yielded} candidates from {seed['name']}"
            )