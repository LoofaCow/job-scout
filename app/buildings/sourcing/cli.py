"""
Sourcing CLI — entry point for triggering hunters and inspecting the registry.

Usage:
    python -m app.buildings.sourcing run board_hunter
    python -m app.buildings.sourcing run board_hunter --strategy awesome_lists
    python -m app.buildings.sourcing run board_hunter --max-candidates 10
    python -m app.buildings.sourcing list-sources
    python -m app.buildings.sourcing list-sources --status quarantine
    python -m app.buildings.sourcing list-sources --full-notes
"""

import argparse
import asyncio
import json
import logging

from sqlmodel import select

from app.buildings.sourcing.agents.board_hunter import (
    DEFAULT_MAX_CANDIDATES,
    run_board_hunter,
)
from app.buildings.sourcing.models import Source, SourceStatus
from app.spine.storage import get_session, init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sourcing",
        description="Sourcing department CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # === run ===
    run_p = sub.add_parser("run", help="Run a hunter")
    run_p.add_argument(
        "hunter",
        choices=["board_hunter"],
        help="Which hunter to run",
    )
    run_p.add_argument(
        "--strategy",
        default=None,
        help="Run only this single strategy (by name)",
    )
    run_p.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        help=f"Cap LLM verifications (default {DEFAULT_MAX_CANDIDATES})",
    )

    # === list-sources ===
    list_p = sub.add_parser("list-sources", help="Print sources in the registry")
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument(
        "--status",
        choices=[s.value for s in SourceStatus],
        default=None,
        help="Filter by status",
    )
    list_p.add_argument(
        "--full-notes",
        action="store_true",
        help="Show full verifier rationale instead of one-line preview",
    )

    args = parser.parse_args()

    _configure_logging()
    init_db()

    if args.command == "run":
        if args.hunter == "board_hunter":
            summary = asyncio.run(
                run_board_hunter(
                    strategy_filter=args.strategy,
                    max_candidates=args.max_candidates,
                )
            )
            print("\n=== BoardHunter summary ===")
            print(json.dumps(summary, indent=2))
        return

    if args.command == "list-sources":
        _list_sources(
            limit=args.limit,
            status=args.status,
            full_notes=args.full_notes,
        )
        return


# ============================================================================
# list-sources
# ============================================================================


def _list_sources(
    *,
    limit: int,
    status: str | None,
    full_notes: bool = False,
) -> None:
    with get_session() as session:
        stmt = select(Source).limit(limit)
        if status is not None:
            stmt = select(Source).where(Source.status == SourceStatus(status)).limit(limit)
        sources = session.exec(stmt).all()

        if not sources:
            print("(no sources found)")
            return

        print(f"=== {len(sources)} sources ===\n")
        for s in sources:
            print(f"  [{s.status.value:10s}] {s.name}")
            print(f"      url:        {s.url}")
            print(f"      type:       {s.source_type.value}")
            print(f"      pipeline:   {s.pipeline.value}")
            print(f"      discovered: {s.discovered_by} via {s.discovered_strategy}")
            if s.scraper_hint:
                print(f"      hint:       {s.scraper_hint}")
            if s.notes:
                if full_notes:
                    indented = s.notes.replace("\n", "\n                  ")
                    print(f"      notes:      {indented}")
                else:
                    first_line = s.notes.splitlines()[0][:120]
                    suffix = "..." if len(s.notes) > 120 or "\n" in s.notes else ""
                    print(f"      notes:      {first_line}{suffix}")
            print()


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)