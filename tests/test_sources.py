"""Offline tests for ATS and structured-listing normalization."""

import json
import logging
from datetime import date
from pathlib import Path

import pytest
import requests

from radar.boards import Board
from radar.models import Platform, Season, Source
from radar.sources import (
    fetch_all,
    fetch_board,
    fetch_simplify,
    platform_from_application_url,
)

FIXTURES_DIRECTORY = Path(__file__).parent / "fixtures"
FOUND_AT = date(2026, 7, 27)
CAREERS_URL = "https://example.com/careers"
NVIDIA_TOKEN = "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite"
PLATFORM_URL_CASES = (
    (
        "https://boards.greenhouse.io/example/jobs/123",
        Platform.GREENHOUSE,
    ),
    (
        "https://job-boards.greenhouse.io/example/jobs/123",
        Platform.GREENHOUSE,
    ),
    ("https://jobs.lever.co/example/123", Platform.LEVER),
    ("https://jobs.ashbyhq.com/example/123", Platform.ASHBY),
    (
        "https://jobs.smartrecruiters.com/example/123",
        Platform.SMARTRECRUITERS,
    ),
    (
        "https://example.wd5.myworkdayjobs.com/en-US/jobs/job/123",
        Platform.WORKDAY,
    ),
    (
        "https://databricks.com/company/careers/open-positions/job?gh_jid=7011263002",
        Platform.GREENHOUSE,
    ),
    (
        "https://stripe.com/jobs/search?gh_jid=8031833",
        Platform.GREENHOUSE,
    ),
    ("https://jobs.apple.com/en-us/details/123", Platform.OTHER),
    ("https://www.tesla.com/careers/search/job/123", Platform.OTHER),
    ("https://www.amazon.jobs/en/jobs/123", Platform.OTHER),
    ("https://www.google.com/about/careers/jobs/123", Platform.OTHER),
    (
        "https://example.com/companies/lever/jobs.ashbyhq.com/123",
        Platform.OTHER,
    ),
)


def _fixture(name: str) -> object:
    return json.loads((FIXTURES_DIRECTORY / name).read_text(encoding="utf-8"))


class FakeResponse:
    """Return a fixed decoded JSON payload."""

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        """Model a successful HTTP response."""

    def json(self) -> object:
        """Return the configured response payload."""
        return self.payload


class FakeSession:
    """Return queued responses without performing network requests."""

    def __init__(
        self,
        get_payloads: list[object] | None = None,
        post_payloads: list[object] | None = None,
    ) -> None:
        self.get_payloads = list(get_payloads or [])
        self.post_payloads = list(post_payloads or [])

    def get(self, url: str, **request_options: object) -> FakeResponse:
        """Return the next queued GET response."""
        return FakeResponse(self.get_payloads.pop(0))

    def post(self, url: str, **request_options: object) -> FakeResponse:
        """Return the next queued POST response."""
        return FakeResponse(self.post_payloads.pop(0))


class RoutingSession:
    """Fail one board while serving another board and Simplify."""

    def __init__(self) -> None:
        self.greenhouse_payload = _fixture("greenhouse.json")

    def get(self, url: str, **request_options: object) -> FakeResponse:
        """Route requests by URL without network access."""
        if "broken" in url:
            raise requests.ConnectionError("offline fixture failure")
        if "listings.json" in url:
            return FakeResponse([])
        return FakeResponse(self.greenhouse_payload)


def _board(company: str, platform: Platform, token: str) -> Board:
    return Board(company, platform, token, CAREERS_URL, 1)


@pytest.mark.parametrize(("apply_url", "expected_platform"), PLATFORM_URL_CASES)
def test_platform_resolution_uses_parsed_host(
    apply_url: str, expected_platform: Platform
) -> None:
    assert platform_from_application_url(apply_url) is expected_platform


def test_greenhouse_normalizes_remote_and_multi_location() -> None:
    board = _board("Anthropic", Platform.GREENHOUSE, "anthropic")
    jobs = fetch_board(
        board,
        FakeSession(get_payloads=[_fixture("greenhouse.json")]),
        FOUND_AT,
    )

    assert len(jobs) == 3
    assert jobs[0].location == ("San Francisco, CA; New York City, NY; Seattle, WA")
    assert jobs[0].posted_at == date(2026, 7, 14)
    assert jobs[1].location == (
        "London, UK; Ontario, CAN; Remote-Friendly, United States; +1 more"
    )
    assert jobs[1].remote
    assert jobs[2].location == "Unspecified"
    assert all(job.source is Source.GREENHOUSE for job in jobs)
    assert all(job.season is None for job in jobs)


def test_greenhouse_missing_date_becomes_none() -> None:
    payload = _fixture("greenhouse.json")
    assert isinstance(payload, dict)
    postings = payload["jobs"]
    assert isinstance(postings, list)
    posting = postings[0]
    assert isinstance(posting, dict)
    posting.pop("updated_at")

    jobs = fetch_board(
        _board("Anthropic", Platform.GREENHOUSE, "anthropic"),
        FakeSession(get_payloads=[payload]),
        FOUND_AT,
    )

    assert jobs[0].posted_at is None


def test_lever_normalizes_timestamp_and_remote_workplace() -> None:
    jobs = fetch_board(
        _board("Palantir Technologies", Platform.LEVER, "palantir"),
        FakeSession(get_payloads=[_fixture("lever.json")]),
        FOUND_AT,
    )

    assert len(jobs) == 2
    assert all(job.company == "Palantir" for job in jobs)
    assert jobs[0].remote
    assert jobs[0].posted_at == date(2025, 6, 17)
    assert jobs[0].apply_url.endswith("/apply")
    assert jobs[1].location == "Foster City, CA"


def test_ashby_joins_secondary_locations() -> None:
    jobs = fetch_board(
        _board("OpenAI", Platform.ASHBY, "openai"),
        FakeSession(get_payloads=[_fixture("ashby.json")]),
        FOUND_AT,
    )

    assert len(jobs) == 2
    assert jobs[0].location == "San Francisco; New York City; Seattle"
    assert jobs[1].remote
    assert jobs[0].posted_at == date(2025, 3, 27)


def test_smartrecruiters_builds_public_application_url() -> None:
    jobs = fetch_board(
        _board("Wise", Platform.SMARTRECRUITERS, "Wise"),
        FakeSession(get_payloads=[_fixture("smartrecruiters.json")]),
        FOUND_AT,
    )

    assert len(jobs) == 2
    assert jobs[0].location == "São Paulo, , Brazil"
    assert jobs[0].posted_at == date(2026, 7, 27)
    assert jobs[0].apply_url == (
        "https://jobs.smartrecruiters.com/Wise/744000140064348"
    )


def test_workday_normalizes_relative_dates() -> None:
    jobs = fetch_board(
        _board("NVIDIA", Platform.WORKDAY, NVIDIA_TOKEN),
        FakeSession(post_payloads=[_fixture("workday.json")]),
        FOUND_AT,
    )

    assert len(jobs) == 2
    assert jobs[0].posted_at == date(2026, 7, 24)
    assert jobs[1].posted_at == date(2026, 7, 23)
    assert jobs[0].apply_url.startswith("https://nvidia.wd5.myworkdayjobs.com/en-US/")


def test_simplify_skips_inactive_and_joins_locations() -> None:
    jobs = fetch_simplify(
        FakeSession(get_payloads=[_fixture("simplify.json")]), FOUND_AT
    )

    assert len(jobs) == 6
    assert jobs[0].location == "New York, NY; San Francisco, CA"
    google_job = next(job for job in jobs if job.company == "Google")
    assert google_job.location == (
        "Mountain View, CA; Atlanta, GA; Austin, TX; +27 more"
    )
    palantir_job = next(job for job in jobs if job.role.startswith("Forward Deployed"))
    assert palantir_job.company == "Palantir"
    assert palantir_job.source is Source.SIMPLIFY
    assert palantir_job.platform is Platform.LEVER
    assert google_job.source is Source.SIMPLIFY
    assert google_job.platform is Platform.OTHER
    etched_job = next(job for job in jobs if job.company == "Etched")
    assert etched_job.platform is Platform.ASHBY
    seasons_by_role = {job.role: job.season for job in jobs}
    assert seasons_by_role["Frontend Software Engineer Intern"] is Season.WINTER
    assert seasons_by_role["Software Engineering Intern"] is Season.SUMMER
    assert (
        seasons_by_role["Forward Deployed Infrastructure Engineer Intern"]
        is Season.FALL
    )
    assert seasons_by_role["Supercomputing Intern"] is Season.SPRING
    assert seasons_by_role["Platform Engineering Intern"] is None


def test_simplify_parses_season_from_terms() -> None:
    payload = _fixture("simplify.json")
    assert isinstance(payload, list)
    etched_posting = next(
        posting
        for posting in payload
        if isinstance(posting, dict) and posting.get("company_name") == "Etched"
    )
    etched_posting.pop("season")
    etched_posting["terms"] = ["Spring 2027"]

    jobs = fetch_simplify(FakeSession(get_payloads=[payload]), FOUND_AT)
    etched_job = next(job for job in jobs if job.company == "Etched")

    assert etched_job.season is Season.SPRING


def test_fetch_all_skips_a_failing_board(
    caplog: pytest.LogCaptureFixture,
) -> None:
    boards = [
        _board("Broken", Platform.GREENHOUSE, "broken"),
        _board("Anthropic", Platform.GREENHOUSE, "anthropic"),
    ]

    with caplog.at_level(logging.WARNING):
        jobs = fetch_all(boards, FOUND_AT, session=RoutingSession())

    assert len(jobs) == 3
    assert "Skipping Broken board" in caplog.text
