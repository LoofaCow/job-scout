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
    # ========================================================================
    # GENERIC DISCOVERY
    # ========================================================================
    {"query": "list of niche job boards", "context": "general directory"},
    {"query": "specialty job boards", "context": "specialized sites"},
    {"query": "best job boards 2026", "context": "current-year listicles"},
    {"query": "underrated job boards", "context": "low-traffic high-quality"},
    {"query": "small job boards niche", "context": "niche sites"},
    {"query": "indie job boards", "context": "independent sites"},
    {"query": "boutique job board", "context": "small specialty sites"},
    {"query": "vertical job board", "context": "industry-specific"},
    {"query": "hidden job boards", "context": "obscure sites"},
    {"query": "community job board", "context": "community-run"},
    {"query": "professional association job board", "context": "association boards"},
    {"query": "union job board", "context": "union-affiliated"},
    {"query": "alumni network jobs", "context": "alumni boards"},

    # ========================================================================
    # TECH — IT operations, infrastructure, support
    # ========================================================================
    {"query": "DevOps job board", "context": "DevOps"},
    {"query": "system administrator job board", "context": "sysadmin"},
    {"query": "IT help desk job board", "context": "help desk"},
    {"query": "automation engineer jobs site", "context": "automation"},
    {"query": "site reliability engineer job board", "context": "SRE"},
    {"query": "platform engineering jobs", "context": "platform eng"},
    {"query": "infrastructure engineer jobs board", "context": "infra"},
    {"query": "MSP job board", "context": "managed service providers"},
    {"query": "tier 1 support jobs board", "context": "entry IT"},
    {"query": "desktop support jobs board", "context": "desktop support"},
    {"query": "network engineer job board", "context": "networking"},
    {"query": "cybersecurity careers board", "context": "security"},
    {"query": "cloud engineer jobs board", "context": "cloud"},
    {"query": "Kubernetes jobs board", "context": "K8s"},
    {"query": "database administrator jobs board", "context": "DBA"},

    # ========================================================================
    # TECH — software engineering
    # ========================================================================
    {"query": "Python developer job board", "context": "Python"},
    {"query": "JavaScript developer job board", "context": "JS"},
    {"query": "Go developer job board", "context": "Go"},
    {"query": "Rust developer job board", "context": "Rust"},
    {"query": "Java developer job board", "context": "Java"},
    {"query": "C# developer job board", "context": "C#/.NET"},
    {"query": "Ruby developer job board", "context": "Ruby"},
    {"query": "PHP developer job board", "context": "PHP"},
    {"query": "frontend developer jobs board", "context": "FE"},
    {"query": "backend developer jobs board", "context": "BE"},
    {"query": "full stack developer jobs board", "context": "FS"},
    {"query": "mobile developer jobs board", "context": "mobile"},
    {"query": "embedded systems jobs board", "context": "embedded"},
    {"query": "game developer jobs board", "context": "games"},
    {"query": "QA engineer jobs board", "context": "QA"},
    {"query": "data engineer jobs board", "context": "data eng"},
    {"query": "data scientist jobs board", "context": "data sci"},
    {"query": "ML engineer jobs board", "context": "ML"},
    {"query": "AI engineer jobs board", "context": "AI"},
    {"query": "junior developer job board", "context": "junior dev"},
    {"query": "entry level tech jobs board", "context": "entry tech"},

    # ========================================================================
    # CREATIVE & DESIGN
    # ========================================================================
    {"query": "graphic design job board", "context": "graphic design"},
    {"query": "UX designer jobs board", "context": "UX"},
    {"query": "UI designer jobs board", "context": "UI"},
    {"query": "product designer jobs board", "context": "product design"},
    {"query": "illustrator jobs board", "context": "illustration"},
    {"query": "animator jobs board", "context": "animation"},
    {"query": "3D artist jobs board", "context": "3D art"},
    {"query": "video editor jobs board", "context": "video editing"},
    {"query": "photographer jobs board", "context": "photography"},
    {"query": "creative director jobs board", "context": "creative direction"},
    {"query": "art director jobs", "context": "art direction"},
    {"query": "fashion industry jobs board", "context": "fashion"},
    {"query": "interior design jobs board", "context": "interior design"},
    {"query": "industrial design jobs board", "context": "industrial design"},

    # ========================================================================
    # WRITING, MEDIA, COMMUNICATIONS
    # ========================================================================
    {"query": "writing job board", "context": "writing"},
    {"query": "freelance writing jobs board", "context": "freelance writing"},
    {"query": "journalism jobs board", "context": "journalism"},
    {"query": "editor jobs board", "context": "editing"},
    {"query": "copywriter jobs board", "context": "copywriting"},
    {"query": "technical writer jobs", "context": "tech writing"},
    {"query": "content marketing jobs board", "context": "content"},
    {"query": "social media manager jobs board", "context": "social media"},
    {"query": "PR jobs board", "context": "public relations"},
    {"query": "communications jobs board", "context": "comms"},
    {"query": "podcast production jobs", "context": "podcasting"},
    {"query": "translator jobs board", "context": "translation"},

    # ========================================================================
    # HEALTHCARE & MEDICAL
    # ========================================================================
    {"query": "nursing job board", "context": "nursing"},
    {"query": "RN jobs board", "context": "registered nurse"},
    {"query": "CNA jobs board", "context": "certified nursing assistant"},
    {"query": "medical assistant jobs board", "context": "MA"},
    {"query": "physician jobs board", "context": "physicians"},
    {"query": "physical therapist jobs board", "context": "PT"},
    {"query": "occupational therapy jobs board", "context": "OT"},
    {"query": "pharmacist jobs board", "context": "pharmacy"},
    {"query": "dental jobs board", "context": "dental"},
    {"query": "veterinary jobs board", "context": "veterinary"},
    {"query": "mental health counselor jobs", "context": "mental health"},
    {"query": "social worker jobs board", "context": "social work"},
    {"query": "healthcare administration jobs", "context": "health admin"},
    {"query": "medical research jobs board", "context": "med research"},

    # ========================================================================
    # EDUCATION
    # ========================================================================
    {"query": "teaching jobs board K-12", "context": "K-12 teaching"},
    {"query": "academic job board", "context": "higher ed"},
    {"query": "professor jobs board", "context": "professors"},
    {"query": "adjunct professor jobs", "context": "adjuncts"},
    {"query": "online teaching jobs board", "context": "online ed"},
    {"query": "ESL teaching jobs board", "context": "ESL"},
    {"query": "tutoring jobs board", "context": "tutoring"},
    {"query": "school administrator jobs", "context": "school admin"},
    {"query": "early childhood education jobs", "context": "early ed"},
    {"query": "special education jobs board", "context": "SPED"},
    {"query": "library jobs board", "context": "libraries"},
    {"query": "instructional designer jobs", "context": "instructional design"},
    {"query": "curriculum developer jobs", "context": "curriculum"},

    # ========================================================================
    # SCIENCE & RESEARCH
    # ========================================================================
    {"query": "biology jobs board", "context": "biology"},
    {"query": "chemistry jobs board", "context": "chemistry"},
    {"query": "physics jobs board", "context": "physics"},
    {"query": "biotech career site", "context": "biotech"},
    {"query": "pharmaceutical jobs board", "context": "pharma"},
    {"query": "lab technician jobs board", "context": "lab tech"},
    {"query": "field research jobs board", "context": "field research"},
    {"query": "ecology jobs board", "context": "ecology"},
    {"query": "marine biology jobs", "context": "marine bio"},
    {"query": "environmental science jobs", "context": "env sci"},
    {"query": "geology jobs board", "context": "geology"},
    {"query": "archaeology jobs board", "context": "archaeology"},
    {"query": "astronomy jobs board", "context": "astronomy"},

    # ========================================================================
    # ENGINEERING (non-software)
    # ========================================================================
    {"query": "mechanical engineering jobs board", "context": "ME"},
    {"query": "electrical engineering jobs board", "context": "EE"},
    {"query": "civil engineering jobs board", "context": "civil eng"},
    {"query": "chemical engineering jobs board", "context": "chem eng"},
    {"query": "aerospace engineering jobs board", "context": "aerospace"},
    {"query": "manufacturing engineer jobs board", "context": "manufacturing eng"},
    {"query": "structural engineer jobs board", "context": "structural"},
    {"query": "environmental engineer jobs board", "context": "env eng"},
    {"query": "industrial engineer jobs", "context": "industrial eng"},
    {"query": "robotics engineer jobs", "context": "robotics"},
    {"query": "PCB design jobs board", "context": "PCB"},
    {"query": "FPGA engineer jobs", "context": "FPGA"},

    # ========================================================================
    # TRADES & SKILLED LABOR
    # ========================================================================
    {"query": "electrician jobs board", "context": "electrical trades"},
    {"query": "plumber jobs board", "context": "plumbing"},
    {"query": "HVAC technician jobs board", "context": "HVAC"},
    {"query": "welder jobs board", "context": "welding"},
    {"query": "carpenter jobs board", "context": "carpentry"},
    {"query": "construction jobs board", "context": "construction"},
    {"query": "trucking jobs board CDL", "context": "trucking"},
    {"query": "auto mechanic jobs board", "context": "auto mechanic"},
    {"query": "machinist jobs board", "context": "machinist"},
    {"query": "skilled trades job board", "context": "trades general"},
    {"query": "union trades hiring", "context": "union trades"},
    {"query": "apprenticeship trades", "context": "trade apprenticeships"},

    # ========================================================================
    # BUSINESS, FINANCE, OPERATIONS
    # ========================================================================
    {"query": "accounting jobs board", "context": "accounting"},
    {"query": "CPA jobs board", "context": "CPA"},
    {"query": "bookkeeper jobs board", "context": "bookkeeping"},
    {"query": "finance jobs board", "context": "finance"},
    {"query": "investment banking jobs board", "context": "IB"},
    {"query": "fintech jobs board", "context": "fintech"},
    {"query": "actuary jobs board", "context": "actuary"},
    {"query": "human resources jobs board", "context": "HR"},
    {"query": "recruiter jobs board", "context": "recruiting"},
    {"query": "operations manager jobs board", "context": "ops mgmt"},
    {"query": "supply chain jobs board", "context": "supply chain"},
    {"query": "logistics jobs board", "context": "logistics"},
    {"query": "project manager jobs board", "context": "PM"},
    {"query": "product manager jobs board", "context": "product mgmt"},
    {"query": "business analyst jobs board", "context": "BA"},
    {"query": "consulting jobs board", "context": "consulting"},

    # ========================================================================
    # SALES & MARKETING
    # ========================================================================
    {"query": "sales jobs board", "context": "sales"},
    {"query": "B2B sales jobs board", "context": "B2B sales"},
    {"query": "SDR jobs board", "context": "SDR"},
    {"query": "account executive jobs board", "context": "AE"},
    {"query": "marketing jobs board", "context": "marketing"},
    {"query": "digital marketing jobs board", "context": "digital marketing"},
    {"query": "growth marketing jobs board", "context": "growth"},
    {"query": "SEO jobs board", "context": "SEO"},
    {"query": "advertising jobs board", "context": "advertising"},
    {"query": "brand manager jobs board", "context": "brand"},
    {"query": "ecommerce jobs board", "context": "ecommerce"},

    # ========================================================================
    # HOSPITALITY, FOOD, RETAIL
    # ========================================================================
    {"query": "restaurant jobs board", "context": "restaurants"},
    {"query": "chef jobs board", "context": "culinary"},
    {"query": "hospitality jobs board", "context": "hospitality"},
    {"query": "hotel jobs board", "context": "hotels"},
    {"query": "bartender jobs board", "context": "bartending"},
    {"query": "barista jobs board", "context": "coffee"},
    {"query": "retail jobs board", "context": "retail"},
    {"query": "tourism jobs board", "context": "tourism"},
    {"query": "event planner jobs board", "context": "events"},
    {"query": "wedding industry jobs", "context": "weddings"},
    {"query": "cruise ship jobs board", "context": "cruise"},

    # ========================================================================
    # PUBLIC SERVICE & GOV
    # ========================================================================
    {"query": "government jobs board", "context": "gov"},
    {"query": "federal jobs board", "context": "federal"},
    {"query": "state government jobs board", "context": "state gov"},
    {"query": "local government jobs", "context": "local gov"},
    {"query": "non-profit job board", "context": "non-profit"},
    {"query": "NGO job board", "context": "NGO"},
    {"query": "foundation jobs board", "context": "foundations"},
    {"query": "diplomatic jobs board", "context": "diplomacy"},
    {"query": "law enforcement jobs board", "context": "law enforcement"},
    {"query": "firefighter jobs board", "context": "firefighting"},
    {"query": "EMT paramedic jobs board", "context": "EMS"},
    {"query": "civic tech jobs", "context": "civic tech"},
    {"query": "policy analyst jobs board", "context": "policy"},
    {"query": "veterans jobs board", "context": "veterans"},

    # ========================================================================
    # LEGAL
    # ========================================================================
    {"query": "legal jobs board", "context": "legal"},
    {"query": "paralegal jobs board", "context": "paralegal"},
    {"query": "attorney jobs board", "context": "attorneys"},
    {"query": "law firm hiring board", "context": "law firms"},
    {"query": "in-house counsel jobs", "context": "in-house legal"},
    {"query": "compliance officer jobs board", "context": "compliance"},

    # ========================================================================
    # AGRICULTURE, ENVIRONMENT, OUTDOOR
    # ========================================================================
    {"query": "agriculture jobs board", "context": "agriculture"},
    {"query": "farming jobs board", "context": "farming"},
    {"query": "ranch jobs board", "context": "ranching"},
    {"query": "agtech job board", "context": "agtech"},
    {"query": "national park jobs board", "context": "parks"},
    {"query": "conservation jobs board", "context": "conservation"},
    {"query": "wildlife jobs board", "context": "wildlife"},
    {"query": "forestry jobs board", "context": "forestry"},
    {"query": "outdoor industry jobs", "context": "outdoor"},
    {"query": "climate tech jobs board", "context": "climate"},
    {"query": "renewable energy jobs board", "context": "renewables"},
    {"query": "solar industry jobs", "context": "solar"},
    {"query": "wind energy jobs", "context": "wind"},
    {"query": "sustainability jobs board", "context": "sustainability"},

    # ========================================================================
    # TRANSPORT & LOGISTICS (non-trucking)
    # ========================================================================
    {"query": "aviation jobs board", "context": "aviation"},
    {"query": "pilot jobs board", "context": "pilots"},
    {"query": "airline jobs board", "context": "airlines"},
    {"query": "maritime jobs board", "context": "maritime"},
    {"query": "rail industry jobs board", "context": "rail"},
    {"query": "warehouse jobs board", "context": "warehousing"},

    # ========================================================================
    # ENTERTAINMENT, ARTS, MUSIC, SPORTS
    # ========================================================================
    {"query": "film industry jobs board", "context": "film"},
    {"query": "tv production jobs board", "context": "TV production"},
    {"query": "music industry jobs board", "context": "music industry"},
    {"query": "performing arts jobs board", "context": "performing arts"},
    {"query": "theater jobs board", "context": "theater"},
    {"query": "museum jobs board", "context": "museums"},
    {"query": "gallery jobs board", "context": "galleries"},
    {"query": "sports industry jobs board", "context": "sports"},
    {"query": "fitness industry jobs board", "context": "fitness"},
    {"query": "yoga instructor jobs board", "context": "yoga"},

    # ========================================================================
    # CARE WORK, HOME SERVICES, FAMILY
    # ========================================================================
    {"query": "childcare jobs board", "context": "childcare"},
    {"query": "nanny jobs board", "context": "nanny"},
    {"query": "elder care jobs board", "context": "elder care"},
    {"query": "caregiver jobs board", "context": "caregiving"},
    {"query": "personal assistant jobs", "context": "PA"},
    {"query": "house manager jobs board", "context": "estate mgmt"},
    {"query": "domestic worker jobs board", "context": "domestic work"},
    {"query": "pet sitting jobs board", "context": "pet sitting"},
    {"query": "dog trainer jobs board", "context": "dog training"},

    # ========================================================================
    # RELIGION, COMMUNITY
    # ========================================================================
    {"query": "ministry jobs board", "context": "ministry"},
    {"query": "church jobs board", "context": "church"},
    {"query": "chaplain jobs board", "context": "chaplaincy"},
    {"query": "youth ministry jobs", "context": "youth ministry"},
    {"query": "community organizer jobs", "context": "community organizing"},

    # ========================================================================
    # WORK ARRANGEMENT
    # ========================================================================
    {"query": "remote-first companies job board", "context": "remote-first"},
    {"query": "remote-only job board", "context": "remote only"},
    {"query": "hybrid jobs board", "context": "hybrid"},
    {"query": "4 day work week jobs", "context": "4dwk"},
    {"query": "freelance contract job board", "context": "freelance"},
    {"query": "part time jobs board", "context": "part-time"},
    {"query": "seasonal jobs board", "context": "seasonal"},
    {"query": "internship board", "context": "internships"},
    {"query": "apprenticeship jobs board", "context": "apprenticeships"},
    {"query": "co-op opportunities", "context": "co-ops"},

    # ========================================================================
    # GEOGRAPHIC — broad regions
    # ========================================================================
    {"query": "European remote jobs board", "context": "EU remote"},
    {"query": "UK jobs board", "context": "UK"},
    {"query": "Canada jobs board", "context": "Canada"},
    {"query": "Australia jobs board", "context": "Australia"},
    {"query": "New Zealand jobs board", "context": "NZ"},
    {"query": "Latin America jobs board", "context": "LATAM"},
    {"query": "Asia Pacific jobs board", "context": "APAC"},
    {"query": "Africa remote jobs board", "context": "Africa"},
    {"query": "Middle East jobs board", "context": "Middle East"},

    # ========================================================================
    # GEOGRAPHIC — US regions
    # ========================================================================
    {"query": "Midwest jobs board", "context": "Midwest US"},
    {"query": "Northeast tech jobs board", "context": "Northeast US"},
    {"query": "Southwest jobs board", "context": "Southwest US"},
    {"query": "Pacific Northwest jobs", "context": "PNW"},
    {"query": "Bay Area jobs board", "context": "Bay Area"},
    {"query": "rural jobs board", "context": "rural"},
    {"query": "small town jobs board", "context": "small town"},

    # ========================================================================
    # IDENTITY & DEMOGRAPHIC
    # ========================================================================
    {"query": "diversity job board", "context": "DEI focused"},
    {"query": "women in tech jobs board", "context": "women in tech"},
    {"query": "Black professionals job board", "context": "Black-focused"},
    {"query": "LGBTQ jobs board", "context": "LGBTQ"},
    {"query": "neurodivergent jobs board", "context": "ND-friendly"},
    {"query": "disability inclusive jobs board", "context": "disability inclusive"},
    {"query": "older workers jobs board", "context": "50+"},
    {"query": "career changers job board", "context": "career change"},
    {"query": "second chance jobs board", "context": "second chance"},
    {"query": "bilingual jobs board", "context": "bilingual"},

    # ========================================================================
    # ALTERNATIVE / UNUSUAL
    # ========================================================================
    {"query": "weird jobs board", "context": "weird/unusual"},
    {"query": "unusual careers website", "context": "unusual"},
    {"query": "expedition jobs board", "context": "expeditions"},
    {"query": "research vessel jobs", "context": "research vessels"},
    {"query": "remote location jobs", "context": "remote-place"},
    {"query": "Antarctica jobs board", "context": "Antarctica"},
    {"query": "yacht crew jobs board", "context": "yachting"},
    {"query": "circus performer jobs", "context": "circus"},
    {"query": "ghostwriter jobs board", "context": "ghostwriting"},
    {"query": "voice acting jobs board", "context": "VO"},
    {"query": "research participant paid", "context": "research participants"},
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