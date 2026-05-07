"""
Scraper protocol — every scraper plugin implements this interface.

Scrapers are intentionally dumb. Their only job is to fetch a job board
and return Job objects with whatever fields they could extract. No scoring,
no LLM calls, no filtering beyond obvious junk.
"""

from typing import Protocol

from app.spine.storage import Job


class Scraper(Protocol):
    """
    A scraper plugin. Identified by `source_name`, which must be unique
    across all scrapers and must match the names listed in
    Profile.enabled_sources.
    """

    source_name: str

    async def fetch(self) -> list[Job]:
        """
        Fetch the board and return a list of Job objects.

        The Job objects should be partially populated — at minimum:
            - source (set to self.source_name)
            - source_job_id (the board's own unique ID for the listing)
            - title, company, location, url, description

        Optional fields (salary, is_remote, is_hybrid, posted_at) should be
        populated when the source provides them, left None otherwise.

        The scraper should NOT set:
            - id (DB will assign)
            - first_seen_at (storage layer sets it on insert)
            - status, notes, evaluations (those are application/scoring concerns)

        Should raise on hard failures (network down, board returned 500).
        Should NOT raise on partial failures (one listing has weird HTML);
        log and skip the bad listing instead.
        """
        ...