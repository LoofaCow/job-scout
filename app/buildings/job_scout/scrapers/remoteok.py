"""
RemoteOK scraper — fetches jobs from RemoteOK's public JSON API.

API endpoint: https://remoteok.com/api
Rate limit: not formally documented; we set a User-Agent and call once per run.
The first array element is a legal disclaimer; we skip it.
"""

import logging
from datetime import datetime
from typing import Any

import httpx

from app.spine.storage import Job

logger = logging.getLogger(__name__)


class RemoteOKScraper:
    source_name = "remoteok"

    API_URL = "https://remoteok.com/api"
    # RemoteOK blocks generic UA strings; identify ourselves politely
    USER_AGENT = "JobScout/0.1 (personal-job-search-tool)"
    TIMEOUT_SECONDS = 30

    async def fetch(self) -> list[Job]:
        """Hit the API, parse, return Job objects. Skips listings we can't parse."""
        async with httpx.AsyncClient(
            timeout=self.TIMEOUT_SECONDS,
            headers={"User-Agent": self.USER_AGENT},
        ) as client:
            response = await client.get(self.API_URL)
            response.raise_for_status()
            raw_data = response.json()

        # Element 0 is a legal disclaimer dict, not a job. Skip it.
        if raw_data and isinstance(raw_data[0], dict) and "legal" in raw_data[0]:
            raw_listings = raw_data[1:]
        else:
            raw_listings = raw_data

        logger.info(f"RemoteOK returned {len(raw_listings)} raw listings")

        jobs: list[Job] = []
        for raw in raw_listings:
            try:
                job = self._parse_listing(raw)
                if job is not None:
                    jobs.append(job)
            except Exception as e:
                # Bad data on one listing shouldn't kill the whole scrape
                logger.warning(
                    f"Failed to parse RemoteOK listing {raw.get('id', '?')}: {e}"
                )
                continue

        logger.info(f"RemoteOK parsed {len(jobs)} usable jobs")
        return jobs

    def _parse_listing(self, raw: dict[str, Any]) -> Job | None:
        """Convert one RemoteOK API record into a Job. Returns None to skip."""
        # Required fields — skip the listing if any are missing
        source_job_id = str(raw.get("id", "")).strip()
        title = (raw.get("position") or "").strip()
        company = (raw.get("company") or "").strip()
        url = (raw.get("url") or raw.get("apply_url") or "").strip()

        if not all([source_job_id, title, company, url]):
            return None

        # Optional fields — best-effort extraction
        location = (raw.get("location") or "Remote").strip()
        description = (raw.get("description") or "").strip()

        # RemoteOK returns salary as separate min/max integer fields when known
        salary_min = self._safe_int(raw.get("salary_min"))
        salary_max = self._safe_int(raw.get("salary_max"))
        salary_text = self._format_salary_text(salary_min, salary_max)

        # Posted timestamp — RemoteOK uses ISO 8601 in `date`
        posted_at = self._parse_date(raw.get("date"))

        return Job(
            source=self.source_name,
            source_job_id=source_job_id,
            title=title,
            company=company,
            location=location,
            url=url,
            description=description,
            salary_text=salary_text,
            salary_min_usd=salary_min,
            salary_max_usd=salary_max,
            is_remote=True,  # the whole site is remote jobs
            is_hybrid=False,
            posted_at=posted_at,
        )

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        """Convert to int, return None on any failure."""
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_salary_text(salary_min: int | None, salary_max: int | None) -> str | None:
        """Build a human-readable salary string from the parsed integers."""
        if salary_min and salary_max:
            return f"${salary_min:,} - ${salary_max:,}"
        if salary_min:
            return f"From ${salary_min:,}"
        if salary_max:
            return f"Up to ${salary_max:,}"
        return None

    @staticmethod
    def _parse_date(value: Any) -> datetime | None:
        """Parse ISO 8601 datetime; return None if it doesn't look like one."""
        if not value or not isinstance(value, str):
            return None
        try:
            # RemoteOK sometimes uses "Z" suffix for UTC; convert for fromisoformat
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None