"""
Directory-crawl strategy — extracts job-board URLs from curated directory pages.

Some pages on the web exist specifically to catalog job boards: listicles,
"best of" articles, niche industry hubs, community wiki pages. This strategy
fetches them, regex-extracts <a href="..."> values, runs the shared
plausibility filter, and yields candidates.

Wikipedia was the original seed but produced too many news/citation links and
no real job boards. The replacements below are pages that explicitly link to
job boards by intent.
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


# Seed directory pages. Curated pages whose explicit purpose is to link
# at job boards. We swap content occasionally as listicles age.
SEED_DIRECTORIES: list[dict[str, str]] = [
    {
        "name": "Arc.dev — Best Remote Job Boards",
        "url": "https://arc.dev/employer-blog/best-remote-job-boards/",
        "context": "Listicle of remote-friendly job boards",
    },
    {
        "name": "FlexJobs — Top Remote Job Sites",
        "url": "https://www.flexjobs.com/blog/post/top-50-best-remote-jobs-companies/",
        "context": "Curated list of remote-job sites",
    },
    {
        "name": "Working Nomads — Remote Job Boards Directory",
        "url": "https://www.workingnomads.com/blog/best-remote-job-boards/",
        "context": "Working Nomads' directory of remote job boards",
    },
    {
        "name": "Built In — Tech Job Boards",
        "url": "https://builtin.com/articles/best-tech-job-boards",
        "context": "Built In's roundup of tech-focused job boards",
    },
    {
        "name": "DEV Community — Niche Tech Job Boards",
        "url": "https://dev.to/llabusch/9-niche-tech-job-boards-you-should-know-1c8d",
        "context": "DEV Community post listing niche tech boards",
    },
    {
        "name": "Indie Hackers — Job Boards",
        "url": "https://www.indiehackers.com/post/list-of-job-boards-2cfb09cdb1",
        "context": "Indie Hackers community list of job boards",
    },
    {
        "name": "Hacker News — Where Are You Hiring? threads",
        "url": "https://hn.algolia.com/?q=Ask+HN+Who+is+hiring",
        "context": "HN search for monthly hiring threads",
    },
    {
        "name": "ProductHunt — Job Boards Collection",
        "url": "https://www.producthunt.com/topics/jobs",
        "context": "Product Hunt listings tagged 'jobs'",
    },
]


# Captures href values from <a> tags. Single or double quotes, absolute URLs.
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
        total_yielded = 0
        seen_urls: set[str] = set()

        for seed in SEED_DIRECTORIES:
            logger.info(f"directory_crawl: fetching {seed['name']}")
            response = await fetcher.get(seed["url"])

            if response is None or response.status_code != 200:
                status = response.status_code if response else "none"
                logger.warning(
                    f"directory_crawl: failed to fetch {seed['name']} "
                    f"(status={status})"
                )
                continue

            html = response.text
            yielded = 0

            for match in HREF_PATTERN.finditer(html):
                link_url = match.group(1).strip().rstrip(".,)")

                if link_url in seen_urls:
                    continue
                seen_urls.add(link_url)

                if not is_plausible_board_url(link_url):
                    continue

                yield SourceCandidate(
                    url=link_url,
                    name=None,
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

            total_yielded += yielded
            logger.info(
                f"directory_crawl: yielded {yielded} candidates from "
                f"{seed['name']}"
            )

        logger.info(f"directory_crawl: total {total_yielded} candidates")