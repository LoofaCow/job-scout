"""
Researcher CLI.

Usage:
    python -m app.buildings.researcher run
    python -m app.buildings.researcher run --max-jobs 50
"""

import argparse
import asyncio
import logging

from app.buildings.researcher.workflow import research_queued_listings
from app.buildings.tear_apart import models  # noqa: F401  -- table registration
from app.spine.storage import init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="researcher",
        description="Researcher building CLI — fill info gaps Tear Apart flagged.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser(
        "run",
        help="Run fetch_full_listing on every QUEUED ListingAnalysis row.",
    )
    run_p.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Cap on rows processed this invocation.",
    )

    args = parser.parse_args()
    _configure_logging()
    init_db()

    if args.command == "run":
        researched, failed, skipped = asyncio.run(
            research_queued_listings(max_jobs=args.max_jobs)
        )
        print(
            f"\nResearcher: {researched} researched, "
            f"{failed} failed, {skipped} skipped"
        )
        return


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
