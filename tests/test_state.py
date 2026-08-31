"""Offline tests for the dedupe state store."""

import json
from datetime import date
from pathlib import Path

import pytest

from radar.models import Job, Platform, Source
from radar.state import JOBS_FIELD, STATE_VERSION, VERSION_FIELD, StateError, Store

FOUND_AT = date(2026, 7, 27)


def make_job(
    role: str = "Software Engineering Intern",
    company: str = "OpenAI",
    location: str = "San Francisco, CA",
    apply_url: str = "https://boards.example.com/apply/1",
    posted_at: date | None = date(2026, 7, 20),
) -> Job:
    return Job(
        company=company,
        role=role,
        location=location,
        remote=False,
        posted_at=posted_at,
        found_at=FOUND_AT,
        source=Source.POOL,
        apply_url=apply_url,
        careers_url="https://example.com/careers",
        platform=Platform.GREENHOUSE,
    )


def test_load_missing_file_is_empty_store(tmp_path: Path) -> None:
    store = Store.load(tmp_path / "absent.json")
    job = make_job()
    assert store.diff([job]).new == [job]


def test_load_empty_file_is_empty_store(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("", encoding="utf-8")
    job = make_job()
    assert Store.load(path).diff([job]).new == [job]


def test_round_trip_marks_recorded_jobs_as_seen(tmp_path: Path) -> None:
    path = tmp_path / "data" / "state.json"
    job = make_job()
    store = Store()
    store.record([job])
    store.save(path)

    reloaded = Store.load(path)
    diff = reloaded.diff([job])
    assert diff.new == []
    assert diff.changed == []


def test_saved_document_matches_specified_shape(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    job = make_job()
    store = Store()
    store.record([job])
    store.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[VERSION_FIELD] == STATE_VERSION
    assert payload[JOBS_FIELD][job.key] == {
        "apply_url": job.apply_url,
        "first_seen": FOUND_AT.isoformat(),
        "company": job.company,
        "role": job.role,
        "location": job.location,
    }


def test_diff_reports_unseen_postings_as_new() -> None:
    store = Store()
    store.record([make_job()])

    fresh = make_job(role="ML Infrastructure Intern")
    diff = store.diff([make_job(), fresh])
    assert diff.new == [fresh]
    assert diff.changed == []


def test_diff_reports_a_moved_application_url_as_changed_not_new() -> None:
    store = Store()
    store.record([make_job(apply_url="https://boards.example.com/apply/1")])

    moved = make_job(apply_url="https://boards.example.com/apply/2")
    diff = store.diff([moved])
    assert diff.new == []
    assert diff.changed == [moved]


def test_diff_ignores_a_relocated_posting_whose_url_is_unchanged() -> None:
    store = Store()
    store.record([make_job(location="San Francisco, CA")])

    relocated = make_job(location="San Francisco, CA; Seattle, WA")
    diff = store.diff([relocated])
    assert diff.new == []
    assert diff.changed == []


def test_record_preserves_first_seen_across_runs(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = Store()
    store.record([make_job()])

    later_run = Job(
        company="OpenAI",
        role="Software Engineering Intern",
        location="San Francisco, CA; Seattle, WA",
        remote=False,
        posted_at=date(2026, 7, 20),
        found_at=date(2026, 8, 3),
        source=Source.POOL,
        apply_url="https://boards.example.com/apply/1",
        careers_url="https://example.com/careers",
        platform=Platform.GREENHOUSE,
    )
    store.record([later_run])
    store.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    entry = payload[JOBS_FIELD][later_run.key]
    assert entry["first_seen"] == FOUND_AT.isoformat()
    assert entry["location"] == later_run.location


def test_save_is_byte_stable_and_newline_terminated(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    store = Store()
    store.record([make_job(), make_job(role="ML Infrastructure Intern")])
    store.save(first)
    store.save(second)

    document = first.read_bytes()
    assert document == second.read_bytes()
    assert document.endswith(b"\n")
    lines = document.decode("utf-8").splitlines()
    assert lines[1].startswith("  ")


def test_corrupt_json_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "jobs": {', encoding="utf-8")
    with pytest.raises(StateError):
        Store.load(path)


def test_non_object_document_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(StateError):
        Store.load(path)


def test_entry_missing_fields_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({VERSION_FIELD: STATE_VERSION, JOBS_FIELD: {"abc": {"role": "x"}}}),
        encoding="utf-8",
    )
    with pytest.raises(StateError):
        Store.load(path)
