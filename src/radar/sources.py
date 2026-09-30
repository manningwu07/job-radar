"""Fetch and normalize jobs from the pooled internship feed."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from radar.models import Job, Platform, Season, Source, join_locations, normalize

HTTP_TIMEOUT_SECONDS = 30
HTTP_RETRY_TOTAL = 3
HTTP_RETRY_BACKOFF_SECONDS = 0.5
HTTP_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
HTTP_RETRY_METHODS = frozenset(("GET",))
USER_AGENT = "job-radar/1.0 (+personal internship search)"
DEFAULT_LOCATION = "Unspecified"
POOL_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "Summer2027-Internships/dev/.github/scripts/listings.json"
)
MAX_FEED_AGE_DAYS: int = 14
GREENHOUSE_JOB_ID_PARAMETER = "gh_jid"
WORKDAY_HOST_SUFFIX = ".myworkdayjobs.com"
PLATFORM_BY_HOST: dict[str, Platform] = {
    "boards.greenhouse.io": Platform.GREENHOUSE,
    "job-boards.greenhouse.io": Platform.GREENHOUSE,
    "jobs.lever.co": Platform.LEVER,
    "jobs.ashbyhq.com": Platform.ASHBY,
    "jobs.smartrecruiters.com": Platform.SMARTRECRUITERS,
}
REMOTE_PATTERN = re.compile(r"\b(remote|work from home|distributed)\b", re.IGNORECASE)
CORPORATE_SUFFIX_PATTERN = re.compile(
    r"(?:,\s*|\s+)(?:inc\.?|llc|corporation|corp\.?|technologies|ltd\.?|co\.?)$",
    re.IGNORECASE,
)
COMPANY_ALIASES: dict[str, str] = {
    normalize("Alphabet"): "Google",
    normalize("Google DeepMind"): "DeepMind",
    normalize("Scale"): "Scale AI",
    normalize("X.AI"): "xAI",
    normalize("Amazon.com"): "Amazon",
    normalize("Amazon Web Services"): "Amazon",
    normalize("AWS"): "Amazon",
    normalize("Aquatic Capital"): "Aquatic",
    normalize("AtomicSemi"): "Atomic Semi",
    normalize("fab2"): "Atomic Semi",
    normalize("Jump Trading Group"): "Jump Trading",
    normalize("SPREEAI"): "SpreeAI",
    normalize("Susquehanna Investment Group"): "Susquehanna",
    normalize("Virtu Financial"): "Virtu",
}


class SourceError(Exception):
    """Raised when the pooled source cannot be read."""


class PoolPosting(TypedDict, total=False):
    """Represent fields consumed from one pooled listing."""

    company_name: str
    company_url: str
    title: str
    date_posted: int
    date_updated: int
    degrees: list[str]
    terms: list[str]
    season: str
    active: bool
    url: str
    is_visible: bool
    locations: list[str]


@dataclass(frozen=True, slots=True)
class _JobFields:
    company: str
    role: str
    posted_at: date | None
    found_at: date
    apply_url: str
    careers_url: str
    platform: Platform
    season: Season | None
    terms: tuple[str, ...]
    degrees: tuple[str, ...]


def canonical_company(company: str) -> str:
    """Return a stable company name after removing legal suffixes."""
    stripped_company: str = company.strip().rstrip(" ,").strip()
    while stripped_company:
        without_suffix: str = CORPORATE_SUFFIX_PATTERN.sub("", stripped_company)
        if without_suffix == stripped_company:
            break
        stripped_company = without_suffix.rstrip(" ,").strip()
    return COMPANY_ALIASES.get(normalize(stripped_company), stripped_company)


def _parse_epoch_date(timestamp: int | None) -> date | None:
    if timestamp is None or isinstance(timestamp, bool):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC).date()
    except (OSError, OverflowError, ValueError):
        return None


def platform_from_application_url(apply_url: str) -> Platform:
    """Return the ATS identified by a parsed application URL."""
    parsed_url = urlparse(apply_url)
    hostname: str = parsed_url.hostname or ""
    known_platform: Platform | None = PLATFORM_BY_HOST.get(hostname)
    if known_platform is not None:
        return known_platform
    if hostname.endswith(WORKDAY_HOST_SUFFIX):
        return Platform.WORKDAY
    if GREENHOUSE_JOB_ID_PARAMETER in parse_qs(parsed_url.query):
        return Platform.GREENHOUSE
    return Platform.OTHER


def _parse_season_label(label: str) -> Season | None:
    words: set[str] = set(re.findall(r"[a-z]+", label.casefold()))
    matches: list[Season] = [
        season for season in Season if season.value.casefold() in words
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _posting_season(posting: PoolPosting) -> Season | None:
    labels: list[str] = list(posting.get("terms", []))
    stated_season: str | None = posting.get("season")
    if stated_season:
        labels.append(stated_season)
    recognized_seasons: set[Season] = {
        parsed_season
        for label in labels
        if (parsed_season := _parse_season_label(label)) is not None
    }
    if len(recognized_seasons) != 1:
        return None
    return next(iter(recognized_seasons))


def _is_remote(*posting_text: str) -> bool:
    return REMOTE_PATTERN.search(" ".join(posting_text)) is not None


def _build_job(fields: _JobFields, locations: Sequence[str]) -> Job:
    normalized_locations: list[str] = list(locations) or [DEFAULT_LOCATION]
    return Job(
        company=canonical_company(fields.company),
        role=fields.role,
        location=join_locations(normalized_locations),
        remote=_is_remote(*normalized_locations, fields.role),
        posted_at=fields.posted_at,
        found_at=fields.found_at,
        source=Source.POOL,
        apply_url=fields.apply_url,
        careers_url=fields.careers_url,
        platform=fields.platform,
        season=fields.season,
        terms=fields.terms,
        degrees=fields.degrees,
    )


def _pool_job(posting: PoolPosting, found_at: date) -> Job:
    apply_url: str = posting["url"]
    fields: _JobFields = _JobFields(
        company=posting["company_name"],
        role=posting["title"],
        posted_at=_parse_epoch_date(posting.get("date_posted")),
        found_at=found_at,
        apply_url=apply_url,
        careers_url=posting.get("company_url", ""),
        platform=platform_from_application_url(apply_url),
        season=_posting_season(posting),
        terms=tuple(posting.get("terms", [])),
        degrees=tuple(posting.get("degrees", [])),
    )
    return _build_job(fields, posting.get("locations", [DEFAULT_LOCATION]))


def fetch_pool(session: requests.Session, found_at: date) -> list[Job]:
    """Fetch active visible internship listings from the pooled feed."""
    response: requests.Response = session.get(POOL_URL, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload: object = response.json()
    if not isinstance(payload, list) or not payload:
        raise ValueError("Pooled internship feed must return a nonempty JSON list")
    postings: list[PoolPosting] = cast(list[PoolPosting], payload)
    update_dates: list[date] = []
    posting: PoolPosting
    for posting in postings:
        updated_at: date | None = _parse_epoch_date(
            posting.get("date_updated", posting.get("date_posted"))
        )
        if updated_at is not None:
            update_dates.append(updated_at)
    if not update_dates:
        raise ValueError("Pooled internship feed contains no valid update dates")
    latest_update: date = max(update_dates)
    if (found_at - latest_update).days > MAX_FEED_AGE_DAYS:
        raise ValueError(
            f"Pooled internship feed is stale: latest update {latest_update}, "
            f"more than {MAX_FEED_AGE_DAYS} days old"
        )
    jobs: list[Job] = [
        _pool_job(posting, found_at)
        for posting in postings
        if posting.get("active")
        and posting.get("is_visible")
    ]
    if not jobs:
        raise ValueError("Pooled internship feed contains no active internship listings")
    return jobs


def _build_session() -> requests.Session:
    retry: Retry = Retry(
        total=HTTP_RETRY_TOTAL,
        connect=HTTP_RETRY_TOTAL,
        read=HTTP_RETRY_TOTAL,
        status=HTTP_RETRY_TOTAL,
        allowed_methods=HTTP_RETRY_METHODS,
        status_forcelist=HTTP_RETRY_STATUS_CODES,
        backoff_factor=HTTP_RETRY_BACKOFF_SECONDS,
    )
    adapter: HTTPAdapter = HTTPAdapter(max_retries=retry)
    session: requests.Session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_all(
    found_at: date, session: requests.Session | None = None
) -> list[Job]:
    """Fetch the pooled listings and fail loudly if the pool is unavailable."""
    owns_session: bool = session is None
    active_session: requests.Session = session if session is not None else _build_session()
    try:
        try:
            return fetch_pool(active_session, found_at)
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            raise SourceError(f"Pooled internship feed failed: {error}") from error
    finally:
        if owns_session:
            active_session.close()
