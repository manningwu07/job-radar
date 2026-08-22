"""Offline tests for ATS and structured-listing normalization."""

import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests

from radar import sources
from radar.boards import Board
from radar.models import Platform, Season, Source
from radar.sources import (
    WORKDAY_PAGE_SIZE,
    fetch_all,
    fetch_board,
    fetch_simplify,
    platform_from_application_url,
)

FIXTURES_DIRECTORY = Path(__file__).parent / "fixtures"
FOUND_AT = date(2026, 7, 27)
CAREERS_URL = "https://example.com/careers"
NVIDIA_TOKEN = "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite"
AMD_HOST = "careers.amd.com"
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


class RecordingSession:
    """Return queued responses and keep the request options each call was made with."""

    def __init__(
        self,
        get_payloads: list[object] | None = None,
        post_payloads: list[object] | None = None,
    ) -> None:
        self.get_payloads = list(get_payloads or [])
        self.post_payloads = list(post_payloads or [])
        self.get_calls: list[dict[str, object]] = []
        self.post_calls: list[dict[str, object]] = []

    def get(self, url: str, **request_options: object) -> FakeResponse:
        """Record a GET and return the next queued response."""
        self.get_calls.append(request_options)
        return FakeResponse(self.get_payloads.pop(0))

    def post(self, url: str, **request_options: object) -> FakeResponse:
        """Record a POST and return the next queued response."""
        self.post_calls.append(request_options)
        return FakeResponse(self.post_payloads.pop(0))


def _workday_page(count: int, total: int) -> dict[str, object]:
    return {
        "total": total,
        "jobPostings": [
            {
                "title": f"Software Engineer Intern {index}",
                "externalPath": f"/job/Santa-Clara/Intern_JR{index}",
                "locationsText": "Santa Clara, CA",
                "postedOn": "Posted Today",
            }
            for index in range(count)
        ],
    }


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


@pytest.fixture(autouse=True)
def _no_paging_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the polite delay between pages so paging tests stay fast."""
    monkeypatch.setattr(sources.time, "sleep", lambda _seconds: None)


def test_workday_pages_past_the_old_five_page_ceiling() -> None:
    """A board with more than 100 matches is read whole, not truncated at 100."""
    session = RecordingSession(
        post_payloads=[_workday_page(WORKDAY_PAGE_SIZE, 150) for _ in range(7)]
        + [_workday_page(10, 150)]
    )

    jobs = fetch_board(_board("NVIDIA", Platform.WORKDAY, NVIDIA_TOKEN), session, FOUND_AT)

    assert len(jobs) == 150
    assert len(session.post_calls) == 8
    offsets = [call["json"]["offset"] for call in session.post_calls]
    assert offsets == [0, 20, 40, 60, 80, 100, 120, 140]


def test_workday_stops_at_the_reported_total_rather_than_an_empty_page() -> None:
    """An offset past the end serves rows again, so the total is what ends the walk."""
    session = RecordingSession(
        post_payloads=[_workday_page(WORKDAY_PAGE_SIZE, 40) for _ in range(4)]
    )

    jobs = fetch_board(_board("NVIDIA", Platform.WORKDAY, NVIDIA_TOKEN), session, FOUND_AT)

    assert len(jobs) == 40
    assert len(session.post_calls) == 2


def test_workday_stops_on_a_short_page_when_no_total_is_reported() -> None:
    page = _workday_page(3, 0)
    del page["total"]
    session = RecordingSession(post_payloads=[page])

    jobs = fetch_board(_board("NVIDIA", Platform.WORKDAY, NVIDIA_TOKEN), session, FOUND_AT)

    assert len(jobs) == 3
    assert len(session.post_calls) == 1


def test_amd_reads_the_intern_category_from_its_own_board() -> None:
    session = RecordingSession(get_payloads=[_fixture("amd.json")])

    jobs = fetch_board(_board("AMD", Platform.AMD, AMD_HOST), session, FOUND_AT)

    assert len(jobs) == 3
    assert session.get_calls[0]["params"]["categories"] == sources.AMD_INTERN_CATEGORY
    assert jobs[0].company == "AMD"
    assert jobs[0].source is Source.AMD
    assert jobs[0].platform is Platform.AMD
    assert jobs[0].posted_at == date(2026, 8, 20)
    assert jobs[0].location == "Santa Clara, California; San Jose, California"
    assert jobs[0].apply_url == "https://careers.amd.com/careers-home/jobs/91176"
    assert jobs[1].location == (
        "Austin, Texas; Boxborough, Massachusetts; Fort Collins, Colorado; +3 more"
    )


def test_amd_pages_the_whole_board_when_the_category_matches_nothing() -> None:
    """AMD owns the category label, so an empty result must not silently mean no jobs."""
    session = RecordingSession(
        get_payloads=[
            {"data": {"jobs": [], "totalCount": 0}},
            _fixture("amd.json"),
        ]
    )

    jobs = fetch_board(_board("AMD", Platform.AMD, AMD_HOST), session, FOUND_AT)

    assert len(jobs) == 3
    assert "categories" in session.get_calls[0]["params"]
    assert "categories" not in session.get_calls[1]["params"]


def test_amd_fallback_stops_once_results_predate_the_lookback() -> None:
    """The board answers newest first, so the walk ends at the first stale posting."""
    stale = FOUND_AT - timedelta(days=sources.AMD_LOOKBACK_DAYS + 1)
    fresh_page = {
        "data": {
            "jobs": [
                {
                    "data": {
                        "slug": str(index),
                        "title": "Software Engineer Intern",
                        "posted_date": FOUND_AT.isoformat(),
                        "full_location": "Santa Clara, California",
                    }
                }
                for index in range(sources.AMD_PAGE_SIZE - 1)
            ]
            + [
                {
                    "data": {
                        "slug": "stale",
                        "title": "Software Engineer Intern",
                        "posted_date": stale.isoformat(),
                        "full_location": "Santa Clara, California",
                    }
                }
            ]
        }
    }
    session = RecordingSession(
        get_payloads=[{"data": {"jobs": []}}, fresh_page, fresh_page]
    )

    jobs = fetch_board(_board("AMD", Platform.AMD, AMD_HOST), session, FOUND_AT)

    assert len(jobs) == sources.AMD_PAGE_SIZE - 1
    assert len(session.get_calls) == 2


def test_amd_accepts_a_response_without_the_data_envelope() -> None:
    payload = _fixture("amd.json")["data"]
    session = RecordingSession(get_payloads=[payload])

    jobs = fetch_board(_board("AMD", Platform.AMD, AMD_HOST), session, FOUND_AT)

    assert len(jobs) == 3


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
