import logging
import re
from datetime import date

import pytest

from radar import notify
from radar.models import LOCATION_SEPARATOR, Job, Platform, Source, join_locations

SMTP_USER = "radar@example.com"
SMTP_PASSWORD = "hunter2-app-password"
RECIPIENT = "me@example.com"
OVERFLOW_MARKER = re.compile(r"\+\d+ more")
HEADLINE_LINE = 3


def shown_location(digest):
    headline = digest.splitlines()[HEADLINE_LINE]
    return headline.rsplit(" (", 1)[1][:-1]


def make_job(company, role, apply_url, location="Mountain View, CA"):
    return Job(
        company=company,
        role=role,
        location=location,
        remote=False,
        posted_at=date(2026, 7, 1),
        found_at=date(2026, 7, 27),
        source=Source.GREENHOUSE,
        apply_url=apply_url,
        careers_url="https://example.com/careers",
        platform=Platform.GREENHOUSE,
    )


def make_jobs(count):
    return [
        make_job(f"Company{index}", "Software Engineering Intern", f"https://jobs/{index}")
        for index in range(1, count + 1)
    ]


def test_render_digest_multiple_jobs_exact_output():
    jobs = [
        make_job("Google", "Software Engineering Intern", "https://jobs.example.com/1"),
        make_job(
            "Anthropic", "ML Systems Intern", "https://jobs.example.com/2", "San Francisco, CA"
        ),
        make_job("Stripe", "Backend Infrastructure Intern", "https://jobs.example.com/3", "Remote"),
    ]

    digest = notify.render_digest(jobs, 0)

    assert digest == (
        "Found: 3 new jobs\n"
        "\n"
        "Top 3:\n"
        "1. Google - Software Engineering Intern (Mountain View, CA)\n"
        "   https://jobs.example.com/1\n"
        "2. Anthropic - ML Systems Intern (San Francisco, CA)\n"
        "   https://jobs.example.com/2\n"
        "3. Stripe - Backend Infrastructure Intern (Remote)\n"
        "   https://jobs.example.com/3"
    )


def test_render_digest_lists_distinct_postings_with_joined_locations():
    roles = [
        "Software Engineer Intern",
        "Software Engineer Intern, Defense Tech",
        "Forward Deployed Software Engineer Intern",
        "Forward Deployed Infrastructure Engineer Intern",
        "Product Infrastructure Intern",
    ]
    jobs = [
        make_job(
            "Palantir",
            role,
            f"https://jobs.lever.co/palantir/{index}",
            join_locations(["Palo Alto, CA", "New York, NY", f"City{index}, TX"]),
        )
        for index, role in enumerate(roles, start=1)
    ]

    digest = notify.render_digest(jobs, 0)
    urls = [line.strip() for line in digest.splitlines() if line.strip().startswith("https")]
    headlines = [line for line in digest.splitlines() if line[:1].isdigit()]

    assert len({job.key for job in jobs}) == notify.TOP_N
    assert len(set(urls)) == notify.TOP_N
    assert len(set(headlines)) == notify.TOP_N
    assert (
        "1. Palantir - Software Engineer Intern (Palo Alto, CA; New York, NY; City1, TX)" in digest
    )


def test_render_digest_location_at_the_truncation_boundary_stays_intact():
    head = join_locations(["Mountain View, CA", "Bellevue, WA"])
    filler = "X" * (notify.MAX_LOCATION_CHARS - len(head) - len(LOCATION_SEPARATOR))
    location = head + LOCATION_SEPARATOR + filler
    job = make_job("Google", "SWE Intern", "https://jobs/1", location)

    assert len(location) == notify.MAX_LOCATION_CHARS
    assert shown_location(notify.render_digest([job], 0)) == location


def test_render_digest_location_one_char_past_the_boundary_is_cut_at_a_separator():
    head = join_locations(["Mountain View, CA", "Bellevue, WA"])
    filler = "X" * (notify.MAX_LOCATION_CHARS - len(head) - len(LOCATION_SEPARATOR) + 1)
    location = head + LOCATION_SEPARATOR + filler
    job = make_job("Google", "SWE Intern", "https://jobs/1", location)

    assert len(location) == notify.MAX_LOCATION_CHARS + 1
    assert shown_location(notify.render_digest([job], 0)) == (
        head + LOCATION_SEPARATOR + notify.LOCATION_ELLIPSIS
    )


def test_render_digest_drops_the_overflow_marker_whole_when_truncating():
    location = join_locations(
        [
            "San Francisco Bay Area, California",
            "Greater Seattle Area, Washington",
            "New York City Metro, New York",
            "Boston, MA",
        ]
    )
    job = make_job("Google", "SWE Intern", "https://jobs/1", location)

    shown = shown_location(notify.render_digest([job], 0))

    assert location.endswith("+1 more")
    assert "+" not in shown
    assert "more" not in shown
    assert shown.endswith(LOCATION_SEPARATOR + notify.LOCATION_ELLIPSIS)


@pytest.mark.parametrize("city_count", range(1, 10))
def test_rendered_location_never_nests_or_splits_overflow_markers(city_count):
    location = join_locations([f"City{index}, ST" for index in range(city_count)])
    job = make_job("Google", "SWE Intern", "https://jobs/1", location)

    shown = shown_location(notify.render_digest([job], 0))
    markers = [
        segment
        for segment in shown.split(LOCATION_SEPARATOR)
        if "+" in segment or "more" in segment
    ]

    assert len(markers) <= 1
    assert all(OVERFLOW_MARKER.fullmatch(marker) for marker in markers)


def test_render_digest_does_not_dedupe_identical_postings():
    duplicate = make_job("Google", "Software Engineering Intern", "https://jobs/1")

    digest = notify.render_digest([duplicate, duplicate], 0)

    assert digest.startswith("Found: 2 new jobs")
    assert digest.count("https://jobs/1") == 2


def test_render_digest_keeps_short_joined_location_intact():
    location = join_locations(["Mountain View, CA", "Bellevue, WA", "Austin, TX", "Chicago, IL"])
    job = make_job("Google", "SWE Intern", "https://jobs/1", location)

    assert len(location) <= notify.MAX_LOCATION_CHARS
    assert f"({location})" in notify.render_digest([job], 0)


def test_render_digest_truncates_long_location_at_separator():
    location = join_locations(
        [
            "San Francisco Bay Area, CA",
            "Greater Seattle Area, WA",
            "New York City Metro, NY",
            "Boston, MA",
            "Austin, TX",
        ]
    )
    job = make_job("Google", "SWE Intern", "https://jobs/1", location)

    digest = notify.render_digest([job], 0)

    assert len(location) > notify.MAX_LOCATION_CHARS
    assert "(San Francisco Bay Area, CA; Greater Seattle Area, WA; ...)" in digest
    assert location not in digest


def test_render_digest_truncates_long_location_without_separator():
    location = "Somewhereville In A Very Long Administrative Region Of A Country"
    job = make_job("Google", "SWE Intern", "https://jobs/1", location)

    shown = shown_location(notify.render_digest([job], 0))

    assert shown.endswith(notify.LOCATION_ELLIPSIS)
    assert len(shown) <= notify.MAX_LOCATION_CHARS + len(notify.LOCATION_ELLIPSIS)


def test_render_digest_no_jobs():
    assert notify.render_digest([], 0) == "No new jobs today."


def test_render_digest_appends_updated_rows_line():
    digest = notify.render_digest([], 3)

    assert digest == "No new jobs today.\n\nUpdated 3 existing rows with new application URLs."


def test_render_digest_omits_updated_line_when_zero():
    assert "Updated" not in notify.render_digest(make_jobs(2), 0)


def test_render_digest_truncates_to_top_n():
    digest = notify.render_digest(make_jobs(7), 0)
    lines = digest.splitlines()

    assert lines[0] == "Found: 7 new jobs"
    assert lines[2] == "Top 5:"
    assert "5. Company5" in digest
    assert "6. Company6" not in digest
    assert sum(1 for line in lines if line.startswith("https")) == 0
    assert sum(1 for line in lines if line.strip().startswith("https")) == notify.TOP_N


def test_render_digest_singular_job():
    assert notify.render_digest(make_jobs(1), 0).startswith("Found: 1 new job\n")


def test_write_step_summary_appends_to_file(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv(notify.STEP_SUMMARY_ENV, str(summary))

    notify.write_step_summary("first digest")
    notify.write_step_summary("second digest")

    assert summary.read_text(encoding="utf-8") == "first digest\nsecond digest\n"


def test_write_step_summary_no_ops_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv(notify.STEP_SUMMARY_ENV, raising=False)

    notify.write_step_summary("ignored")

    assert list(tmp_path.iterdir()) == []


def test_write_step_summary_warns_instead_of_raising(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(notify.STEP_SUMMARY_ENV, str(tmp_path))

    with caplog.at_level(logging.WARNING):
        notify.write_step_summary("digest")

    assert "Could not write step summary" in caplog.text


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.login_user = None
        self.login_password = None
        self.message = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True
        return False

    def login(self, user, password):
        self.login_user = user
        self.login_password = password

    def send_message(self, message):
        self.message = message


class FakeSMTPFactory:
    def __init__(self):
        self.instances = []

    def __call__(self, host, port, timeout=None):
        instance = FakeSMTP(host, port, timeout)
        self.instances.append(instance)
        return instance


def install_fake_smtp(monkeypatch):
    factory = FakeSMTPFactory()
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", factory)
    return factory


def test_send_email_builds_message_and_closes_connection(monkeypatch):
    factory = install_fake_smtp(monkeypatch)
    digest = notify.render_digest(make_jobs(12), 0)

    notify.send_email(digest, SMTP_USER, SMTP_PASSWORD, RECIPIENT)

    smtp = factory.instances[0]
    assert (smtp.host, smtp.port) == (notify.GMAIL_SMTP_HOST, notify.GMAIL_SMTP_PORT)
    assert smtp.timeout == notify.SMTP_TIMEOUT_SECONDS
    assert (smtp.login_user, smtp.login_password) == (SMTP_USER, SMTP_PASSWORD)
    assert smtp.closed
    assert smtp.message["Subject"] == "job-radar: 12 new internships"
    assert smtp.message["To"] == RECIPIENT
    assert smtp.message["From"] == SMTP_USER
    assert smtp.message.get_content() == digest + "\n"


def test_send_email_subject_for_single_job(monkeypatch):
    factory = install_fake_smtp(monkeypatch)

    notify.send_email(notify.render_digest(make_jobs(1), 0), SMTP_USER, SMTP_PASSWORD, RECIPIENT)

    assert factory.instances[0].message["Subject"] == "job-radar: 1 new internship"


def test_send_email_subject_for_no_jobs(monkeypatch):
    factory = install_fake_smtp(monkeypatch)

    notify.send_email(notify.render_digest([], 2), SMTP_USER, SMTP_PASSWORD, RECIPIENT)

    assert factory.instances[0].message["Subject"] == "job-radar: no new jobs"


def test_send_email_does_not_leak_password(monkeypatch, caplog):
    factory = install_fake_smtp(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        notify.send_email(
            notify.render_digest(make_jobs(2), 0), SMTP_USER, SMTP_PASSWORD, RECIPIENT
        )

    assert SMTP_PASSWORD not in factory.instances[0].message.as_string()
    assert SMTP_PASSWORD not in caplog.text
