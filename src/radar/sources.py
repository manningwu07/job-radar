"""Fetch and normalize jobs from supported public ATS APIs."""

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from radar.boards import Board, canonical_company
from radar.models import Job, Platform, Season, Source, join_locations

HTTP_TIMEOUT_SECONDS = 30
HTTP_RETRY_TOTAL = 3
HTTP_RETRY_BACKOFF_SECONDS = 0.5
HTTP_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
HTTP_RETRY_METHODS = frozenset(("GET", "POST"))
USER_AGENT = "job-radar/1.0 (+personal internship search)"
SMARTRECRUITERS_PAGE_SIZE = 100
SMARTRECRUITERS_PAGE_LIMIT = 5
WORKDAY_PAGE_SIZE = 20
WORKDAY_PAGE_LIMIT = 5
WORKDAY_TOKEN_PARTS = 3
MILLISECONDS_PER_SECOND = 1_000
DEFAULT_LOCATION = "Unspecified"
SIMPLIFY_ROLE_TERM = "intern"
SIMPLIFY_LISTINGS_URL = (
    "https://raw.githubusercontent.com/vanshb03/"
    "Summer2027-Internships/dev/.github/scripts/listings.json"
)
GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{token}?mode=json"
ASHBY_URL = (
    "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"
)
SMARTRECRUITERS_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
SMARTRECRUITERS_JOB_URL = "https://jobs.smartrecruiters.com/{token}/{job_id}"
WORKDAY_URL = "https://{host}/wday/cxs/{tenant}/{site}/jobs"
WORKDAY_JOB_URL = "https://{host}/en-US/{site}{external_path}"
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
LOCATION_SEPARATOR_PATTERN = re.compile(r"\s*(?:;|\||\n)\s*")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
WORKDAY_DAYS_PATTERN = re.compile(r"^Posted (\d+) Days? Ago$", re.IGNORECASE)
WORKDAY_TODAY = "posted today"
WORKDAY_YESTERDAY = "posted yesterday"

logger = logging.getLogger(__name__)


class GreenhouseLocation(TypedDict):
    """Represent a Greenhouse posting location."""

    name: str | None


class GreenhousePosting(TypedDict, total=False):
    """Represent fields consumed from a Greenhouse posting."""

    title: str
    absolute_url: str
    updated_at: str
    location: GreenhouseLocation
    content: str


class GreenhouseResponse(TypedDict):
    """Represent a Greenhouse board response."""

    jobs: list[GreenhousePosting]


class LeverCategories(TypedDict, total=False):
    """Represent fields consumed from Lever posting categories."""

    location: str
    allLocations: list[str]


class LeverPosting(TypedDict, total=False):
    """Represent fields consumed from a Lever posting."""

    text: str
    categories: LeverCategories
    createdAt: int
    hostedUrl: str
    applyUrl: str
    descriptionPlain: str
    workplaceType: str


class AshbySecondaryLocation(TypedDict, total=False):
    """Represent one Ashby secondary location."""

    location: str


class AshbyPosting(TypedDict, total=False):
    """Represent fields consumed from an Ashby posting."""

    title: str
    location: str
    secondaryLocations: list[AshbySecondaryLocation]
    publishedAt: str
    jobUrl: str
    applyUrl: str
    descriptionPlain: str
    workplaceType: str


class AshbyResponse(TypedDict):
    """Represent an Ashby board response."""

    jobs: list[AshbyPosting]


class SmartRecruitersLocation(TypedDict, total=False):
    """Represent a SmartRecruiters posting location."""

    fullLocation: str
    city: str
    remote: bool


class SmartRecruitersPosting(TypedDict, total=False):
    """Represent fields consumed from a SmartRecruiters posting."""

    id: str
    name: str
    releasedDate: str
    location: SmartRecruitersLocation


class SmartRecruitersResponse(TypedDict):
    """Represent a SmartRecruiters postings response."""

    content: list[SmartRecruitersPosting]


class SmartRecruitersParams(TypedDict):
    """Represent SmartRecruiters pagination parameters."""

    limit: int
    offset: int


class WorkdayPosting(TypedDict, total=False):
    """Represent fields consumed from a Workday posting."""

    title: str
    externalPath: str
    locationsText: str
    postedOn: str


class WorkdayResponse(TypedDict):
    """Represent a Workday search response."""

    jobPostings: list[WorkdayPosting]


class WorkdayRequest(TypedDict):
    """Represent a Workday search request body."""

    appliedFacets: dict[str, str]
    limit: int
    offset: int
    searchText: str


class SimplifyPosting(TypedDict, total=False):
    """Represent fields consumed from a structured internship listing."""

    company_name: str
    company_url: str
    title: str
    date_posted: int
    terms: list[str]
    season: str
    active: bool
    url: str
    is_visible: bool
    locations: list[str]
    sponsorship: str


@dataclass(frozen=True, slots=True)
class _JobFields:
    company: str
    role: str
    posted_at: date | None
    found_at: date
    source: Source
    apply_url: str
    careers_url: str
    platform: Platform
    notes: str
    description: str
    season: Season | None = None


def _parse_iso_date(timestamp: str | None) -> date | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).date()
    except ValueError:
        return None


def _parse_epoch_date(timestamp: int | None, divisor: int = 1) -> date | None:
    if timestamp is None or isinstance(timestamp, bool):
        return None
    try:
        return datetime.fromtimestamp(timestamp / divisor, tz=UTC).date()
    except (OSError, OverflowError, ValueError):
        return None


def _parse_workday_date(posted_on: str | None, found_at: date) -> date | None:
    if not posted_on:
        return None
    normalized = posted_on.strip().casefold()
    if normalized == WORKDAY_TODAY:
        return found_at
    if normalized == WORKDAY_YESTERDAY:
        return found_at - timedelta(days=1)
    match = WORKDAY_DAYS_PATTERN.fullmatch(posted_on.strip())
    if match is None:
        return None
    return found_at - timedelta(days=int(match.group(1)))


def _clean_description(description: str) -> str:
    return HTML_TAG_PATTERN.sub(" ", unescape(description))


def _locations_from_text(location_text: str) -> list[str]:
    candidates = LOCATION_SEPARATOR_PATTERN.split(location_text.strip())
    locations: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in locations:
            locations.append(candidate)
    return locations or [DEFAULT_LOCATION]


def _is_remote(*posting_text: str) -> bool:
    return REMOTE_PATTERN.search(" ".join(posting_text)) is not None


def platform_from_application_url(apply_url: str) -> Platform:
    """Return the ATS identified by a parsed application URL."""
    parsed_url = urlparse(apply_url)
    hostname = parsed_url.hostname or ""
    known_platform = PLATFORM_BY_HOST.get(hostname)
    if known_platform is not None:
        return known_platform
    if hostname.endswith(WORKDAY_HOST_SUFFIX):
        return Platform.WORKDAY
    if GREENHOUSE_JOB_ID_PARAMETER in parse_qs(parsed_url.query):
        return Platform.GREENHOUSE
    return Platform.OTHER


def _build_job(fields: _JobFields, locations: Sequence[str]) -> Job:
    normalized_locations = list(locations) or [DEFAULT_LOCATION]
    return Job(
        company=canonical_company(fields.company),
        role=fields.role,
        location=join_locations(normalized_locations),
        remote=_is_remote(
            *normalized_locations,
            fields.role,
            fields.description,
        ),
        posted_at=fields.posted_at,
        found_at=fields.found_at,
        source=fields.source,
        apply_url=fields.apply_url,
        careers_url=fields.careers_url,
        platform=fields.platform,
        notes=fields.notes,
        season=fields.season,
    )


def _greenhouse_job(board: Board, posting: GreenhousePosting, found_at: date) -> Job:
    location = posting.get("location", {}).get("name") or DEFAULT_LOCATION
    fields = _JobFields(
        company=board.company,
        role=posting["title"],
        posted_at=_parse_iso_date(posting.get("updated_at")),
        found_at=found_at,
        source=Source.GREENHOUSE,
        apply_url=posting["absolute_url"],
        careers_url=board.careers_url,
        platform=board.platform,
        notes="",
        description=_clean_description(posting.get("content", "")),
    )
    return _build_job(fields, _locations_from_text(location))


def _fetch_greenhouse(
    board: Board, session: requests.Session, found_at: date
) -> list[Job]:
    response = session.get(
        GREENHOUSE_URL.format(token=board.token), timeout=HTTP_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    payload = cast(GreenhouseResponse, response.json())
    return [_greenhouse_job(board, posting, found_at) for posting in payload["jobs"]]


def _lever_locations(posting: LeverPosting) -> list[str]:
    categories = posting.get("categories", {})
    locations = categories.get("allLocations", [])
    if locations:
        return locations
    return _locations_from_text(categories.get("location", DEFAULT_LOCATION))


def _lever_job(board: Board, posting: LeverPosting, found_at: date) -> Job:
    workplace_type = posting.get("workplaceType", "")
    fields = _JobFields(
        company=board.company,
        role=posting["text"],
        posted_at=_parse_epoch_date(posting.get("createdAt"), MILLISECONDS_PER_SECOND),
        found_at=found_at,
        source=Source.LEVER,
        apply_url=posting.get("applyUrl", posting["hostedUrl"]),
        careers_url=board.careers_url,
        platform=board.platform,
        notes="",
        description=f"{posting.get('descriptionPlain', '')} {workplace_type}",
    )
    return _build_job(fields, _lever_locations(posting))


def _fetch_lever(board: Board, session: requests.Session, found_at: date) -> list[Job]:
    response = session.get(
        LEVER_URL.format(token=board.token), timeout=HTTP_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    postings = cast(list[LeverPosting], response.json())
    return [_lever_job(board, posting, found_at) for posting in postings]


def _ashby_locations(posting: AshbyPosting) -> list[str]:
    locations = [posting.get("location", DEFAULT_LOCATION)]
    locations.extend(
        secondary["location"]
        for secondary in posting.get("secondaryLocations", [])
        if secondary.get("location")
    )
    return list(dict.fromkeys(locations))


def _ashby_job(board: Board, posting: AshbyPosting, found_at: date) -> Job:
    fields = _JobFields(
        company=board.company,
        role=posting["title"],
        posted_at=_parse_iso_date(posting.get("publishedAt")),
        found_at=found_at,
        source=Source.ASHBY,
        apply_url=posting.get("applyUrl", posting["jobUrl"]),
        careers_url=board.careers_url,
        platform=board.platform,
        notes="",
        description=(
            f"{posting.get('descriptionPlain', '')} {posting.get('workplaceType', '')}"
        ),
    )
    return _build_job(fields, _ashby_locations(posting))


def _fetch_ashby(board: Board, session: requests.Session, found_at: date) -> list[Job]:
    response = session.get(
        ASHBY_URL.format(token=board.token), timeout=HTTP_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    payload = cast(AshbyResponse, response.json())
    return [_ashby_job(board, posting, found_at) for posting in payload["jobs"]]


def _smartrecruiters_job(
    board: Board, posting: SmartRecruitersPosting, found_at: date
) -> Job:
    location_payload = posting.get("location", {})
    location = location_payload.get(
        "fullLocation", location_payload.get("city", DEFAULT_LOCATION)
    )
    remote_indicator = "remote" if location_payload.get("remote", False) else ""
    fields = _JobFields(
        company=board.company,
        role=posting["name"],
        posted_at=_parse_iso_date(posting.get("releasedDate")),
        found_at=found_at,
        source=Source.SMARTRECRUITERS,
        apply_url=SMARTRECRUITERS_JOB_URL.format(
            token=board.token, job_id=posting["id"]
        ),
        careers_url=board.careers_url,
        platform=board.platform,
        notes="",
        description=remote_indicator,
    )
    return _build_job(fields, _locations_from_text(location))


def _fetch_smartrecruiters(
    board: Board, session: requests.Session, found_at: date
) -> list[Job]:
    jobs: list[Job] = []
    for page in range(SMARTRECRUITERS_PAGE_LIMIT):
        params = SmartRecruitersParams(
            limit=SMARTRECRUITERS_PAGE_SIZE,
            offset=page * SMARTRECRUITERS_PAGE_SIZE,
        )
        response = session.get(
            SMARTRECRUITERS_URL.format(token=board.token),
            params=params,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = cast(SmartRecruitersResponse, response.json())
        postings = payload["content"]
        jobs.extend(
            _smartrecruiters_job(board, posting, found_at) for posting in postings
        )
        if len(postings) < SMARTRECRUITERS_PAGE_SIZE:
            break
    return jobs


def _parse_workday_token(token: str) -> tuple[str, str, str]:
    parts = token.split("|")
    if len(parts) != WORKDAY_TOKEN_PARTS or not all(parts):
        raise ValueError("Workday token must use host|tenant|site format")
    return parts[0], parts[1], parts[2]


def _workday_job(
    board: Board,
    posting: WorkdayPosting,
    found_at: date,
    host: str,
    site: str,
) -> Job:
    fields = _JobFields(
        company=board.company,
        role=posting["title"],
        posted_at=_parse_workday_date(posting.get("postedOn"), found_at),
        found_at=found_at,
        source=Source.WORKDAY,
        apply_url=WORKDAY_JOB_URL.format(
            host=host, site=site, external_path=posting["externalPath"]
        ),
        careers_url=board.careers_url,
        platform=board.platform,
        notes="",
        description="",
    )
    return _build_job(
        fields, _locations_from_text(posting.get("locationsText", DEFAULT_LOCATION))
    )


def _fetch_workday(
    board: Board, session: requests.Session, found_at: date
) -> list[Job]:
    host, tenant, site = _parse_workday_token(board.token)
    jobs: list[Job] = []
    for page in range(WORKDAY_PAGE_LIMIT):
        request_payload = WorkdayRequest(
            appliedFacets={},
            limit=WORKDAY_PAGE_SIZE,
            offset=page * WORKDAY_PAGE_SIZE,
            searchText=SIMPLIFY_ROLE_TERM,
        )
        response = session.post(
            WORKDAY_URL.format(host=host, tenant=tenant, site=site),
            json=request_payload,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = cast(WorkdayResponse, response.json())
        postings = payload["jobPostings"]
        jobs.extend(
            _workday_job(board, posting, found_at, host, site) for posting in postings
        )
        if len(postings) < WORKDAY_PAGE_SIZE:
            break
    return jobs


BoardFetcher = Callable[[Board, requests.Session, date], list[Job]]
BOARD_FETCHERS: dict[Platform, BoardFetcher] = {
    Platform.GREENHOUSE: _fetch_greenhouse,
    Platform.LEVER: _fetch_lever,
    Platform.ASHBY: _fetch_ashby,
    Platform.SMARTRECRUITERS: _fetch_smartrecruiters,
    Platform.WORKDAY: _fetch_workday,
}


def fetch_board(board: Board, session: requests.Session, found_at: date) -> list[Job]:
    """Fetch one ATS board and normalize every returned posting."""
    try:
        fetcher = BOARD_FETCHERS[board.platform]
    except KeyError as error:
        raise ValueError(
            f"Unsupported board platform: {board.platform.value}"
        ) from error
    return fetcher(board, session, found_at)


def _simplify_notes(posting: SimplifyPosting) -> str:
    notes: list[str] = []
    sponsorship = posting.get("sponsorship")
    if sponsorship:
        notes.append(f"Sponsorship: {sponsorship}")
    terms = posting.get("terms", [])
    season = posting.get("season")
    if terms:
        notes.append(f"Terms: {', '.join(terms)}")
    elif season:
        notes.append(f"Season: {season}")
    return "; ".join(notes)


def _parse_season_label(label: str) -> Season | None:
    words = set(re.findall(r"[a-z]+", label.casefold()))
    matches = [season for season in Season if season.value.casefold() in words]
    if len(matches) != 1:
        return None
    return matches[0]


def _simplify_season(posting: SimplifyPosting) -> Season | None:
    labels = list(posting.get("terms", []))
    stated_season = posting.get("season")
    if stated_season:
        labels.append(stated_season)
    recognized_seasons = {
        parsed_season
        for label in labels
        if (parsed_season := _parse_season_label(label)) is not None
    }
    if len(recognized_seasons) != 1:
        return None
    return next(iter(recognized_seasons))


def _simplify_job(posting: SimplifyPosting, found_at: date) -> Job:
    fields = _JobFields(
        company=posting["company_name"],
        role=posting["title"],
        posted_at=_parse_epoch_date(posting.get("date_posted")),
        found_at=found_at,
        source=Source.SIMPLIFY,
        apply_url=posting["url"],
        careers_url=posting.get("company_url", ""),
        platform=platform_from_application_url(posting["url"]),
        notes=_simplify_notes(posting),
        description="",
        season=_simplify_season(posting),
    )
    return _build_job(fields, posting.get("locations", [DEFAULT_LOCATION]))


def fetch_simplify(session: requests.Session, found_at: date) -> list[Job]:
    """Fetch active visible internship listings from the Summer 2027 feed."""
    response = session.get(SIMPLIFY_LISTINGS_URL, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    postings = cast(list[SimplifyPosting], response.json())
    return [
        _simplify_job(posting, found_at)
        for posting in postings
        if posting.get("active")
        and posting.get("is_visible")
        and SIMPLIFY_ROLE_TERM in posting["title"].casefold()
    ]


def _build_session() -> requests.Session:
    retry = Retry(
        total=HTTP_RETRY_TOTAL,
        connect=HTTP_RETRY_TOTAL,
        read=HTTP_RETRY_TOTAL,
        status=HTTP_RETRY_TOTAL,
        allowed_methods=HTTP_RETRY_METHODS,
        status_forcelist=HTTP_RETRY_STATUS_CODES,
        backoff_factor=HTTP_RETRY_BACKOFF_SECONDS,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _fetch_all_sources(
    boards: Sequence[Board], session: requests.Session, found_at: date
) -> list[Job]:
    jobs: list[Job] = []
    for board in boards:
        try:
            jobs.extend(fetch_board(board, session, found_at))
        except (requests.RequestException, ValueError, KeyError) as error:
            logger.warning("Skipping %s board: %s", board.company, error)
    try:
        jobs.extend(fetch_simplify(session, found_at))
    except (requests.RequestException, ValueError, KeyError) as error:
        logger.warning("Skipping Simplify listings: %s", error)
    return jobs


def fetch_all(
    boards: Sequence[Board],
    found_at: date,
    session: requests.Session | None = None,
) -> list[Job]:
    """Fetch all boards and Simplify while isolating source failures."""
    owns_session = session is None
    active_session = session if session is not None else _build_session()
    try:
        return _fetch_all_sources(boards, active_session, found_at)
    finally:
        if owns_session:
            active_session.close()
