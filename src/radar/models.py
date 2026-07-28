import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

DEFAULT_STATUS = "Not Applied"
KEY_LENGTH = 16
MAX_LOCATIONS_SHOWN = 3
LOCATION_SEPARATOR = "; "

SHEET_HEADERS = [
    "Company",
    "Role",
    "Location",
    "Remote?",
    "Posting Date",
    "Date Found",
    "Source",
    "Application URL",
    "Careers URL",
    "Platform",
    "Status",
]

_WHITESPACE = re.compile(r"\s+")
_SEASON_YEAR = re.compile(r"\b(?:spring|summer|fall|autumn|winter)?\s*(?:19|20)\d{2}\b")
_ROLE_EDGE_PUNCTUATION = " -,|/("


class Platform(Enum):
    GREENHOUSE = "Greenhouse"
    LEVER = "Lever"
    ASHBY = "Ashby"
    SMARTRECRUITERS = "SmartRecruiters"
    WORKDAY = "Workday"
    OTHER = "Other"


class Season(Enum):
    SPRING = "Spring"
    SUMMER = "Summer"
    FALL = "Fall"
    WINTER = "Winter"


class Source(Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    WORKDAY = "workday"
    SIMPLIFY = "simplify"


def normalize(text: str) -> str:
    """Casefold and collapse whitespace, for dedupe keys and keyword matching."""
    return _WHITESPACE.sub(" ", text.strip().lower())


def role_identity(role: str) -> str:
    """Role text stripped of season and year decoration, for dedupe.

    The same requisition is titled inconsistently across sources: Greenhouse
    lists Anduril's as "2027 Software Engineer Intern" while the aggregator
    lists it as "Software Engineer Intern".
    """
    without_dates = _SEASON_YEAR.sub(" ", normalize(role))
    return _WHITESPACE.sub(" ", without_dates).strip(_ROLE_EDGE_PUNCTUATION)


def join_locations(locations: Sequence[str]) -> str:
    """Render a posting's locations as one cell, truncating a long city list."""
    unique = list(dict.fromkeys(loc.strip() for loc in locations if loc.strip()))
    if not unique:
        return ""
    shown = unique[:MAX_LOCATIONS_SHOWN]
    remaining = len(unique) - len(shown)
    joined = LOCATION_SEPARATOR.join(shown)
    if remaining:
        return f"{joined}{LOCATION_SEPARATOR}+{remaining} more"
    return joined


@dataclass(frozen=True, slots=True)
class Job:
    company: str
    role: str
    location: str
    remote: bool
    posted_at: date | None
    found_at: date
    source: Source
    apply_url: str
    careers_url: str
    platform: Platform
    status: str = DEFAULT_STATUS
    season: Season | None = None

    @property
    def key(self) -> str:
        """Dedupe identity: one row per role per company.

        Location and URL are excluded on purpose. Employers routinely post one
        requisition per city, and the same requisition reaches us from several
        sources under different URL forms, so including either splits a single
        opportunity across many rows. Excluding the URL is also what lets a
        moved posting update its existing row instead of appearing as new.
        """
        raw = f"{normalize(self.company)}|{role_identity(self.role)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:KEY_LENGTH]

    def to_row(self) -> list[str]:
        return [
            self.company,
            self.role,
            self.location,
            "Yes" if self.remote else "No",
            self.posted_at.isoformat() if self.posted_at else "",
            self.found_at.isoformat(),
            self.source.value,
            self.apply_url,
            self.careers_url,
            self.platform.value,
            self.status,
        ]
