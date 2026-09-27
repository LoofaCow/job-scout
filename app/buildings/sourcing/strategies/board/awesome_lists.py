"""
Awesome-lists strategy — parses curated GitHub README files for board links.

GitHub "awesome lists" are community-maintained markdown documents that
catalog tools/sites/resources for a topic. For job hunting, they're a
gold mine: each list has dozens of pre-vetted board URLs alongside many
unrelated links (articles, books, podcasts, tools).

This strategy:
    1. Fetches the raw markdown of each seed list
    2. Walks the document section by section, only yielding links from
       sections classified as job-board content
    3. Watches for links to OTHER awesome lists and recursively fetches
       them once (one level deep, no cycles), since the awesome-X meta
       pattern means good lists often reference other good lists.
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


# ============================================================================
# Seed list catalog
# ============================================================================

SEED_LISTS: list[dict[str, str]] = [
    # Verified working seeds. Each one tested manually and confirmed to have
    # heading-classified job-board sections that produce real candidates.
    {
        "name": "lukasz-madon/awesome-remote-job",
        "raw_url": (
            "https://raw.githubusercontent.com/"
            "lukasz-madon/awesome-remote-job/master/README.md"
        ),
        "context": "Curated list of remote-friendly job boards",
    },
    {
        "name": "yanirs/established-remote",
        "raw_url": (
            "https://raw.githubusercontent.com/"
            "yanirs/established-remote/master/README.md"
        ),
        "context": "Established remote-first companies and where to find them",
    },
    # Recursion (depth=2) will discover lists these lists link to. So we don't
    # need to manually maintain a huge top-level seed catalog — the awesome-X
    # meta-pattern means good lists reference other good lists. Two strong
    # seeds + recursion outperforms a long list of mediocre seeds.
]


# ============================================================================
# Patterns
# ============================================================================


# Markdown link pattern: [text](url) — captures both groups.
# (?<!\!) skips image refs ![alt](url).
LINK_PATTERN = re.compile(r"(?<!\!)\[([^\]]+)\]\((https?://[^\s)]+)\)")

# Heading detector
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
    "job opportunities",
    "employment",
    "careers",
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
    "library",
    "extension",
    "twitter account",
    "youtube channel",
    "guide",
    "framework",
    "case study",
    "interview",
    "research",
    "study",
    "license",
    "contributing",
    "table of contents",
)


# Detect awesome-list URLs for recursion. Looks for github.com/.../awesome-*
# or links that reference raw.githubusercontent.com README files.
AWESOME_LIST_PATTERN = re.compile(
    r"https?://(?:raw\.)?github(?:usercontent)?\.com/[^/]+/awesome[-_a-z0-9]*",
    re.IGNORECASE,
)


# Maximum recursion depth for following awesome-list links to other lists.
# Depth 1 = the seeds themselves; depth 2 = lists they link to. We stop at 2.
MAX_RECURSION_DEPTH = 2


# ============================================================================
# Strategy
# ============================================================================


class AwesomeListsStrategy:
    name = "awesome_lists"
    target_pipeline = SourcePipeline.CAREER

    async def discover(
        self,
        fetcher: "PoliteFetcher",
    ) -> AsyncIterator[SourceCandidate | GigCandidate]:
        """Walk seeds + transitively-linked lists, yield board candidates."""
        visited_lists: set[str] = set()
        # Queue of (raw_url, name, context, depth) tuples.
        queue: list[tuple[str, str, str, int]] = [
            (s["raw_url"], s["name"], s["context"], 1)
            for s in SEED_LISTS
        ]

        seen_urls: set[str] = set()
        total_yielded = 0

        while queue:
            raw_url, list_name, context, depth = queue.pop(0)

            if raw_url in visited_lists:
                continue
            visited_lists.add(raw_url)

            logger.info(
                f"awesome_lists: fetching {list_name} (depth {depth})"
            )
            response = await fetcher.get(raw_url, respect_robots=False)
            if response is None or response.status_code != 200:
                status = response.status_code if response else "none"
                logger.warning(
                    f"awesome_lists: failed to fetch {list_name} "
                    f"(status={status})"
                )
                continue

            markdown = response.text
            yielded_for_list = 0

            for heading, link_text, link_url in _walk_sections(markdown):
                if link_url in seen_urls:
                    continue
                seen_urls.add(link_url)

                # If this link points at another awesome list, queue it for
                # one-level-deep recursion (regardless of section).
                if (
                    depth < MAX_RECURSION_DEPTH
                    and AWESOME_LIST_PATTERN.search(link_url)
                ):
                    raw_variant = _to_raw_github_url(link_url)
                    if raw_variant and raw_variant not in visited_lists:
                        queue.append((
                            raw_variant,
                            f"(linked from {list_name}) {link_text}",
                            f"Recursively-discovered list. {context}",
                            depth + 1,
                        ))

                # Standard board candidate (only if it passes plausibility)
                if not is_plausible_board_url(link_url):
                    continue

                yield SourceCandidate(
                    url=link_url,
                    name=link_text,
                    suggested_type=SourceType.STRUCTURED_BOARD,
                    target_pipeline=SourcePipeline.CAREER,
                    discovery_context=(
                        f"Listed in {list_name} under '{heading}' "
                        f"as '{link_text}'. {context}."
                    ),
                    raw_evidence={
                        "seed_list": list_name,
                        "section": heading,
                        "link_text": link_text,
                        "depth": depth,
                    },
                )
                yielded_for_list += 1

            total_yielded += yielded_for_list
            logger.info(
                f"awesome_lists: yielded {yielded_for_list} candidates "
                f"from {list_name} (depth {depth})"
            )

        logger.info(
            f"awesome_lists: total {total_yielded} candidates from "
            f"{len(visited_lists)} lists"
        )


# ============================================================================
# Markdown section walker
# ============================================================================


def _walk_sections(markdown: str) -> Iterator[tuple[str, str, str]]:
    """
    Walk markdown line by line, tracking the current heading.
    Yields (heading_text, link_text, link_url) for every link inside
    a section classified as job-board-relevant.
    """
    current_heading = ""
    in_target_section = False

    for line in markdown.splitlines():
        heading_match = HEADING_PATTERN.match(line)
        if heading_match is not None:
            current_heading = heading_match.group(2).strip()
            in_target_section = _is_job_board_section(current_heading)
            continue

        if not in_target_section:
            continue

        for match in LINK_PATTERN.finditer(line):
            link_text = match.group(1).strip()
            link_url = match.group(2).strip().rstrip(".,)")
            yield (current_heading, link_text, link_url)


def _is_job_board_section(heading: str) -> bool:
    """Accept if any positive keyword matches AND no negative keyword matches."""
    h = heading.lower()
    if any(neg in h for neg in NON_JOB_SECTION_KEYWORDS):
        return False
    if any(pos in h for pos in JOB_SECTION_KEYWORDS):
        return True
    return False


# ============================================================================
# GitHub URL helpers
# ============================================================================


def _to_raw_github_url(github_url: str) -> str | None:
    """
    Convert a github.com/user/repo URL to its raw README markdown URL.
    Returns None if the URL doesn't look convertible.

    Examples:
        https://github.com/user/awesome-x
            -> https://raw.githubusercontent.com/user/awesome-x/main/README.md
        https://github.com/user/awesome-x/blob/main/README.md
            -> https://raw.githubusercontent.com/user/awesome-x/main/README.md
    """
    # Already a raw URL
    if "raw.githubusercontent.com" in github_url:
        return github_url

    # Strip github.com prefix
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/?#]+)(?:/tree/([^/?#]+))?/?$",
        github_url,
    )
    if m:
        user, repo, branch = m.group(1), m.group(2), m.group(3) or "main"
        return (
            f"https://raw.githubusercontent.com/"
            f"{user}/{repo}/{branch}/README.md"
        )

    # github.com/user/repo/blob/branch/path/to/README.md
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
        github_url,
    )
    if m:
        user, repo, branch, path = m.groups()
        return (
            f"https://raw.githubusercontent.com/"
            f"{user}/{repo}/{branch}/{path}"
        )

    return None