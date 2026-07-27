"""Notification layer: plain-text digest, GitHub step summary, Gmail delivery."""

import logging
import os
import re
import smtplib
from collections.abc import Sequence
from email.message import EmailMessage

from radar.models import LOCATION_SEPARATOR, Job

logger = logging.getLogger(__name__)

TOP_N = 5

NO_JOBS_LINE = "No new jobs today."
FOUND_LINE = "Found: %d new %s"
TOP_HEADING = "Top %d:"
UPDATED_LINE = "Updated %d existing rows with new application URLs."
HEADLINE_WITH_LOCATION = "%s - %s (%s)"
HEADLINE_WITHOUT_LOCATION = "%s - %s"
URL_INDENT = "   "
MAX_LOCATION_CHARS = 60
LOCATION_ELLIPSIS = "..."

STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465
SMTP_TIMEOUT_SECONDS = 30
SUBJECT_NO_JOBS = "job-radar: no new jobs"
SUBJECT_WITH_COUNT = "job-radar: %d new %s"

_FOUND_COUNT = re.compile(r"^Found: (\d+) new job", re.MULTILINE)


def _plural(count: int, singular: str, plural: str) -> str:
    if count == 1:
        return singular
    return plural


def _display_location(location: str) -> str:
    """Shorten a joined location list to keep one digest line scannable.

    Display only - the sheet cell keeps the full value.
    """
    if len(location) <= MAX_LOCATION_CHARS:
        return location
    boundary = location.rfind(LOCATION_SEPARATOR, 0, MAX_LOCATION_CHARS)
    if boundary > 0:
        return location[:boundary] + LOCATION_SEPARATOR + LOCATION_ELLIPSIS
    return location[:MAX_LOCATION_CHARS].rstrip() + LOCATION_ELLIPSIS


def _headline(job: Job) -> str:
    if job.location:
        return HEADLINE_WITH_LOCATION % (job.company, job.role, _display_location(job.location))
    return HEADLINE_WITHOUT_LOCATION % (job.company, job.role)


def render_digest(new_jobs: Sequence[Job], updated_count: int) -> str:
    """Render the plain-text digest read in both a terminal and an email body."""
    lines: list[str] = []
    if new_jobs:
        shown = list(new_jobs[:TOP_N])
        lines.append(FOUND_LINE % (len(new_jobs), _plural(len(new_jobs), "job", "jobs")))
        lines.append("")
        lines.append(TOP_HEADING % len(shown))
        for position, job in enumerate(shown, start=1):
            lines.append(f"{position}. {_headline(job)}")
            lines.append(URL_INDENT + job.apply_url)
    else:
        lines.append(NO_JOBS_LINE)

    if updated_count > 0:
        lines.append("")
        lines.append(UPDATED_LINE % updated_count)
    return "\n".join(lines)


def write_step_summary(digest: str) -> None:
    """Append the digest to the GitHub step summary file, if the runner set one."""
    path = os.environ.get(STEP_SUMMARY_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(digest + "\n")
    except OSError as exc:
        logger.warning("Could not write step summary to %s: %s", path, exc)


def _subject(digest: str) -> str:
    match = _FOUND_COUNT.search(digest)
    if match is None:
        return SUBJECT_NO_JOBS
    count = int(match.group(1))
    return SUBJECT_WITH_COUNT % (count, _plural(count, "internship", "internships"))


def send_email(digest: str, smtp_user: str, smtp_password: str, recipient: str) -> None:
    """Send the digest over Gmail SMTP over SSL. Transport errors propagate."""
    message = EmailMessage()
    message["From"] = smtp_user
    message["To"] = recipient
    message["Subject"] = _subject(digest)
    message.set_content(digest)

    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)
    logger.info("Sent digest email to %s", recipient)
