"""
Awesome-lists strategy — parses curated GitHub README files for board links.

GitHub "awesome lists" are community-maintained markdown documents that
catalog tools/sites/resources for a topic. For job hunting, they're a
gold mine: each list has dozens of pre-vetted board URLs alongside many
unrelated links (articles, books, podcasts, tools).

This strategy fetches the raw markdown of seed lists, walks the document
section by section, and only yields links from sections whose heading
indicates job-board content. Non-board sections (Articles, Books, etc.)
are skipped entirely. The verifier handles edge-case judgment within the
yielded candidates.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, AsyncIterator, Iterator

from app.buildings.sourcing.models import SourcePipeline, SourceType
from app.buildings.sourcing.strategies.base import GigCandidate, SourceCandidate
from app.buildings.sourcing.strategies.board._link_filter import is_plausible_board_url

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


# Markdown link pattern: [text](url) — captures both groups.
# The (?<!\!) negative lookbehind skips image refs ![alt](url).
LINK_PATTERN = re.compile(r"(?<!\!)\[([^\]]+)\]\((https?://[^\s)]+)\)")

# Heading detector: matches "## Heading", "### Heading", etc.
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


# Strong positive signals for job-board sections.
JOB_SECTION_KEYWORDS = (
    "job board",
    "job site",
    "job listing",
    "job platform",
    "remote job",
    "remote work site",
    "where to find",
    "find a job",
    "find work",
    "find remote",
    "hiring",
    "job hunting",
    "career site",
    "remote-first",
    "list of companies",
    "companies hiring",
    "places to find",
)


# Strong negative signals — even if a job keyword matches, skip these.
NON_JOB_SECTION_KEYWORDS = (
    "article",
    "blog",
    "book",
    "podcast",
    "video",
    "course",
    "tutorial",
    "newsletter",
    "talk",
    "speaker",
    "tool",
    "software",
    "library",
    "extension",
    "newsletter",
    "twitter account",
    "youtube channel",
    "guide",
    "resource",
    "framework",
    "case study",
    "interview",
    "research",
    "study",
)



class AwesomeListsStrategy:
    name = "awesome_lists"
    target_pipeline = SourcePipeline.CAREER

    async def discover(
        self,
        fetcher: "PoliteFetcher",
    ) -> AsyncIterator[SourceCandidate | GigCandidate]:
        """Fetch each seed list, walk by section, yield board-section links."""
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

            for heading, link_text, link_url in _walk_sections(markdown):
                # In-list dedup; the hunter dedups across strategies separately
                if link_url in seen_urls:
                    continue
                seen_urls.add(link_url)

                if not is_plausible_board_url(link_url):
                    continue

                yield SourceCandidate(
                    url=link_url,
                    name=link_text,
                    suggested_type=SourceType.STRUCTURED_BOARD,
                    target_pipeline=SourcePipeline.CAREER,
                    discovery_context=(
                        f"Listed in {seed['name']} under section '{heading}' "
                        f"as '{link_text}'. {seed['context']}."
                    ),
                    raw_evidence={
                        "seed_list": seed["name"],
                        "section": heading,
                        "link_text": link_text,
                    },
                )
                yielded += 1

            logger.info(
                f"awesome_lists: yielded {yielded} candidates from {seed['name']}"
            )


# ============================================================================
# Markdown section walker
# ============================================================================


def _walk_sections(markdown: str) -> Iterator[tuple[str, str, str]]:
    """
    Walk the markdown line by line, tracking the current heading.
    Yields (heading_text, link_text, link_url) for every link that appears
    inside a section classified as job-board-relevant.
    """
    current_heading = ""
    in_target_section = False

    for line in markdown.splitlines():
        # Update section context if this line is a heading
        heading_match = HEADING_PATTERN.match(line)
        if heading_match is not None:
            current_heading = heading_match.group(2).strip()
            in_target_section = _is_job_board_section(current_heading)
            continue

        if not in_target_section:
            continue

        # Extract every [text](url) from this line
        for match in LINK_PATTERN.finditer(line):
            link_text = match.group(1).strip()
            link_url = match.group(2).strip().rstrip(".,)")
            yield (current_heading, link_text, link_url)


def _is_job_board_section(heading: str) -> bool:
    """
    Classify a heading as job-board-relevant.

    Accept if any positive keyword matches AND no negative keyword matches.
    The order matters: a heading like 'Articles about job boards' should
    be rejected because it's an article section, not a board section.
    """
    h = heading.lower()

    if any(neg in h for neg in NON_JOB_SECTION_KEYWORDS):
        return False

    if any(pos in h for pos in JOB_SECTION_KEYWORDS):
        return True

    return False
