"""On-disk dedupe state, so each run only reports postings the user has not seen.

Exports:
    STATE_VERSION -- schema version written to disk.
    StateError -- raised when an existing state file cannot be parsed.
    Diff -- new postings and postings whose application URL changed.
    SeenJob -- one recorded posting.
    Store -- load, diff, record and save the seen-posting set.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import NamedTuple

from radar.models import Job

logger = logging.getLogger(__name__)

STATE_VERSION = 1
VERSION_FIELD = "version"
JOBS_FIELD = "jobs"
JSON_INDENT = 2


class StateError(Exception):
    """Raised when a state file exists but does not hold a readable state document."""


class Diff(NamedTuple):
    new: list[Job]
    changed: list[Job]


@dataclass
class SeenJob:
    apply_url: str
    first_seen: str
    company: str
    role: str
    location: str


SEEN_JOB_FIELDS = tuple(field.name for field in fields(SeenJob))


class Store:
    """The set of postings seen by previous runs, keyed by `Job.key`."""

    def __init__(self, jobs: dict[str, SeenJob] | None = None) -> None:
        self._jobs: dict[str, SeenJob] = jobs if jobs is not None else {}

    @classmethod
    def load(cls, path: Path) -> "Store":
        """Read state from `path`. A missing or empty file yields an empty store."""
        if not path.exists():
            logger.debug("No state file at %s, starting empty", path)
            return cls()
        document = path.read_text(encoding="utf-8").strip()
        if not document:
            logger.debug("Empty state file at %s, starting empty", path)
            return cls()
        try:
            payload = json.loads(document)
        except json.JSONDecodeError as exc:
            raise StateError(f"State file {path} is not valid JSON: {exc}") from exc
        return cls(_parse_jobs(payload, path))

    def diff(self, jobs: Sequence[Job]) -> Diff:
        """Split `jobs` into never-seen postings and postings with a new application URL."""
        new: list[Job] = []
        changed: list[Job] = []
        for job in jobs:
            seen = self._jobs.get(job.key)
            if seen is None:
                new.append(job)
            elif seen.apply_url != job.apply_url:
                changed.append(job)
        return Diff(new=new, changed=changed)

    def record(self, jobs: Sequence[Job]) -> None:
        """Mark `jobs` as seen, preserving the original `first_seen` for known keys."""
        for job in jobs:
            known = self._jobs.get(job.key)
            self._jobs[job.key] = SeenJob(
                apply_url=job.apply_url,
                first_seen=known.first_seen if known else job.found_at.isoformat(),
                company=job.company,
                role=job.role,
                location=job.location,
            )

    def save(self, path: Path) -> None:
        """Write state with sorted keys and a trailing newline, for stable git diffs."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            VERSION_FIELD: STATE_VERSION,
            JOBS_FIELD: {key: asdict(seen) for key, seen in self._jobs.items()},
        }
        document = json.dumps(payload, indent=JSON_INDENT, sort_keys=True)
        path.write_text(f"{document}\n", encoding="utf-8")
        logger.info("Saved %d tracked postings to %s", len(self._jobs), path)


def _parse_jobs(payload: object, path: Path) -> dict[str, SeenJob]:
    if not isinstance(payload, dict):
        raise StateError(f"State file {path} must hold a JSON object")
    entries = payload.get(JOBS_FIELD, {})
    if not isinstance(entries, dict):
        raise StateError(f"State file {path} field {JOBS_FIELD!r} must hold a JSON object")
    return {key: _parse_seen_job(key, entry, path) for key, entry in entries.items()}


def _parse_seen_job(key: str, entry: object, path: Path) -> SeenJob:
    if not isinstance(entry, dict):
        raise StateError(f"State file {path} entry {key!r} must hold a JSON object")
    missing = [field for field in SEEN_JOB_FIELDS if field not in entry]
    if missing:
        raise StateError(f"State file {path} entry {key!r} is missing {', '.join(missing)}")
    return SeenJob(**{field: entry[field] for field in SEEN_JOB_FIELDS})
