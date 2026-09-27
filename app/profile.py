"""
Job-hunt profile — what the scout looks for and how the scorer ranks it.

This file is a generic EXAMPLE, committed so the repo runs out of the box.
Every agent reads from `PROFILE`.

To use your own without committing it, copy this file to
`app/profile_local.py` and edit it there. That path is gitignored, and if it
exists it wins — see the bottom of this file. Otherwise the example below is
used, and you can edit it directly.
"""

from pydantic import BaseModel, Field


class Profile(BaseModel):
    # === Identity (used in cover letters / outreach drafts later) ===
    name: str = "Example User"
    location_city: str = "Springfield, IL"
    open_to_relocation: bool = True
    relocation_targets: list[str] = Field(
        default_factory=lambda: [
            "Austin, TX",
            "Denver, CO",
            "Raleigh, NC",
            "Minneapolis, MN",
        ]
    )

    # Free text. Gives the scorer context the skill lists can't carry —
    # career direction, what you've actually shipped, how you work.
    background_summary: str = (
        "Self-taught developer with a support and operations background, "
        "moving into backend and AI work. Comfortable across the stack: API "
        "design, agent orchestration, persistence, and deployment."
    )

    # === Target roles — priority-ordered, earlier = higher pull ===
    target_roles: list[str] = Field(
        default_factory=lambda: [
            "Junior Software Engineer",
            "Junior Backend Engineer",
            "AI Agent Developer",
            "Automation Engineer",
            "IT Service Desk",
        ]
    )

    target_seniority: list[str] = Field(
        default_factory=lambda: ["entry-level", "junior", "associate", "I / II"]
    )

    # === Strong skills — productive without much reference ===
    # Keep this tight. A long list dilutes the scorer's signal.
    strong_skills: list[str] = Field(
        default_factory=lambda: [
            "Python",
            "FastAPI",
            "Local LLM inference (Ollama)",
            "Multi-agent system design",
            "Pydantic-validated structured outputs",
            "SQL / SQLite",
            "Linux",
            "Docker / Podman",
        ]
    )

    # === Learning — studied, but not claimed as mastery ===
    learning_skills: list[str] = Field(
        default_factory=lambda: [
            "Cloud (AWS / GCP)",
            "Terraform",
            "Kubernetes",
            "Networking fundamentals",
        ]
    )

    # === Domains whose vocabulary you actually know ===
    domain_expertise: list[str] = Field(
        default_factory=lambda: [
            "AI / agentic systems and multi-agent architectures",
            "Local LLM inference and prompt discipline",
            "Self-hosted infrastructure and homelab design",
            "Technical support and consumer-electronics repair",
            "Quality control methodology",
        ]
    )

    # === Compensation (USD) — 0 means "don't filter on salary" ===
    salary_floor_usd: int = 0
    salary_target_usd: int = 0
    salary_ceiling_irrelevant_above: int = 500_000  # ignore anomalous listings

    # === Work arrangement ===
    remote_ok: bool = True
    hybrid_ok: bool = True
    onsite_ok: bool = True  # home city or relocation targets only

    # === Dealbreakers — auto-reject listings containing these ===
    dealbreaker_keywords: list[str] = Field(
        default_factory=lambda: [
            "unpaid",
            "commission only",
            "100% commission",
            "MLM",
            "multi-level marketing",
            "door-to-door",
            "must own vehicle for company use",
        ]
    )

    # === Companies of interest (boost score if matched) ===
    target_companies_local: list[str] = Field(default_factory=list)

    # === Brief preferences ===
    daily_brief_max_jobs: int = 10
    minimum_score_to_surface: int = 30  # 0-100; below this, don't show


# A gitignored app/profile_local.py wins if present; otherwise use the example.
try:
    from app.profile_local import PROFILE  # type: ignore[no-redef]
except ImportError:
    PROFILE = Profile()
