"""
Search-rotation strategy — discovers boards via self-hosted SearXNG.

How it works:
    1. A curated SEED_QUERIES list defines what we want to find.
    2. Each run picks a random subset of QUERIES_PER_RUN queries.
    3. Each query is sent to our local SearXNG instance, which queries many
       upstream search engines (Google, Bing, DuckDuckGo, Brave, Mojeek...)
       and aggregates the results.
    4. JSON results are parsed; result URLs become candidates.

Why self-hosted:
    Privacy stays local (only the upstream engines and our SearXNG see the
    queries). SearXNG natively rotates between many engines so no single
    one sees all our traffic, and at our query volume (~25/run, weekly)
    no engine will block us. Default URL points at the Podman container
    on localhost:8080 — see searxng-config/settings.yml for the SearXNG
    config.
"""

from __future__ import annotations

import json
import logging
import random
from typing import TYPE_CHECKING, Any, AsyncIterator
from urllib.parse import quote_plus

from app.buildings.sourcing.models import SourcePipeline, SourceType
from app.buildings.sourcing.strategies.base import GigCandidate, SourceCandidate
from app.buildings.sourcing.strategies.board._link_filter import is_plausible_board_url
from app.config import settings

if TYPE_CHECKING:
    from app.buildings.sourcing.http_client import PoliteFetcher

logger = logging.getLogger(__name__)


QUERIES_PER_RUN = 5            # how many queries to sample from SEED_QUERIES per run
MAX_QUERY_RETRIES = 2          # tolerate transient failures (engine timeouts) per query
RESULTS_PER_QUERY_CAP = 15     # don't yield more than this many results per query


# Curated query list. Each run samples QUERIES_PER_RUN of these.
# Categories: generic / role-specific / industry / geography / niche.
# Add freely — bigger list = better natural rotation over time.
SEED_QUERIES: list[dict[str, str]] = [
    # Generic
    {"query": "list of niche job boards", "context": "general directory listing"},
    {"query": "specialty job boards", "context": "specialized sites"},
    {"query": "remote-only job board", "context": "remote-first sites"},
    {"query": "obscure job boards", "context": "anomaly hunting"},

    # IT / DevOps / SWE roles
    {"query": "DevOps job board", "context": "DevOps-specific"},
    {"query": "system administrator job board", "context": "sysadmin-specific"},
    {"query": "IT help desk job board", "context": "support roles"},
    {"query": "automation engineer jobs site", "context": "automation roles"},
    {"query": "Python developer job board", "context": "language-specific"},
    {"query": "Linux jobs board", "context": "linux-specific"},

    # Industry
    {"query": "biotech career site", "context": "biotech industry"},
    {"query": "academic job board", "context": "higher ed jobs"},
    {"query": "government IT jobs board", "context": "public sector"},
    {"query": "non-profit job board", "context": "non-profit sector"},
    {"query": "cybersecurity careers board", "context": "security industry"},
    {"query": "AI engineer jobs board", "context": "AI/ML industry"},
    {"query": "fintech jobs board", "context": "fintech industry"},

    # Geography (Trevor's locations of interest)
    {"query": "Iowa tech jobs board", "context": "local"},
    {"query": "Midwest software engineering jobs", "context": "regional"},
    {"query": "Austin Texas tech job board", "context": "relocation target"},
    {"query": "Denver Colorado tech jobs", "context": "relocation target"},
    {"query": "Raleigh tech jobs board", "context": "relocation target"},
    {"query": "Minneapolis tech jobs", "context": "relocation target"},

    # Niche / anomaly
    {"query": "freelance contract job board", "context": "contractor work"},
    {"query": "hardware engineering jobs board", "context": "physical/embedded"},
    {"query": "bilingual job board", "context": "language-specific"},
]


class SearchRotationStrategy:
    name = "search_rotation"
    target_pipeline = SourcePipeline.CAREER

    async def discover(
        self,
        fetcher: "PoliteFetcher",
    ) -> AsyncIterator[SourceCandidate | GigCandidate]:
        """Sample queries, search via local SearXNG, yield result URLs as candidates."""
        sample_size = min(QUERIES_PER_RUN, len(SEED_QUERIES))
        sampled = random.sample(SEED_QUERIES, sample_size)
        logger.info(
            f"search_rotation: sampled {len(sampled)}/{len(SEED_QUERIES)} queries this run"
        )

        seen_urls: set[str] = set()
        total_yielded = 0

        for seed in sampled:
            query = seed["query"]
            context = seed["context"]
            logger.info(f"search_rotation: searching {query!r}")

            results = await self._search_with_retry(fetcher, query)
            if not results:
                logger.info(f"search_rotation: {query!r} -> 0 results after retries")
                continue

            yielded_for_query = 0
            for result in results[:RESULTS_PER_QUERY_CAP]:
                url = result.get("url")
                title = result.get("title")
                if not url or not isinstance(url, str):
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                if not is_plausible_board_url(url):
                    continue

                yield SourceCandidate(
                    url=url,
                    name=title if isinstance(title, str) else None,
                    suggested_type=SourceType.STRUCTURED_BOARD,
                    target_pipeline=SourcePipeline.CAREER,
                    discovery_context=(
                        f"Found via search query {query!r} ({context}). "
                        f"Search engine result."
                    ),
                    raw_evidence={
                        "query": query,
                        "context": context,
                        "result_title": title,
                    },
                )
                yielded_for_query += 1

            total_yielded += yielded_for_query
            logger.info(
                f"search_rotation: {query!r} -> {yielded_for_query} candidates"
            )

        logger.info(f"search_rotation: total {total_yielded} candidates this run")

    # ========================================================================
    # Internals
    # ========================================================================

    async def _search_with_retry(
        self,
        fetcher: "PoliteFetcher",
        query: str,
    ) -> list[dict[str, Any]]:
        """
        Run one search through the local SearXNG instance.

        Self-hosted SearXNG is reliable and fast; the retry budget here is
        only for transient failures (one of SearXNG's upstream engines
        timing out, network blip, container hiccup).

        Returns the JSON 'results' array, or empty list on total failure.
        """
        encoded = quote_plus(query)
        url = f"{settings.SEARXNG_BASE_URL.rstrip('/')}/search?q={encoded}&format=json"

        for attempt in range(1, MAX_QUERY_RETRIES + 1):
            response = await fetcher.get(url, respect_robots=False)
            # respect_robots=False because localhost has no robots.txt and
            # this is our own instance — politeness doesn't apply to ourselves.

            if response is None:
                logger.debug(
                    f"search_rotation: attempt {attempt} got no response for {query!r}"
                )
                continue

            if response.status_code != 200:
                logger.debug(
                    f"search_rotation: attempt {attempt} got HTTP "
                    f"{response.status_code} for {query!r}"
                )
                continue

            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                logger.warning(
                    f"search_rotation: SearXNG returned non-JSON for {query!r}; "
                    f"check that 'json' is in settings.yml formats"
                )
                continue

            results = payload.get("results", [])
            if not isinstance(results, list):
                logger.debug(
                    f"search_rotation: malformed JSON for {query!r}"
                )
                continue

            return results

        return []