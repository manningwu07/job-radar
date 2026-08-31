"""Offline tests for pooled-listing normalization."""

import json
from datetime import date
from pathlib import Path

import pytest
import requests

from radar.models import Job, Platform, Season, Source
from radar.sources import (
    SourceError,
    canonical_company,
    fetch_all,
    fetch_pool,
    platform_from_application_url,
)

FIXTURES_DIRECTORY = Path(__file__).parent / "fixtures"
FOUND_AT = date(2026, 7, 27)
PLATFORM_URL_CASES = (
    ("https://boards.greenhouse.io/example/jobs/123", Platform.GREENHOUSE),
    ("https://job-boards.greenhouse.io/example/jobs/123", Platform.GREENHOUSE),
    ("https://jobs.lever.co/example/123", Platform.LEVER),
    ("https://jobs.ashbyhq.com/example/123", Platform.ASHBY),
    ("https://jobs.smartrecruiters.com/example/123", Platform.SMARTRECRUITERS),
    (
        "https://example.wd5.myworkdayjobs.com/en-US/jobs/job/123",
        Platform.WORKDAY,
    ),
    (
        "https://stripe.com/jobs/search?gh_jid=8031833",
        Platform.GREENHOUSE,
    ),
    ("https://jobs.apple.com/en-us/details/123", Platform.OTHER),
)
COMPANY_CASES = (
    ("Palantir Technologies", "Palantir"),
    ("Google LLC", "Google"),
    ("NVIDIA Corporation", "NVIDIA"),
    ("Apple Inc.", "Apple"),
    ("Apex Technology, Inc.", "Apex Technology"),
    ("Example Corp,", "Example"),
    ("Jump Trading Group", "Jump Trading"),
    ("Aquatic Capital", "Aquatic"),
    ("Virtu Financial", "Virtu"),
)


def _fixture(name: str) -> object:
    return json.loads((FIXTURES_DIRECTORY / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload: object = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class FakeSession:
    def __init__(self, payload: object | None = None, error: Exception | None = None) -> None:
        self.payload: object | None = payload
        self.error: Exception | None = error
        self.closed: bool = False

    def get(self, url: str, **request_options: object) -> FakeResponse:
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("apply_url", "expected_platform"), PLATFORM_URL_CASES)
def test_platform_resolution_uses_parsed_host(
    apply_url: str, expected_platform: Platform
) -> None:
    assert platform_from_application_url(apply_url) is expected_platform


@pytest.mark.parametrize(("variant", "canonical_name"), COMPANY_CASES)
def test_company_names_drop_legal_suffixes(variant: str, canonical_name: str) -> None:
    assert canonical_company(variant) == canonical_name


def test_pool_skips_inactive_and_joins_locations() -> None:
    jobs = fetch_pool(FakeSession(_fixture("simplify.json")), FOUND_AT)

    assert len(jobs) == 6
    assert all(job.source is Source.POOL for job in jobs)
    assert jobs[0].posted_at == date(2025, 6, 7)
    google_job = next(job for job in jobs if job.company == "Google")
    assert google_job.location == (
        "Mountain View, CA; Atlanta, GA; Austin, TX; +27 more"
    )
    palantir_job = next(job for job in jobs if job.role.startswith("Forward Deployed"))
    assert palantir_job.platform is Platform.LEVER
    etched_job = next(job for job in jobs if job.company == "Etched")
    assert etched_job.platform is Platform.ASHBY
    seasons_by_role = {job.role: job.season for job in jobs}
    assert seasons_by_role["Software Engineering Intern"] is Season.SUMMER
    assert seasons_by_role["Supercomputing Intern"] is Season.SPRING


def test_pool_parses_season_from_terms() -> None:
    payload = _fixture("simplify.json")
    assert isinstance(payload, list)
    etched_posting = next(
        posting
        for posting in payload
        if isinstance(posting, dict) and posting.get("company_name") == "Etched"
    )
    etched_posting.pop("season")
    etched_posting["terms"] = ["Spring 2027"]

    jobs: list[Job] = fetch_pool(FakeSession(payload), FOUND_AT)
    etched_job = next(job for job in jobs if job.company == "Etched")

    assert etched_job.season is Season.SPRING


def test_pool_keeps_coop_titles_without_the_word_intern() -> None:
    payload: object = _fixture("simplify.json")
    assert isinstance(payload, list)
    payload.append(
        {
            "active": True,
            "company_name": "Example",
            "company_url": "https://example.com/careers",
            "date_posted": 1784878468,
            "is_visible": True,
            "locations": ["New York, NY"],
            "season": "Spring",
            "title": "Software Engineering Co-op",
            "url": "https://example.com/jobs/software-engineering-coop",
        }
    )

    jobs: list[Job] = fetch_pool(FakeSession(payload), FOUND_AT)

    assert any(job.role == "Software Engineering Co-op" for job in jobs)


def test_fetch_all_fails_loudly_when_the_pool_is_unavailable() -> None:
    session = FakeSession(error=requests.ConnectionError("offline"))

    with pytest.raises(SourceError, match="Pooled internship feed failed"):
        fetch_all(FOUND_AT, session=session)


@pytest.mark.parametrize("payload", ([], {}, None))
def test_fetch_all_fails_loudly_when_the_pool_shape_is_invalid(payload: object) -> None:
    with pytest.raises(SourceError, match="nonempty JSON list"):
        fetch_all(FOUND_AT, session=FakeSession(payload))


def test_fetch_all_returns_the_general_pool() -> None:
    jobs = fetch_all(FOUND_AT, session=FakeSession(_fixture("simplify.json")))

    assert len(jobs) == 6
    assert {job.company for job in jobs} >= {"Google", "Etched", "Palantir"}
