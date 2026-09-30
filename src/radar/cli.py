"""Command line entry point wiring fetch -> triage -> diff -> sheet -> notify -> state."""

import argparse
import logging
import os
import smtplib
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import IntEnum
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

from radar.models import Job
from radar.notify import render_digest, send_email, write_step_summary
from radar.sheet import JobSheet, SheetError
from radar.sources import SourceError, fetch_all
from radar.state import Diff, StateError, Store
from radar.triage import triage

logger = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_STATE_PATH = Path("data/state.json")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

ENV_SHEET_ID = "JOB_RADAR_SHEET_ID"
ENV_SERVICE_ACCOUNT_JSON = "GOOGLE_SERVICE_ACCOUNT_JSON"
ENV_SMTP_USER = "SMTP_USER"
ENV_SMTP_PASSWORD = "SMTP_PASSWORD"
ENV_NOTIFY_EMAIL = "NOTIFY_EMAIL"

SHEET_ENV_VARS = (ENV_SHEET_ID, ENV_SERVICE_ACCOUNT_JSON)
EMAIL_ENV_VARS = (ENV_SMTP_USER, ENV_SMTP_PASSWORD, ENV_NOTIFY_EMAIL)


class ExitCode(IntEnum):
    OK = 0
    CONFIG_ERROR = 2
    SHEET_ERROR = 3
    NO_SOURCES = 4


class AppendPlan(NamedTuple):
    """Ranked jobs split by what the sheet already holds."""

    to_append: list[Job]
    already_present: list[Job]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="job-radar",
        description="Collect Summer internship postings, track them in a sheet, and email a digest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, triage and print the digest without writing the sheet, email or state.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to the dedupe state file (default: {DEFAULT_STATE_PATH}).",
    )
    parser.add_argument("--verbose", action="store_true", help="Log at DEBUG level.")
    return parser.parse_args(argv)


def _missing_env(names: tuple[str, ...]) -> list[str]:
    return [name for name in names if not os.environ.get(name)]


def _send_email_best_effort(digest: str) -> None:
    missing = _missing_env(EMAIL_ENV_VARS)
    if missing:
        logger.warning("Skipping email, unset variables: %s", ", ".join(missing))
        return
    try:
        send_email(
            digest,
            os.environ[ENV_SMTP_USER],
            os.environ[ENV_SMTP_PASSWORD],
            os.environ[ENV_NOTIFY_EMAIL],
        )
    except (smtplib.SMTPException, OSError):
        logger.exception("Email delivery failed, continuing without it")


def _reconcile_with_sheet(jobs: Sequence[Job], rows: Mapping[str, int]) -> AppendPlan:
    """Return missing and present jobs using the sheet as authority."""
    to_append: list[Job] = [job for job in jobs if job.key not in rows]
    already_present: list[Job] = [job for job in jobs if job.key in rows]
    if already_present:
        logger.info(
            "%d job(s) already in the sheet, appending %d",
            len(already_present),
            len(to_append),
        )
    return AppendPlan(to_append=to_append, already_present=already_present)


def _publish(
    diff: Diff, ranked_jobs: list[Job], store: Store, state_path: Path
) -> ExitCode:
    try:
        sheet: JobSheet = JobSheet.open(
            os.environ[ENV_SHEET_ID], os.environ[ENV_SERVICE_ACCOUNT_JSON]
        )
        rows: dict[str, int] = sheet.existing_keys()
        plan: AppendPlan = _reconcile_with_sheet(ranked_jobs, rows)
        appended: int = sheet.append(plan.to_append)
        url_candidates: list[Job] = [
            job for job in [*diff.changed, *diff.new] if job.key in rows
        ]
        updated: int = sheet.update_apply_urls(url_candidates, rows)
        sheet.sort_by_posting_date()
    except SheetError:
        logger.exception("Sheet update failed, state left untouched for the next run")
        return ExitCode.SHEET_ERROR
    logger.info("Appended %d rows, refreshed %d application URLs", appended, updated)
    digest: str = render_digest(plan.to_append, updated)
    write_step_summary(digest)
    print(digest)
    # Three runs a day means three digests a day, and most of them find nothing. Only a
    # run with something to report earns an email; the step summary still records every
    # run, so a quiet run is visible in Actions without landing in the inbox.
    if plan.to_append or updated:
        _send_email_best_effort(digest)
    else:
        logger.info("No new postings and no changed URLs, skipping the email")
    store.record(ranked_jobs)
    store.save(state_path)
    return ExitCode.OK


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT
    )
    missing: list[str] = _missing_env(SHEET_ENV_VARS)
    if not args.dry_run and missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        return ExitCode.CONFIG_ERROR
    found_at: date = datetime.now(PACIFIC).date()
    try:
        fetched_jobs: list[Job] = fetch_all(found_at)
    except SourceError:
        logger.exception("Cannot read the pooled internship feed")
        return ExitCode.NO_SOURCES
    ranked_jobs: list[Job] = triage(fetched_jobs)
    try:
        store: Store = Store.load(args.state)
    except StateError:
        logger.exception("Cannot read state file %s, refusing to overwrite it", args.state)
        return ExitCode.CONFIG_ERROR
    diff: Diff = store.diff(ranked_jobs)
    if args.dry_run:
        print(render_digest(diff.new, len(diff.changed)))
        return ExitCode.OK
    return _publish(diff, ranked_jobs, store, args.state)


if __name__ == "__main__":
    sys.exit(main())
