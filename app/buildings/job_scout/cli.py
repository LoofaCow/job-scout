"""
Job scout CLI — entry point for triggering scrape+score runs and inspecting state.

Usage:
    python -m app.buildings.job_scout scrape
    python -m app.buildings.job_scout scrape --max-sources 50
    python -m app.buildings.job_scout scrape --max-sources 20 --max-jobs-to-score 100
    python -m app.buildings.job_scout score-only
    python -m app.buildings.job_scout list-jobs
    python -m app.buildings.job_scout list-jobs --min-score 50
"""

import argparse
import asyncio
import logging
from typing import Optional

from sqlmodel import col, select

from app.buildings.job_scout.agents import score_unscored_jobs
from app.buildings.job_scout.workflow import run_scout
from app.buildings import sourcing  # noqa: F401  - register sourcing tables
from app.profile import PROFILE
from app.spine.storage import Evaluation, Job, get_session, init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="job_scout",
        description="Job scout building CLI — scrape sources, score jobs, inspect.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # === scrape (full pipeline: scrape + score + surface counts) ===
    scrape_p = sub.add_parser(
        "scrape",
        help="Run the full pipeline: scrape every pollable source, score new jobs.",
    )
    scrape_p.add_argument(
        "--max-sources",
        type=int,
        default=None,
        help="Cap how many registry sources to poll. Default: all of them.",
    )
    scrape_p.add_argument(
        "--max-jobs-to-score",
        type=int,
        default=None,
        help=(
            "Cap how many jobs to score this run. Default: score everything "
            "that needs scoring. The next run picks up where this one left off."
        ),
    )

    # === score-only (no scraping; just chew the scoring queue) ===
    score_p = sub.add_parser(
        "score-only",
        help="Skip scraping; score jobs that have no recent evaluation.",
    )
    score_p.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Cap on jobs scored this invocation.",
    )
    score_p.add_argument(
        "--rescore-after-days",
        type=int,
        default=7,
        help="Re-score jobs whose newest evaluation is older than this many days.",
    )

    # === list-jobs (inspect the surfaced set) ===
    list_p = sub.add_parser(
        "list-jobs",
        help="Print scored jobs from the DB, newest first.",
    )
    list_p.add_argument("--limit", type=int, default=20)
    list_p.add_argument(
        "--min-score",
        type=int,
        default=None,
        help=f"Filter by minimum score. Default: PROFILE threshold ({PROFILE.minimum_score_to_surface}).",
    )

    args = parser.parse_args()

    _configure_logging()
    init_db()

    if args.command == "scrape":
        summary = asyncio.run(
            run_scout(
                max_jobs_to_score=args.max_jobs_to_score,
                max_sources=args.max_sources,
            )
        )
        _print_run_summary(summary)
        return

    if args.command == "score-only":
        scored, failed = asyncio.run(
            score_unscored_jobs(
                max_jobs=args.max_jobs,
                rescore_after_days=args.rescore_after_days,
            )
        )
        print(f"\nScored {scored} jobs ({failed} failed)")
        return

    if args.command == "list-jobs":
        threshold = (
            args.min_score if args.min_score is not None
            else PROFILE.minimum_score_to_surface
        )
        _list_jobs(limit=args.limit, min_score=threshold)
        return


# ============================================================================
# Helpers
# ============================================================================


def _print_run_summary(run) -> None:
    print("\n=== ScoutRun summary ===")
    print(f"  run_id:         {run.id}")
    print(f"  started_at:     {run.started_at}")
    print(f"  finished_at:    {run.finished_at}")
    print(f"  jobs_found:     {run.jobs_found}")
    print(f"  jobs_new:       {run.jobs_new}")
    print(f"  jobs_evaluated: {run.jobs_evaluated}")
    print(f"  jobs_surfaced:  {run.jobs_surfaced} (>= {PROFILE.minimum_score_to_surface})")
    if run.error:
        print(f"  error:          {run.error}")


def _list_jobs(*, limit: int, min_score: int) -> None:
    """Pretty-print scored jobs at or above min_score, newest first."""
    with get_session() as session:
        all_jobs = session.exec(
            select(Job).order_by(col(Job.first_seen_at).desc())
        ).all()

        printed = 0
        for job in all_jobs:
            if printed >= limit:
                break
            latest_eval: Optional[Evaluation] = session.exec(
                select(Evaluation)
                .where(Evaluation.job_id == job.id)
                .order_by(col(Evaluation.evaluated_at).desc())
                .limit(1)
            ).first()
            if latest_eval is None or latest_eval.score < min_score:
                continue

            print(
                f"  [{latest_eval.score:>3}] {job.title[:60]:<60s}  "
                f"{job.company[:25]:<25s}  {job.source}"
            )
            printed += 1

        if printed == 0:
            print(f"(no jobs at or above score {min_score})")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
