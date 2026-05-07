"""
Trevor's job-hunt profile — the source of truth for what the scout looks for
and how the scorer ranks what it finds.

Edit this file freely as your search evolves. Every agent reads from here.
"""

from pydantic import BaseModel, Field


class Profile(BaseModel):
    # === Identity (used in cover letters / outreach drafts later) ===
    name: str = "Trevor"
    location_city: str = "Dubuque, IA"
    open_to_relocation: bool = True
    relocation_targets: list[str] = Field(
        default_factory=lambda: [
            # Tech-oriented metros you'd consider
            "Austin, TX",
            "Denver, CO",
            "Raleigh, NC",
            "Minneapolis, MN",
            "Madison, WI",
            "Pittsburgh, PA",
        ]
    )

    # === What you're looking for ===
    target_roles: list[str] = Field(
        default_factory=lambda: [
            # Order matters — earlier = stronger interest
            "IT Service Desk",
            "Help Desk Technician",
            "Junior DevOps Engineer",
            "Junior Software Developer",
            "Automation Engineer",
            "Systems Administrator",
        ]
    )

    # === Skills the agent should match against job listings ===
    strong_skills: list[str] = Field(
        default_factory=lambda: [
            "Windows administration",
            "Hardware troubleshooting",
            "Software troubleshooting",
            "Python",
            "PowerShell",
            "Linux",
            "Docker",
            "Home lab / self-hosting",
            "AI tooling (Ollama, n8n)",
        ]
    )
    learning_skills: list[str] = Field(
        default_factory=lambda: [
            # Stuff you're studying but don't claim mastery of
            "Networking (CCNA-track)",
            "AWS / Cloud",
            "Terraform",
            "CompTIA Security+",
        ]
    )

    # === Compensation ===
    salary_floor_usd: int = 50_000   # below this, don't bother
    salary_target_usd: int = 65_000  # what you actually want
    salary_ceiling_irrelevant_above: int = 200_000  # ignore "anomaly" listings

    # === Work arrangement ===
    remote_ok: bool = True
    hybrid_ok: bool = True
    onsite_ok: bool = True  # in Dubuque or relocation targets only

    # === Dealbreakers — auto-reject jobs containing these ===
    dealbreaker_keywords: list[str] = Field(
        default_factory=lambda: [
            "unpaid",
            "commission only",
            "MLM",
            "door-to-door",
            "must own vehicle for company use",
        ]
    )

    # === Companies of interest (boost score if matched) ===
    target_companies_local: list[str] = Field(
        default_factory=lambda: [
            "isolved",
            "Trinity Health",
            "HARDY Industries",
        ]
    )

    # === Job boards to scrape ===
    # We'll wire these up as scraper plugins. Order = priority.
    enabled_sources: list[str] = Field(
        default_factory=lambda: [
            "remoteok",
            "indeed_dubuque",
            "indeed_remote",
            # Future: "linkedin", "weworkremotely", "hn_whoshiring"
        ]
    )

    # === Brief preferences ===
    daily_brief_max_jobs: int = 10  # how many jobs to surface each morning
    minimum_score_to_surface: int = 60  # 0-100; below this, don't show


# Singleton — import this anywhere you need profile data
PROFILE = Profile()