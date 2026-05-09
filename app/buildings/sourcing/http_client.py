"""
Polite HTTP client — the only way the sourcing department fetches the web.

Why this exists:
    Sourcing is going to hit hundreds of sites at scrape time. We must not
    look like a hostile bot. This wrapper enforces:
        - robots.txt compliance (cached per host)
        - per-domain rate limiting (default 5s between requests)
        - honest User-Agent identifying the project
        - sane timeouts and retries on transient failures

Strategies, verifiers, and helpers fetch through this. They do not call
httpx directly.
"""

import asyncio
import logging
import time
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)


# Identify ourselves honestly. Sites can email this if we misbehave.
DEFAULT_USER_AGENT = (
    "JobScout/0.1 (https://github.com/LoofaCow/job-scout) personal-job-search-tool"
)

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RATE_LIMIT_SECONDS = 5.0
ROBOTS_CACHE_TTL_SECONDS = 60 * 60 * 24  # 24h


class RobotsCache:
    """Cache of parsed robots.txt files, keyed by domain."""

    def __init__(self) -> None:
        self._parsers: dict[str, RobotFileParser] = {}
        self._fetched_at: dict[str, float] = {}

    async def get(self, client: httpx.AsyncClient, url: str) -> RobotFileParser:
        """Return a parser for the URL's domain, fetching robots.txt if stale."""
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        now = time.time()

        cached_at = self._fetched_at.get(host_key, 0)
        if (now - cached_at) < ROBOTS_CACHE_TTL_SECONDS and host_key in self._parsers:
            return self._parsers[host_key]

        rp = RobotFileParser()
        rp.set_url(f"{host_key}/robots.txt")

        try:
            response = await client.get(f"{host_key}/robots.txt", timeout=10)
            if response.status_code == 200:
                rp.parse(response.text.splitlines())
            else:
                # Missing robots.txt = no restrictions (per RFC 9309)
                rp.parse([])
        except Exception as e:
            logger.warning(f"Failed to fetch robots.txt for {host_key}: {e}")
            rp.parse([])  # fail open: treat as no restrictions

        self._parsers[host_key] = rp
        self._fetched_at[host_key] = now
        return rp


class PoliteFetcher:
    """
    Polite HTTP fetcher. One instance per sourcing run; carries rate-limit
    state and robots cache across calls.

    Usage:
        fetcher = PoliteFetcher()
        async with fetcher:
            response = await fetcher.get("https://example.com/jobs")
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        default_rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout_seconds
        self._default_rate_limit = default_rate_limit_seconds
        self._client: Optional[httpx.AsyncClient] = None
        self._robots = RobotsCache()
        self._last_request_at: dict[str, float] = {}   # domain -> unix timestamp
        self._domain_rate_limits: dict[str, float] = {}  # domain -> seconds between

    # === Async context manager ===

    async def __aenter__(self) -> "PoliteFetcher":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": self._user_agent},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # === Public API ===

    async def can_fetch(self, url: str) -> bool:
        """robots.txt check. True if our user-agent is allowed to fetch the URL."""
        if self._client is None:
            raise RuntimeError("PoliteFetcher must be used inside `async with`")
        rp = await self._robots.get(self._client, url)
        return rp.can_fetch(self._user_agent, url)

    async def crawl_delay(self, url: str) -> Optional[float]:
        """robots.txt Crawl-delay for our UA, if any."""
        if self._client is None:
            raise RuntimeError("PoliteFetcher must be used inside `async with`")
        rp = await self._robots.get(self._client, url)
        try:
            delay = rp.crawl_delay(self._user_agent)
            if delay is None:
                return None
            return float(delay)
        except Exception:
            return None

    async def get(
        self,
        url: str,
        *,
        respect_robots: bool = True,
        rate_limit_override_seconds: Optional[float] = None,
    ) -> Optional[httpx.Response]:
        """
        Polite GET. Returns None if robots.txt forbids the URL.

        Always rate-limits per domain. If robots.txt declares a Crawl-delay
        higher than our default, we respect that. Caller can pass a manual
        override (e.g. when an API has a documented limit).
        """
        if self._client is None:
            raise RuntimeError("PoliteFetcher must be used inside `async with`")

        if respect_robots and not await self.can_fetch(url):
            logger.info(f"robots.txt disallows {url}; skipping")
            return None

        await self._wait_for_rate_limit(url, rate_limit_override_seconds)

        try:
            response = await self._client.get(url)
            return response
        except httpx.HTTPError as e:
            logger.warning(f"GET {url} failed: {e}")
            return None

    # === Internals ===

    async def _wait_for_rate_limit(
        self,
        url: str,
        override_seconds: Optional[float],
    ) -> None:
        """Sleep just long enough to respect the per-domain rate limit."""
        domain = urlparse(url).netloc.lower()
        now = time.time()

        if override_seconds is not None:
            limit = override_seconds
        elif domain in self._domain_rate_limits:
            limit = self._domain_rate_limits[domain]
        else:
            robots_delay = await self.crawl_delay(url)
            limit = max(robots_delay or 0, self._default_rate_limit)
            self._domain_rate_limits[domain] = limit

        last = self._last_request_at.get(domain, 0)
        elapsed = now - last
        if elapsed < limit:
            await asyncio.sleep(limit - elapsed)

        self._last_request_at[domain] = time.time()