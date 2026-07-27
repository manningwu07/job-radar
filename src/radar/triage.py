"""Relevance filtering, duplicate merging and ranking for postings from the sources layer.

Exports:
    RoleCategory -- interest areas, value is the user's priority (lower sorts first).
    GeoTier -- geographic preference.
    SortKey -- the ranking key `sort_key` returns.
    target_summer_year -- the summer the user is currently recruiting for.
    is_relevant -- level, interest-area, reject-rule, freshness and season gate for one posting.
    merge_duplicates -- collapse records sharing a `Job.key` into one well-formed row.
    sort_key -- ascending ranking key.
    triage -- filter, merge duplicates, then sort.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from enum import Enum, IntEnum
from typing import NamedTuple

from radar.boards import company_priority
from radar.models import LOCATION_SEPARATOR, Job, Season, Source, join_locations, normalize

logger = logging.getLogger(__name__)


class RoleCategory(Enum):
    """Interest areas. Value is the user's interest priority, lower sorts first."""

    SOFTWARE_ENGINEERING = 1
    MACHINE_LEARNING = 2
    BACKEND_INFRASTRUCTURE = 3
    DISTRIBUTED_SYSTEMS = 4
    PLATFORM_ENGINEERING = 5
    AI_INFRASTRUCTURE = 6
    ROBOTICS_SOFTWARE = 7
    RESEARCH_ENGINEERING = 8
    OTHER = 9


class GeoTier(IntEnum):
    """Geographic preference, lower sorts first."""

    BAY_AREA = 0
    SEATTLE = 1
    NEW_YORK = 2
    BOSTON = 3
    REMOTE = 4
    OTHER = 5


class SortKey(NamedTuple):
    """Ascending ranking key. The trailing names make the order total, so a run's output
    does not depend on the order the sources returned equally ranked postings."""

    company_rank: int
    category: int
    recency: int
    geo: GeoTier
    company_name: str
    role_name: str


INTERNSHIP_KEYWORDS = frozenset(
    {
        "intern",
        "interns",
        "internship",
        "internships",
        "co-op",
        "coop",
        "student",
        "students",
        "university",
        "undergraduate",
        "apprentice",
        "apprenticeship",
        "early career",
        "early-career",
    }
)

SOFTWARE_ENGINEERING_KEYWORDS = frozenset(
    {
        "software engineer",
        "software engineering",
        "software developer",
        "software development",
        "software development engineer",
        "swe",
        "sde",
        "programmer",
    }
)
MACHINE_LEARNING_KEYWORDS = frozenset(
    {
        "machine learning",
        "ml engineer",
        "ml engineering",
        "ml systems",
        "ml infrastructure",
        "ml infra",
        "ml platform",
        "mlops",
        "deep learning",
    }
)
BACKEND_INFRASTRUCTURE_KEYWORDS = frozenset(
    {
        "backend",
        "back-end",
        "back end",
        "server-side",
        "infrastructure",
        "infra",
    }
)
DISTRIBUTED_SYSTEMS_KEYWORDS = frozenset(
    {
        "distributed systems",
        "distributed computing",
        "distributed infrastructure",
    }
)
PLATFORM_ENGINEERING_KEYWORDS = frozenset(
    {
        "platform engineer",
        "platform engineering",
        "developer platform",
        "developer productivity",
        "compute platform",
        "cloud platform",
    }
)
AI_INFRASTRUCTURE_KEYWORDS = frozenset(
    {
        "ai infrastructure",
        "ai infra",
        "ai systems",
        "ai platform",
        "llm infrastructure",
        "inference",
        "accelerator",
        "gpu",
        "cuda",
    }
)
ROBOTICS_SOFTWARE_KEYWORDS = frozenset(
    {
        "robotics",
        "robot software",
        "autonomy",
        "autonomous",
        "motion planning",
        "perception",
        "slam",
    }
)
RESEARCH_ENGINEERING_KEYWORDS = frozenset(
    {
        "research engineer",
        "research engineering",
        "research scientist",
        "research intern",
    }
)

PRODUCT_REJECT_KEYWORDS = frozenset(
    {
        "product manager",
        "product management",
        "product marketing",
        "program manager",
        "project manager",
        "tpm",
    }
)
ANALYST_REJECT_KEYWORDS = frozenset(
    {
        "data analyst",
        "data analytics",
        "business analyst",
        "business intelligence",
    }
)
IT_REJECT_KEYWORDS = frozenset(
    {
        "information technology",
        "it support",
        "it operations",
        "help desk",
        "helpdesk",
        "desktop support",
        "system administrator",
    }
)
QA_REJECT_KEYWORDS = frozenset(
    {
        "qa",
        "quality assurance",
        "quality engineer",
        "test engineer",
        "sdet",
        "manual testing",
    }
)
QUANT_REJECT_KEYWORDS = frozenset(
    {
        "quant research",
        "quant researcher",
        "quantitative research",
        "quantitative researcher",
        "quantitative analyst",
    }
)
LEVEL_REJECT_KEYWORDS = frozenset(
    {
        "new grad",
        "new graduate",
        "new college grad",
        "new college graduate",
        "recent graduate",
        "senior",
        "sr",
        "staff",
        "principal",
        "distinguished",
        "experienced",
        "lead",
        "manager",
        "director",
        "head of",
    }
)
TRADING_KEYWORDS = frozenset({"trading", "trader"})

FRONTEND_SCOPE_KEYWORDS = frozenset(
    {
        "frontend",
        "front-end",
        "front end",
        "ui engineer",
        "ui engineering",
        "web ui",
    }
)
MOBILE_SCOPE_KEYWORDS = frozenset(
    {
        "mobile",
        "ios",
        "android",
        "react native",
    }
)
SECURITY_SCOPE_KEYWORDS = frozenset(
    {
        "security",
        "cybersecurity",
        "appsec",
        "application security",
        "information security",
    }
)
BROADER_SCOPE_KEYWORDS = frozenset(
    {
        "full stack",
        "full-stack",
        "fullstack",
        "backend",
        "back-end",
        "back end",
        "infrastructure",
        "infra",
        "platform",
        "distributed",
        "systems",
        "machine learning",
        "ml",
        "ai",
        "compiler",
        "kernel",
    }
)

BAY_AREA_KEYWORDS = frozenset(
    {
        "bay area",
        "san francisco",
        "south san francisco",
        "sf",
        "mountain view",
        "palo alto",
        "menlo park",
        "sunnyvale",
        "santa clara",
        "san jose",
        "cupertino",
        "redwood city",
        "foster city",
        "burlingame",
        "fremont",
    }
)
SEATTLE_KEYWORDS = frozenset({"seattle", "bellevue", "redmond", "kirkland"})
NEW_YORK_KEYWORDS = frozenset({"new york", "nyc", "manhattan", "brooklyn"})
BOSTON_KEYWORDS = frozenset({"boston", "cambridge", "somerville"})
REMOTE_KEYWORDS = frozenset({"remote", "anywhere", "distributed team"})

SEASON_ALIASES = {
    "winter": Season.WINTER,
    "spring": Season.SPRING,
    "summer": Season.SUMMER,
    "fall": Season.FALL,
    "autumn": Season.FALL,
}
TARGET_SEASON = Season.SUMMER
SUMMER_CYCLE_ROLLOVER_MONTH = 6
CENTURY_OFFSET = 2000
UNDATED_POSTING_RANK = (date.max - date.min).days + 1
MAX_POSTING_AGE_DAYS = 180
TRUNCATION_MARKER_PATTERN = re.compile(r"^\+\d+ more$")
SEGMENT_DELIMITER = LOCATION_SEPARATOR.strip()
QUERY_MARKER = "?"


def _matcher(keywords: frozenset[str]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(keyword) for keyword in sorted(keywords))
    return re.compile(rf"\b(?:{alternatives})\b")


_INTERNSHIP_PATTERN = _matcher(INTERNSHIP_KEYWORDS)
_SOFTWARE_ENGINEERING_PATTERN = _matcher(SOFTWARE_ENGINEERING_KEYWORDS)
_FUNCTION_REJECT_PATTERN = _matcher(
    PRODUCT_REJECT_KEYWORDS
    | ANALYST_REJECT_KEYWORDS
    | IT_REJECT_KEYWORDS
    | QA_REJECT_KEYWORDS
    | QUANT_REJECT_KEYWORDS
)
_LEVEL_REJECT_PATTERN = _matcher(LEVEL_REJECT_KEYWORDS)
_TRADING_PATTERN = _matcher(TRADING_KEYWORDS)
_BROADER_SCOPE_PATTERN = _matcher(BROADER_SCOPE_KEYWORDS)

CATEGORY_PATTERNS: tuple[tuple[RoleCategory, re.Pattern[str]], ...] = (
    (RoleCategory.SOFTWARE_ENGINEERING, _SOFTWARE_ENGINEERING_PATTERN),
    (RoleCategory.MACHINE_LEARNING, _matcher(MACHINE_LEARNING_KEYWORDS)),
    (RoleCategory.BACKEND_INFRASTRUCTURE, _matcher(BACKEND_INFRASTRUCTURE_KEYWORDS)),
    (RoleCategory.DISTRIBUTED_SYSTEMS, _matcher(DISTRIBUTED_SYSTEMS_KEYWORDS)),
    (RoleCategory.PLATFORM_ENGINEERING, _matcher(PLATFORM_ENGINEERING_KEYWORDS)),
    (RoleCategory.AI_INFRASTRUCTURE, _matcher(AI_INFRASTRUCTURE_KEYWORDS)),
    (RoleCategory.ROBOTICS_SOFTWARE, _matcher(ROBOTICS_SOFTWARE_KEYWORDS)),
    (RoleCategory.RESEARCH_ENGINEERING, _matcher(RESEARCH_ENGINEERING_KEYWORDS)),
)

NARROW_SCOPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    _matcher(FRONTEND_SCOPE_KEYWORDS),
    _matcher(MOBILE_SCOPE_KEYWORDS),
    _matcher(SECURITY_SCOPE_KEYWORDS),
)

GEO_PATTERNS: tuple[tuple[GeoTier, re.Pattern[str]], ...] = (
    (GeoTier.BAY_AREA, _matcher(BAY_AREA_KEYWORDS)),
    (GeoTier.SEATTLE, _matcher(SEATTLE_KEYWORDS)),
    (GeoTier.NEW_YORK, _matcher(NEW_YORK_KEYWORDS)),
    (GeoTier.BOSTON, _matcher(BOSTON_KEYWORDS)),
    (GeoTier.REMOTE, _matcher(REMOTE_KEYWORDS)),
)

_SEASON_PATTERN = _matcher(frozenset(SEASON_ALIASES))
_YEAR_PATTERN = re.compile(r"\b20(\d{2})\b|'(\d{2})\b")
_DATED_TERM_PATTERN = re.compile(
    rf"\b(?P<season>{'|'.join(sorted(SEASON_ALIASES))})[-_,\s]*'?(?P<year>20\d{{2}}|\d{{2}})\b"
)


def target_summer_year(today: date) -> int:
    """Return the summer the user is recruiting for.

    The cycle rolls over once the current summer has begun, so a run in June 2026 or later
    targets Summer 2027, while a run in January 2027 still targets Summer 2027.
    """
    if today.month >= SUMMER_CYCLE_ROLLOVER_MONTH:
        return today.year + 1
    return today.year


def categorize(role: str) -> RoleCategory:
    """Return the highest-priority interest area the role title matches."""
    normalized = normalize(role)
    for category, pattern in CATEGORY_PATTERNS:
        if pattern.search(normalized):
            return category
    return RoleCategory.OTHER


def _expand_year(digits: str) -> int:
    if len(digits) == len(str(CENTURY_OFFSET)):
        return int(digits)
    return CENTURY_OFFSET + int(digits)


def _stated_years(text: str) -> set[int]:
    return {
        CENTURY_OFFSET + int(match.group(1) or match.group(2))
        for match in _YEAR_PATTERN.finditer(text)
    }


def _stated_seasons(text: str) -> set[Season]:
    return {SEASON_ALIASES[match.group()] for match in _SEASON_PATTERN.finditer(text)}


def _dated_terms(text: str) -> set[tuple[Season, int]]:
    """Season and year stated together, as in `summer 2027` or `...-intern-fall-2026-266119`."""
    return {
        (SEASON_ALIASES[match.group("season")], _expand_year(match.group("year")))
        for match in _DATED_TERM_PATTERN.finditer(text)
    }


def _seasons_allow(seasons: set[Season]) -> bool:
    return all(season is TARGET_SEASON for season in seasons)


def _years_allow(years: set[int], dated: set[tuple[Season, int]], target_year: int) -> bool:
    """A term dated before the target rejects; otherwise the latest year stated must qualify."""
    if any(year < target_year for _, year in dated):
        return False
    return not years or max(years) >= target_year


def _season_allows(job: Job) -> bool:
    """Reject any stated term that is not the target Summer. An unstated term passes.

    Three signals are read and any one of them can reject: the role title, a season and
    year embedded in the apply URL slug, and the `Job.season` field. The URL is searched
    only for a season next to a year, because board slugs carry company names such as
    `spring-health` and requisition numbers that look like years.
    """
    role = normalize(job.role)
    dated = _dated_terms(role) | _dated_terms(normalize(job.apply_url))
    seasons = _stated_seasons(role) | {season for season, _ in dated}
    if job.season is not None:
        seasons.add(job.season)
    if not _seasons_allow(seasons):
        return False
    years = _stated_years(role) | {year for _, year in dated}
    return _years_allow(years, dated, target_summer_year(job.found_at))


def _is_narrow_scope_only(role: str) -> bool:
    """True when the role is scoped to frontend, mobile or security and nothing broader."""
    if _BROADER_SCOPE_PATTERN.search(role):
        return False
    return any(pattern.search(role) for pattern in NARROW_SCOPE_PATTERNS)


def _is_rejected(role: str) -> bool:
    if _LEVEL_REJECT_PATTERN.search(role):
        return True
    if _FUNCTION_REJECT_PATTERN.search(role):
        return True
    if _TRADING_PATTERN.search(role) and not _SOFTWARE_ENGINEERING_PATTERN.search(role):
        return True
    return _is_narrow_scope_only(role)


def _is_fresh(posted_at: date | None, found_at: date) -> bool:
    """Undated postings pass: most ATS titles carry no date, so dropping them guts results."""
    if posted_at is None:
        return True
    return (found_at - posted_at).days <= MAX_POSTING_AGE_DAYS


def is_relevant(job: Job) -> bool:
    """True when the posting is a fresh internship in one of the user's interest areas."""
    role = normalize(job.role)
    if not _INTERNSHIP_PATTERN.search(role):
        return False
    if categorize(role) is RoleCategory.OTHER:
        return False
    if _is_rejected(role):
        return False
    if not _is_fresh(job.posted_at, job.found_at):
        return False
    return _season_allows(job)


def _posting_rank(posted_at: date | None) -> int:
    """Ascending rank where newer postings sort first and undated postings sort last."""
    if posted_at is None:
        return UNDATED_POSTING_RANK
    return (date.max - posted_at).days


def split_locations(location: str) -> list[str]:
    """Split a joined location cell into cities, discarding the `+N more` truncation marker."""
    parts = (part.strip() for part in location.split(SEGMENT_DELIMITER))
    return [
        part for part in parts if part and not TRUNCATION_MARKER_PATTERN.match(normalize(part))
    ]


def _segment_tier(segment: str) -> GeoTier:
    for tier, pattern in GEO_PATTERNS:
        if pattern.search(segment):
            return tier
    return GeoTier.OTHER


def _geo_rank(location: str, remote: bool) -> GeoTier:
    """Score a multi-location posting by its most preferred location."""
    best = min(
        (_segment_tier(normalize(segment)) for segment in split_locations(location)),
        default=GeoTier.OTHER,
    )
    if best is GeoTier.OTHER and remote:
        return GeoTier.REMOTE
    return best


def sort_key(job: Job) -> SortKey:
    """Ascending ranking key: company priority, interest area, recency, then geography."""
    return SortKey(
        company_rank=company_priority(job.company),
        category=categorize(job.role).value,
        recency=_posting_rank(job.posted_at),
        geo=_geo_rank(job.location, job.remote),
        company_name=normalize(job.company),
        role_name=normalize(job.role),
    )


def _url_preference(job: Job) -> tuple[int, int, int, str]:
    """Ascending: a first-party ATS link beats Simplify, a bare URL beats a parameterised
    one, then the shortest canonical form wins, with the URL itself as a stable tiebreak."""
    return (
        int(job.source is Source.SIMPLIFY),
        int(QUERY_MARKER in job.apply_url),
        len(job.apply_url),
        job.apply_url,
    )


def _states_a_term(role: str) -> bool:
    normalized = normalize(role)
    return bool(_stated_seasons(normalized) or _stated_years(normalized))


def _role_informativeness(job: Job) -> tuple[int, int, str]:
    """Read by `max`: a title naming its term beats a bare one, then the longest title
    wins, with the title itself as a tiebreak so the result never depends on input order."""
    return (int(_states_a_term(job.role)), len(job.role), job.role)


def _merged_locations(jobs: Sequence[Job]) -> list[str]:
    """Union every city across the group, first spelling of a repeat wins.

    Cities are ordered by geographic preference so that the three `join_locations` shows are
    the three the user cares most about, and so the cell does not depend on the order the
    sources happened to return.
    """
    merged: dict[str, str] = {}
    for job in jobs:
        for city in split_locations(job.location):
            merged.setdefault(normalize(city), city)
    ordered = sorted(merged, key=lambda city: (_segment_tier(city), city))
    return [merged[city] for city in ordered]


def _latest_posting(jobs: Sequence[Job]) -> date | None:
    dates = [job.posted_at for job in jobs if job.posted_at is not None]
    if not dates:
        return None
    return max(dates)


def _stated_season(jobs: Sequence[Job]) -> Season | None:
    return next((job.season for job in jobs if job.season is not None), None)


def merge_duplicates(jobs: Sequence[Job]) -> Job:
    """Collapse records that share a `Job.key` into one row carrying the richest values.

    Employers post one requisition per city and aggregators republish the same requisition
    under a different URL form and a differently decorated title, so a group holds
    complementary fields rather than noise. `source`, `platform`, `careers_url` and `notes`
    come from the record whose apply URL is chosen, which keeps the row internally
    consistent. The role is chosen independently of the URL because it is display text, and
    every member shares one `role_identity`, so the group's key is unaffected by the choice.
    """
    primary = min(jobs, key=_url_preference)
    return replace(
        primary,
        role=max(jobs, key=_role_informativeness).role,
        location=join_locations(_merged_locations(jobs)),
        remote=any(job.remote for job in jobs),
        posted_at=_latest_posting(jobs),
        season=_stated_season(jobs),
    )


def _group_by_key(jobs: Sequence[Job]) -> list[list[Job]]:
    groups: dict[str, list[Job]] = {}
    for job in jobs:
        groups.setdefault(job.key, []).append(job)
    return list(groups.values())


def triage(jobs: Sequence[Job]) -> list[Job]:
    """Filter to relevant postings, merge duplicates, and rank the survivors."""
    relevant = [job for job in jobs if is_relevant(job)]
    merged = [merge_duplicates(group) for group in _group_by_key(relevant)]
    logger.info(
        "Triaged %d raw postings into %d relevant, %d merged rows",
        len(jobs),
        len(relevant),
        len(merged),
    )
    return sorted(merged, key=sort_key)
