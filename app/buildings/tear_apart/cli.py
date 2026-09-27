"""
Tear Apart CLI.

Usage:
    python -m app.buildings.tear_apart analyze
    python -m app.buildings.tear_apart analyze --max-jobs 200 --min-score 30
    python -m app.buildings.tear_apart reanalyze
    python -m app.buildings.tear_apart status
"""

import argparse
import asyncio
import logging
from typing import Optional

from sqlmodel import col, func, select

from app.buildings.tear_apart import models  # noqa: F401  -- table registration
from app.buildings.tear_apart.models import (
    ListingAnalysis,
    ListingClass,
    ResearchStatus,
)
from app.buildings.tear_apart.workflow import (
    analyze_unanalyzed_jobs,
    reanalyze_after_research,
)
from app.spine.storage import Job, get_session, init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tear_apart",
        description="Tear Apart building CLI — normalize raw scraped listings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # === analyze ===
    analyze_p = sub.add_parser(
        "analyze",
        help="Run Tear Apart on Job rows that don't have a ListingAnalysis yet.",
    )
    analyze_p.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Cap on jobs analyzed this invocation. Default: no cap.",
    )
    analyze_p.add_argument(
        "--min-score",
        type=int,
        default=None,
        help=(
            "Only analyze jobs whose latest evaluation score is at or above "
            "this. Useful for focusing Tear Apart on already-surfaced jobs."
        ),
    )

    # === reanalyze ===
    sub.add_parser(
        "reanalyze",
        help=(
            "Re-run Tear Apart on rows where the researcher just dropped a "
            "richer full_description."
        ),
    )

    # === status ===
    sub.add_parser(
        "status",
        help="Print analysis-table counts (classification, research queue).",
    )

    args = parser.parse_args()
    _configure_logging()
    init_db()

    if args.command == "analyze":
        analyzed, queued, failed = asyncio.run(
            analyze_unanalyzed_jobs(
                max_jobs=args.max_jobs,
                min_score=args.min_score,
            )
        )
        print(
            f"\nTear Apart: {analyzed} analyzed, "
            f"{queued} queued for research, {failed} failed"
        )
        return

    if args.command == "reanalyze":
        reanalyzed, failed = asyncio.run(reanalyze_after_research())
        print(f"\nTear Apart: {reanalyzed} re-analyzed, {failed} failed")
        return

    if args.command == "status":
        _print_status()
        return


def _print_status() -> None:
    with get_session() as session:
        total_analyses = session.exec(
            select(func.count()).select_from(ListingAnalysis)
        ).one()
        total_jobs = session.exec(select(func.count()).select_from(Job)).one()

        print(f"=== Tear Apart status ===")
        print(f"  jobs in DB:           {total_jobs}")
        print(f"  ListingAnalysis rows: {total_analyses}")
        print(f"  unanalyzed jobs:      {total_jobs - total_analyses}")
        print()
        print("--- by listing_class ---")
        for klass in ListingClass:
            n = session.exec(
                select(func.count()).select_from(ListingAnalysis)
                .where(ListingAnalysis.listing_class == klass)
            ).one()
            if n:
                print(f"  {klass.value:18s} {n}")
        print()
        print("--- by research_status ---")
        for status in ResearchStatus:
            n = session.exec(
                select(func.count()).select_from(ListingAnalysis)
                .where(ListingAnalysis.research_status == status)
            ).one()
            if n:
                print(f"  {status.value:14s} {n}")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
