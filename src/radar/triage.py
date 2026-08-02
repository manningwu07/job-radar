"""Relevance filtering, duplicate merging and ranking for postings from the sources layer.

Exports:
    RoleCategory -- interest areas, value is the user's priority (lower sorts first).
    GeoTier -- geographic preference.
    Country -- whether a location segment is somewhere the user can work.
    SortKey -- the ranking key `sort_key` returns.
    target_summer_year -- the summer the user is currently recruiting for.
    classify_location -- place one location segment as US, foreign or unrecognized.
    is_relevant -- level, degree, country, interest-area, freshness and season gate.
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
    """Interest areas. Value is the user's interest priority, lower sorts first.

    AI/automation leads because dual-titled postings (e.g. `Software Engineer Intern,
    Autonomy`) must beat plain SWE in the digest, and company tier still dominates the
    sort key so a tier-one SWE intern is not buried by a long-tail automation role.
    """

    AI_AUTOMATION = 1
    MACHINE_LEARNING = 2
    SOFTWARE_ENGINEERING = 3
    AI_INFRASTRUCTURE = 4
    PLATFORM_ENGINEERING = 5
    BACKEND_INFRASTRUCTURE = 6
    DISTRIBUTED_SYSTEMS = 7
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


class Country(Enum):
    """Whether one location segment is somewhere the user can work."""

    UNITED_STATES = "united states"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"


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
AI_AUTOMATION_KEYWORDS = frozenset(
    {
        "ai automation",
        "automation",
        "robotics",
        "robot software",
        "autonomy",
        "autonomous",
        "motion planning",
        "perception",
        "slam",
        "control systems",
        "vehicle controls",
        "motion control",
        "simulation",
        "embodied",
        "self-driving",
        "self driving",
        "driverless",
        "adas",
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
# `SDET` spelled out, in the separator forms boards actually use: `Software Development
# Engineer in Test`, `Software Engineer, Test`, `Software Engineer - Test`. Testing is the
# whole job only when `test` sits directly against `engineer`; everywhere else the word
# qualifies something broader, as in `Test Infrastructure`, so this is checked as a narrow
# scope that a broader-scope keyword anywhere in the title overrides.
ENGINEER_IN_TEST_PATTERN = re.compile(r"\bengineer\s*(?:in|,|-|/)\s*test\b")
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

GRADUATE_DEGREE_KEYWORDS = frozenset(
    {
        "phd",
        "phds",
        "doctoral",
        "doctorate",
        "master",
        "masters",
        "ms",
        "msc",
        "meng",
        "mba",
        "graduate degree",
        "graduate intern",
        "graduate internship",
        "graduate student",
        "graduate students",
        "grad student",
        "grad students",
        "postgraduate",
        "post-graduate",
        "postgrad",
    }
)
UNDERGRADUATE_DEGREE_KEYWORDS = frozenset(
    {
        "undergrad",
        "undergrads",
        "undergraduate",
        "undergraduates",
        "bachelor",
        "bachelors",
        "ba",
        "bs",
        "bsc",
        "beng",
        "freshman",
        "sophomore",
    }
)

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

US_STATE_NAMES = frozenset(
    {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
        "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
        "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
        "new mexico", "new york", "north carolina", "north dakota", "ohio",
        "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
        "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
        "washington", "west virginia", "wisconsin", "wyoming",
        "district of columbia", "puerto rico",
    }
)
US_COUNTRY_NAMES = frozenset({"united states", "united states of america", "usa", "us"})
US_STATE_CODES = frozenset(
    {
        "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
        "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
        "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
        "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
        "wi", "wy", "dc", "pr",
    }
)
US_LOCATION_CODES = US_STATE_CODES | frozenset({"us", "usa"})

FOREIGN_COUNTRY_NAMES = frozenset(
    {
        "canada", "mexico", "united kingdom", "uk", "england", "scotland", "wales",
        "ireland", "france", "germany", "netherlands", "belgium", "luxembourg",
        "switzerland", "austria", "spain", "portugal", "italy", "greece", "poland",
        "czechia", "czech republic", "slovakia", "hungary", "romania", "bulgaria",
        "serbia", "croatia", "slovenia", "ukraine", "lithuania", "latvia",
        "estonia", "finland", "sweden", "norway", "denmark", "iceland", "israel",
        "turkey", "united arab emirates", "uae", "saudi arabia", "qatar", "egypt",
        "kenya", "nigeria", "ghana", "south africa", "india", "pakistan",
        "bangladesh", "sri lanka", "china", "hong kong", "taiwan", "japan",
        "korea", "south korea", "singapore", "malaysia", "indonesia", "thailand",
        "vietnam", "philippines", "australia", "new zealand", "brazil",
        "argentina", "chile", "colombia", "uruguay", "costa rica", "guatemala",
        "cayman",
    }
)
# Only cities whose foreign namesake is the one an employer on these boards would mean.
# `Melbourne` and `Brisbane` are absent because Northrop Grumman hires in Melbourne, FL
# and Brisbane, CA is in the Bay Area; `Cambridge`, `Dublin` and `Vancouver` are absent
# for the same reason. Those places are still caught when a country or province is given.
FOREIGN_CITY_NAMES = frozenset(
    {
        "ahmedabad", "bangalore", "bengaluru", "chennai", "gurgaon", "gurugram",
        "hyderabad", "kolkata", "mumbai", "new delhi", "noida", "pune",
        "haifa", "herzliya", "ramat gan", "tel aviv",
        "beijing", "chengdu", "guangzhou", "hangzhou", "osaka", "seoul",
        "shanghai", "shenzhen", "taipei", "tokyo", "yokohama",
        "bangkok", "cebu", "dhaka", "hanoi", "ho chi minh city", "islamabad",
        "jakarta", "karachi", "kuala lumpur", "lahore", "manila",
        "auckland", "canberra", "sydney",
        "calgary", "edmonton", "markham", "mississauga", "montreal", "ottawa",
        "quebec city", "toronto", "winnipeg",
        "belgrade", "bratislava", "brno", "bucharest", "budapest", "gdansk",
        "krakow", "ljubljana", "poznan", "prague", "riga", "sofia", "tallinn",
        "vilnius", "warsaw", "wroclaw", "zagreb",
        "amsterdam", "antwerp", "basel", "brussels", "cologne", "copenhagen",
        "dusseldorf", "eindhoven", "frankfurt", "geneva", "gothenburg",
        "helsinki", "lausanne", "leipzig", "munich", "nuremberg", "oslo",
        "reykjavik", "rotterdam", "stockholm", "stuttgart", "the hague",
        "utrecht", "zurich",
        "barcelona", "edinburgh", "glasgow", "leeds", "lisbon", "london", "madrid",
        "porto", "seville", "turin",
        "abu dhabi", "almaty", "ankara", "doha", "dubai", "istanbul", "jeddah",
        "kyiv", "riyadh", "tbilisi", "yerevan",
        "accra", "cape town", "johannesburg", "lagos", "nairobi",
        "belo horizonte", "bogota", "buenos aires", "guadalajara", "medellin",
        "mexico city", "monterrey", "queretaro", "rio de janeiro", "santiago",
        "sao paulo",
    }
)
FOREIGN_SUBDIVISION_CODES = frozenset(
    {"ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt"}
)

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
FIELD_DELIMITER = ","
LOCATION_CODE_LENGTH = 2
QUERY_MARKER = "?"

# Collapses the dotted and possessive spellings of an abbreviation, so `D.C.` is read as
# `dc` and `Master's` as `masters` without needing a table entry for every spelling.
ABBREVIATION_PUNCTUATION = str.maketrans("", "", ".'\u2019")


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
_GRADUATE_DEGREE_PATTERN = _matcher(GRADUATE_DEGREE_KEYWORDS)
_UNDERGRADUATE_DEGREE_PATTERN = _matcher(UNDERGRADUATE_DEGREE_KEYWORDS)
_US_PLACE_PATTERN = _matcher(US_STATE_NAMES | US_COUNTRY_NAMES)
_FOREIGN_PLACE_PATTERN = _matcher(FOREIGN_COUNTRY_NAMES | FOREIGN_CITY_NAMES)

CATEGORY_PATTERNS: tuple[tuple[RoleCategory, re.Pattern[str]], ...] = (
    (RoleCategory.AI_AUTOMATION, _matcher(AI_AUTOMATION_KEYWORDS)),
    (RoleCategory.MACHINE_LEARNING, _matcher(MACHINE_LEARNING_KEYWORDS)),
    (RoleCategory.SOFTWARE_ENGINEERING, _SOFTWARE_ENGINEERING_PATTERN),
    (RoleCategory.AI_INFRASTRUCTURE, _matcher(AI_INFRASTRUCTURE_KEYWORDS)),
    (RoleCategory.PLATFORM_ENGINEERING, _matcher(PLATFORM_ENGINEERING_KEYWORDS)),
    (RoleCategory.BACKEND_INFRASTRUCTURE, _matcher(BACKEND_INFRASTRUCTURE_KEYWORDS)),
    (RoleCategory.DISTRIBUTED_SYSTEMS, _matcher(DISTRIBUTED_SYSTEMS_KEYWORDS)),
    (RoleCategory.RESEARCH_ENGINEERING, _matcher(RESEARCH_ENGINEERING_KEYWORDS)),
)

NARROW_SCOPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    _matcher(FRONTEND_SCOPE_KEYWORDS),
    _matcher(MOBILE_SCOPE_KEYWORDS),
    _matcher(SECURITY_SCOPE_KEYWORDS),
    ENGINEER_IN_TEST_PATTERN,
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
    """Return the highest-priority interest area the role title matches.

    Every category pattern is checked, then the best (lowest) priority wins. That way a
    title like `Software Engineer Intern, Autonomy` lands in AI_AUTOMATION instead of
    plain SOFTWARE_ENGINEERING just because the SWE keywords also matched.
    """
    normalized = normalize(role)
    matches = [category for category, pattern in CATEGORY_PATTERNS if pattern.search(normalized)]
    if not matches:
        return RoleCategory.OTHER
    return min(matches, key=lambda category: category.value)


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


def split_locations(location: str) -> list[str]:
    """Split a joined location cell into cities, discarding the `+N more` truncation marker."""
    parts = (part.strip() for part in location.split(SEGMENT_DELIMITER))
    return [
        part for part in parts if part and not TRUNCATION_MARKER_PATTERN.match(normalize(part))
    ]


def _is_graduate_only(role: str) -> bool:
    """True when the title targets graduate candidates and never mentions undergraduates.

    Reject only the graduate-EXCLUSIVE title. A title listing several degrees, such as
    "open to Bachelors, Masters and PhD students", is undergrad-eligible, and a title
    stating no degree at all is accepted because most internships take undergraduates and
    the unstated majority is the bulk of the results.
    """
    degrees = normalize(role).translate(ABBREVIATION_PUNCTUATION)
    if _UNDERGRADUATE_DEGREE_PATTERN.search(degrees):
        return False
    return bool(_GRADUATE_DEGREE_PATTERN.search(degrees))


def _location_codes(segment: str) -> set[str]:
    """The comma-separated fields of one segment that are bare two-letter codes.

    A code is only ever read from a whole field, never as a word inside the text, because
    `CA` also abbreviates Canada and `IN`, `OR`, `ME`, `OK`, `HI` and `DE` are ordinary
    English words that appear in city names and phrases like "Remote in the US".
    """
    fields = (
        field.strip().translate(ABBREVIATION_PUNCTUATION)
        for field in segment.split(FIELD_DELIMITER)
    )
    return {field for field in fields if len(field) == LOCATION_CODE_LENGTH}


def classify_location(segment: str) -> Country:
    """Place one location segment, resolving the codes that two countries share.

    Order matters. A spelled-out US state or country wins first, so `Albuquerque, New
    Mexico` is not read as Mexico and `US / Canada` is not read as Canada. A spelled-out
    foreign place wins next, so `Bengaluru, IN` is India rather than Indiana. Only then is
    a bare two-letter code trusted, which keeps `Vancouver, WA` in Washington while
    `Vancouver, BC` goes to British Columbia.
    """
    text = normalize(segment)
    if _US_PLACE_PATTERN.search(text):
        return Country.UNITED_STATES
    codes = _location_codes(text)
    if _FOREIGN_PLACE_PATTERN.search(text) or codes & FOREIGN_SUBDIVISION_CODES:
        return Country.FOREIGN
    if codes & US_LOCATION_CODES:
        return Country.UNITED_STATES
    return Country.UNKNOWN


def _is_us_eligible(job: Job) -> bool:
    """True unless every place the posting names is outside the US.

    The role title is read as well as the location cell: Palantir advertises its Warsaw
    and Paris internships as `... Internship - Poland` and `... - France` while the board
    reports the location as New York, so the title is the only country signal there.

    An unrecognized location is KEPT. Bare `Remote` and a bare city with no country are
    the common shapes of a US posting that simply did not spell out its country, and
    these boards are US-company boards, so treating silence as foreign would drop real
    opportunities to remove a few. A posting is only dropped on positive evidence.
    """
    if _FOREIGN_PLACE_PATTERN.search(normalize(job.role)):
        return False
    countries = {classify_location(segment) for segment in split_locations(job.location)}
    if Country.UNITED_STATES in countries:
        return True
    return Country.FOREIGN not in countries


def _is_narrow_scope_only(role: str) -> bool:
    """True when the role is scoped to frontend, mobile, security or testing and nothing
    broader. `Software Engineer Intern, Test Infrastructure` names infrastructure, which
    is work the user wants, so it survives on the same rule that keeps a role mentioning
    React among many things."""
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
    """True for a fresh US undergraduate summer internship in one of the interest areas."""
    role = normalize(job.role)
    if not _INTERNSHIP_PATTERN.search(role):
        return False
    if categorize(role) is RoleCategory.OTHER:
        return False
    if _is_rejected(role):
        return False
    if _is_graduate_only(role):
        return False
    if not _is_us_eligible(job):
        return False
    if not _is_fresh(job.posted_at, job.found_at):
        return False
    return _season_allows(job)


def _posting_rank(posted_at: date | None) -> int:
    """Ascending rank where newer postings sort first and undated postings sort last."""
    if posted_at is None:
        return UNDATED_POSTING_RANK
    return (date.max - posted_at).days


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
    """Union the US and unrecognized cities across the group, first spelling of a repeat wins.

    Foreign cities are dropped rather than shown. A posting open in both Palo Alto and
    Warsaw survives the US filter on its Palo Alto seat, and the user should not be reading
    Warsaw in a row they are meant to act on.

    Cities are ordered by geographic preference so that the three `join_locations` shows are
    the three the user cares most about, and so the cell does not depend on the order the
    sources happened to return.
    """
    merged: dict[str, str] = {}
    for job in jobs:
        for city in split_locations(job.location):
            if classify_location(city) is not Country.FOREIGN:
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
    complementary fields rather than noise. `source`, `platform` and `careers_url` come
    from the record whose apply URL is chosen, which keeps the row internally consistent. The role is chosen independently of the URL because it is display text, and
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
