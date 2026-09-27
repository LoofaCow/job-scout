"""
Tear Apart agent — the LLM-driven normalizer.

Given a Job's title + company + description, the agent returns a
TearApartResult: classification (real job vs. not), extracted structured
fields (salary, seniority, arrangement, etc.), required/nice-to-have
skills, a quality score, and a list of info gaps that warrant research.

The agent runs on the local tier (Qwen 2.5 7B) — same model as the
scorer. Structured output via Pydantic + Agno's `output_schema`.

Re-running Tear Apart on the same job is supported: when the researcher
fills in `full_description`, the workflow calls analyze() again with the
richer text, and `upsert_analysis` updates the row in place.
"""

from __future__ import annotations

import logging
from typing import Optional

from agno.agent import Agent
from pydantic import BaseModel, Field

from app.buildings.tear_apart.models import (
    EmploymentType,
    ListingClass,
    Seniority,
    WorkArrangement,
)
from app.spine.models import Tier, get_model
from app.spine.storage import Job

logger = logging.getLogger(__name__)


# ============================================================================
# Structured output — what the agent must return
# ============================================================================


class InfoGap(BaseModel):
    """One missing-data point worth researching."""
    field: str = Field(
        description=(
            "Which field is missing or weakly inferred. Use a stable short "
            "name like 'salary', 'seniority', 'work_arrangement', 'company', "
            "'required_skills', 'visa_sponsorship', 'employment_type', "
            "'full_description'."
        )
    )
    why: str = Field(
        description=(
            "One short clause explaining why this gap matters or what's "
            "missing. Plain English."
        )
    )


class TearApartResult(BaseModel):
    """Structured output from one Tear Apart pass on one Job."""

    listing_class: ListingClass = Field(
        description=(
            "What is this artifact? REAL_JOB, NOT_A_JOB (podcast/article/etc.), "
            "AGGREGATED_LIST (listicle), EXPIRED (filled/closed), or AMBIGUOUS "
            "(too thin to tell). Bias toward REAL_JOB only when the text is "
            "clearly a job posting."
        )
    )
    is_real_job_confidence: int = Field(
        ge=0, le=100,
        description=(
            "0-100 confidence that this is a real, currently-open job. Set to "
            "0 if listing_class is NOT_A_JOB or EXPIRED."
        ),
    )
    title_normalized: Optional[str] = Field(
        default=None,
        description=(
            "Cleaned-up role title. If the scraped title was already clean, "
            "echo it. Strip company suffixes and trailing fluff."
        ),
    )
    company_normalized: Optional[str] = Field(
        default=None,
        description=(
            "The hiring company's actual name. Note: many RSS feeds put the "
            "BOARD's name (e.g. 'DevITJobs', 'Python Job Board') in the "
            "'company' field — if the listing actually mentions a different "
            "hiring company, use that. Otherwise null."
        ),
    )
    seniority: Seniority = Field(
        default=Seniority.UNKNOWN,
        description=(
            "Seniority level: INTERN, ENTRY, JUNIOR, MID, SENIOR, STAFF, "
            "PRINCIPAL, LEAD, MANAGER, or UNKNOWN. Inferred from title and "
            "any years-of-experience requirement in the body."
        ),
    )
    work_arrangement: WorkArrangement = Field(
        default=WorkArrangement.UNKNOWN,
        description="REMOTE, HYBRID, ONSITE, or UNKNOWN.",
    )
    employment_type: EmploymentType = Field(
        default=EmploymentType.UNKNOWN,
        description=(
            "FULL_TIME, PART_TIME, CONTRACT, INTERNSHIP, TEMPORARY, or UNKNOWN."
        ),
    )
    salary_min_usd: Optional[int] = Field(
        default=None,
        description=(
            "Annual salary floor in USD if explicitly stated. Convert from "
            "other currencies only if the listing makes a USD equivalent "
            "obvious. Null if not stated."
        ),
    )
    salary_max_usd: Optional[int] = Field(
        default=None,
        description="Annual salary ceiling in USD. Same rules as salary_min_usd.",
    )
    salary_currency: Optional[str] = Field(
        default=None,
        description=(
            "Currency code (USD, EUR, GBP, CHF, etc.) if salary is stated. "
            "Null if no salary at all."
        ),
    )
    location_normalized: Optional[str] = Field(
        default=None,
        description=(
            "Cleaned-up location string. 'Remote' is fine; 'Anywhere'-style "
            "phrasing should map to 'Remote'."
        ),
    )
    visa_sponsorship_offered: Optional[bool] = Field(
        default=None,
        description=(
            "True if the listing explicitly says they sponsor visas, False if "
            "explicitly says they don't, null if unstated."
        ),
    )
    required_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Skills/technologies the listing says are required. Short labels, "
            "no full sentences. Empty list if none stated."
        ),
    )
    nice_to_have_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Skills/technologies marked as preferred or bonus. Empty list if "
            "none stated."
        ),
    )
    description_quality_score: int = Field(
        ge=0, le=100,
        description=(
            "0-100 score for how rich the source data is. 0-30 = title and "
            "almost nothing else; 31-60 = some details but key fields missing; "
            "61-85 = standard listing with role + skills + most fields; "
            "86-100 = comprehensive (responsibilities, requirements, salary, "
            "team, sponsorship, all clearly stated)."
        ),
    )
    info_gaps: list[InfoGap] = Field(
        default_factory=list,
        description=(
            "List of missing or weakly-inferred fields that researching the "
            "full page would likely fill in. Examples: 'salary', "
            "'work_arrangement', 'company' (when the listing exposes only the "
            "board name), 'full_description' (when the source text is < 300 "
            "chars). Empty list if listing is comprehensive."
        ),
    )
    rationale: str = Field(
        description=(
            "2-3 plain-English sentences summarizing the classification and "
            "what was extracted. Reference specific text where useful."
        ),
    )


# ============================================================================
# Agent factory
# ============================================================================


def build_tear_apart_agent() -> Agent:
    """Construct a fresh Tear Apart agent with the local-tier model."""
    return Agent(
        model=get_model(Tier.LOCAL),
        instructions=_INSTRUCTIONS,
        output_schema=TearApartResult,
        use_json_mode=True,
    )


_INSTRUCTIONS = """\
You are a job-listing normalizer. You receive a job listing's scraped data
(title, company, location, URL, description) and return a structured
analysis: classification, extracted fields, skills, quality assessment, and
a list of info gaps that would benefit from further research.

## Output values are lowercase snake_case

All enum-typed fields must use lowercase snake_case values, matching the
JSON schema exactly:
  listing_class:    real_job | not_a_job | aggregated_list | expired | ambiguous
  seniority:        intern | entry | junior | mid | senior | staff | principal | lead | manager | unknown
  work_arrangement: remote | hybrid | onsite | unknown
  employment_type:  full_time | part_time | contract | internship | temporary | unknown

Do not emit uppercase variants like "REAL_JOB" or "Full Time" — they will fail
validation and the listing will be dropped.

## What is and isn't a real job

A real_job has at minimum: a role title, a hiring entity (or strong
inference of one), and the implication that they are accepting applicants.
Body text describing responsibilities or skills is supportive but not
required.

not_a_job examples (be willing to call these out):
  - Podcast episodes, even when the title mentions "career" or "leadership"
  - Articles ABOUT job hunting, salary trends, etc.
  - Tutorials, courses, certifications, books
  - Login walls or marketing pages with no listing
  - "Top 10 jobs of 2026" listicles -> aggregated_list, not real_job

When the scraped data is too thin to decide, prefer ambiguous over guessing
real_job. ambiguous rows are queued for research, which usually resolves them.

## Watch out for board-as-company confusion

Many RSS feeds put the BOARD's name in the company slot:
  - "DevITJobs" is a board, not an employer.
  - "Python Job Board" is a board, not an employer.
  - "Conservation Careers" / "Dr Nick Askew" are boards/authors, not employers.

If the listing body mentions a different hiring company, put THAT in
company_normalized. If you can't determine the real company, leave it null
and add `company` as an info gap.

## Skills extraction discipline

required_skills and nice_to_have_skills should be short labels like:
  ["Python", "AWS", "PostgreSQL", "Linux administration"]

Not phrases or sentences. Not "experience with Python" — just "Python".
Empty lists are fine. Don't invent skills the listing doesn't mention.

## Info gaps

Add an info_gap entry whenever a high-value field is missing or weakly
inferred. Use these stable field names:
  - "full_description" — the source text is < 300 chars (typical RSS teaser)
  - "salary" — no comp range stated
  - "seniority" — title doesn't pin it down and body doesn't say
  - "work_arrangement" — remote/hybrid/onsite not clearly stated
  - "company" — the visible "company" looks like a board name, not employer
  - "required_skills" — listing has no skill list
  - "visa_sponsorship" — relevant for international listings, unstated
  - "employment_type" — full-time vs contract not stated

Empty info_gaps means the listing was comprehensive enough to skip research.

## Quality score calibration

  - 0-15: title only, almost no body
  - 15-35: title + a couple sentences (typical RSS teaser)
  - 35-60: title + role description but missing key fields
  - 60-80: standard full job posting with role, requirements, location
  - 80-100: comprehensive with salary, sponsorship, team info, application
    process, etc.

A description_quality_score under 35 should ALWAYS produce at least one
info_gap (almost always "full_description").

## Be honest

The user wants accurate normalization, not flattering normalization.
Misclassifying a podcast as a job wastes scoring effort and pollutes the
dashboard. Marking an obvious junior listing as senior wastes the user's
attention. Calibrate carefully.
"""


# ============================================================================
# Public API
# ============================================================================


async def analyze_listing(
    *,
    title: str,
    company: str,
    location: str,
    url: str,
    description: str,
) -> TearApartResult:
    """
    Run the Tear Apart agent on one listing's text. Pure function — caller
    handles persistence.
    """
    agent = build_tear_apart_agent()
    prompt = _format_prompt(
        title=title,
        company=company,
        location=location,
        url=url,
        description=description,
    )
    response = await agent.arun(prompt)

    if not isinstance(response.content, TearApartResult):
        # Log a snippet of whatever came back so we can diagnose the schema
        # mismatch (typically: model emitted uppercase enum values, or
        # non-JSON text). Without this the workflow swallows the error as a
        # per-job "failed" with no signal about what went wrong.
        preview = repr(response.content)[:500] if response.content is not None else "None"
        logger.warning(
            f"Tear Apart agent returned non-conforming output: "
            f"type={type(response.content).__name__}, preview={preview}"
        )
        raise RuntimeError(
            f"Tear Apart agent returned unexpected content type: "
            f"{type(response.content).__name__}"
        )
    return response.content


async def analyze_job(job: Job, *, override_description: Optional[str] = None) -> TearApartResult:
    """
    Convenience wrapper: run analyze_listing with a Job's fields.

    `override_description` lets the workflow pass a richer body than
    `job.description` — typically the researcher-fetched full page.
    """
    return await analyze_listing(
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        description=override_description if override_description is not None else job.description,
    )


# ============================================================================
# Internals
# ============================================================================


def _format_prompt(
    *,
    title: str,
    company: str,
    location: str,
    url: str,
    description: str,
) -> str:
    # Cap description so we don't blow context on giant pages.
    desc_capped = description[:6000]
    desc_note = "" if len(description) <= 6000 else (
        f"\n\n[NOTE: description was truncated from {len(description)} chars]"
    )
    return f"""\
Analyze this job listing.

Title: {title}
Company (from scraper, possibly board name): {company}
Location: {location}
URL: {url}

Description (length: {len(description)} chars):
---
{desc_capped}{desc_note}
---

Return the structured analysis.
"""
