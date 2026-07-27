"""Offline tests for the pipeline wiring, focused on sheet and state reconciliation."""

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import pytest

from radar import cli
from radar.cli import AppendPlan, ExitCode, _publish, _reconcile_with_sheet, main
from radar.models import Job, Platform, Source
from radar.sheet import SheetError, identity_key
from radar.state import Store

FOUND_AT = date(2026, 7, 27)
SHEET_ID = "sheet-123"
SERVICE_ACCOUNT_JSON = json.dumps({"type": "service_account"})
FIRST_DATA_ROW = 2


def make_job(
    role: str = "Software Engineering Intern",
    company: str = "OpenAI",
    apply_url: str = "https://boards.example.com/apply/1",
    location: str = "San Francisco, CA",
) -> Job:
    return Job(
        company=company,
        role=role,
        location=location,
        remote=False,
        posted_at=date(2026, 7, 20),
        found_at=FOUND_AT,
        source=Source.GREENHOUSE,
        apply_url=apply_url,
        careers_url="https://example.com/careers",
        platform=Platform.GREENHOUSE,
    )


class FakeSheet:
    """Records what the CLI asks of a sheet. Patched in where `JobSheet` normally sits,
    so `JobSheet.open(...)` resolves to `open` on this instance and hands back itself."""

    def __init__(self, present: Sequence[Job] = ()) -> None:
        self.rows = {
            job.key: FIRST_DATA_ROW + offset for offset, job in enumerate(present)
        }
        self.appended: list[Job] = []
        self.url_updates: list[Job] = []
        self.existing_keys_calls = 0

    def open(self, sheet_id: str, service_account_json: str) -> "FakeSheet":
        return self

    def existing_keys(self) -> dict[str, int]:
        self.existing_keys_calls += 1
        return dict(self.rows)

    def append(self, jobs: Sequence[Job]) -> int:
        self.appended.extend(jobs)
        return len(jobs)

    def update_apply_urls(self, jobs: Sequence[Job], rows: Mapping[str, int]) -> int:
        present = [job for job in jobs if job.key in rows]
        self.url_updates.extend(present)
        return len(present)


class FailingSheet:
    """A sheet whose authorization fails, to exercise the fatal path."""

    def open(self, sheet_id: str, service_account_json: str) -> "FailingSheet":
        raise SheetError("service account rejected")


class UnusableSheet:
    """Fails the test if the CLI opens a sheet on a path that must stay offline."""

    def open(self, sheet_id: str, service_account_json: str) -> "UnusableSheet":
        raise AssertionError("the sheet must not be opened on this path")


def fake_fetch_all(boards: Sequence[object], found_at: date) -> list[Job]:
    return [make_job()]


@pytest.fixture
def sheet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.ENV_SHEET_ID, SHEET_ID)
    monkeypatch.setenv(cli.ENV_SERVICE_ACCOUNT_JSON, SERVICE_ACCOUNT_JSON)
    for name in cli.EMAIL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_reconcile_splits_new_jobs_by_what_the_sheet_holds() -> None:
    present = make_job(role="ML Infrastructure Intern")
    absent = make_job(role="Software Engineering Intern")
    plan = _reconcile_with_sheet([present, absent], {present.key: FIRST_DATA_ROW})
    assert plan == AppendPlan(to_append=[absent], already_present=[present])


def test_reconcile_is_quiet_when_state_and_sheet_agree(
    caplog: pytest.LogCaptureFixture,
) -> None:
    jobs = [make_job(), make_job(role="ML Infrastructure Intern")]
    with caplog.at_level(logging.INFO, logger=cli.logger.name):
        plan = _reconcile_with_sheet(jobs, {})
    assert plan == AppendPlan(to_append=jobs, already_present=[])
    assert caplog.records == []


def test_reconcile_logs_the_drift_count(caplog: pytest.LogCaptureFixture) -> None:
    present = make_job()
    with caplog.at_level(logging.INFO, logger=cli.logger.name):
        _reconcile_with_sheet([present], {present.key: FIRST_DATA_ROW})
    assert "already in the sheet" in caplog.text
    assert "1 job(s)" in caplog.text


@pytest.mark.usefixtures("sheet_env")
def test_publish_does_not_append_a_job_the_sheet_already_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    drifted = make_job(role="Software Engineering Intern")
    genuinely_new = make_job(role="ML Infrastructure Intern")
    sheet = FakeSheet(present=[drifted])
    monkeypatch.setattr(cli, "JobSheet", sheet)

    store = Store()
    ranked = [drifted, genuinely_new]
    exit_code = _publish(store.diff(ranked), ranked, store, tmp_path / "state.json")

    assert exit_code == ExitCode.OK
    assert sheet.appended == [genuinely_new]
    assert sheet.existing_keys_calls == 1


@pytest.mark.usefixtures("sheet_env")
def test_publish_appends_everything_when_state_and_sheet_agree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ranked = [make_job(), make_job(role="ML Infrastructure Intern")]
    sheet = FakeSheet()
    monkeypatch.setattr(cli, "JobSheet", sheet)

    store = Store()
    exit_code = _publish(store.diff(ranked), ranked, store, tmp_path / "state.json")

    assert exit_code == ExitCode.OK
    assert sheet.appended == ranked
    assert sheet.url_updates == []


@pytest.mark.usefixtures("sheet_env")
def test_publish_refreshes_the_apply_url_of_a_drifted_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    drifted = make_job(apply_url="https://boards.example.com/apply/moved")
    sheet = FakeSheet(present=[drifted])
    monkeypatch.setattr(cli, "JobSheet", sheet)

    store = Store()
    _publish(store.diff([drifted]), [drifted], store, tmp_path / "state.json")

    assert sheet.appended == []
    assert sheet.url_updates == [drifted]


@pytest.mark.usefixtures("sheet_env")
def test_publish_suppresses_a_row_whose_sheet_title_differs_only_by_year(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sheet holds whatever title an earlier run displayed, and the merged title can
    change when a differently decorated variant wins, so matching must survive that."""
    this_run = make_job(role="2027 Software Engineer Intern", company="Anduril")
    sheet = FakeSheet()
    sheet.rows = {identity_key("Anduril", "Software Engineer Intern"): FIRST_DATA_ROW}
    monkeypatch.setattr(cli, "JobSheet", sheet)

    store = Store()
    _publish(store.diff([this_run]), [this_run], store, tmp_path / "state.json")

    assert sheet.appended == []
    assert sheet.url_updates == [this_run]


@pytest.mark.usefixtures("sheet_env")
def test_digest_counts_only_the_rows_actually_appended(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    drifted = make_job(role="Software Engineering Intern")
    genuinely_new = make_job(role="ML Infrastructure Intern")
    monkeypatch.setattr(cli, "JobSheet", FakeSheet(present=[drifted]))

    store = Store()
    ranked = [drifted, genuinely_new]
    _publish(store.diff(ranked), ranked, store, tmp_path / "state.json")

    digest = capsys.readouterr().out
    assert "Found: 1 new job" in digest
    assert "ML Infrastructure Intern" in digest
    assert "Software Engineering Intern" not in digest


@pytest.mark.usefixtures("sheet_env")
def test_publish_records_the_full_ranked_set_so_drift_self_heals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    drifted = make_job(role="Software Engineering Intern")
    genuinely_new = make_job(role="ML Infrastructure Intern")
    monkeypatch.setattr(cli, "JobSheet", FakeSheet(present=[drifted]))
    state_path = tmp_path / "state.json"

    store = Store()
    ranked = [drifted, genuinely_new]
    _publish(store.diff(ranked), ranked, store, state_path)

    healed = Store.load(state_path)
    assert healed.diff(ranked).new == []
    assert healed.diff(ranked).changed == []

    second_sheet = FakeSheet(present=[drifted])
    monkeypatch.setattr(cli, "JobSheet", second_sheet)
    _publish(healed.diff(ranked), ranked, healed, state_path)
    assert second_sheet.appended == []


@pytest.mark.usefixtures("sheet_env")
def test_publish_leaves_state_unsaved_when_the_sheet_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "JobSheet", FailingSheet())
    state_path = tmp_path / "state.json"

    store = Store()
    ranked = [make_job()]
    exit_code = _publish(store.diff(ranked), ranked, store, state_path)

    assert exit_code == ExitCode.SHEET_ERROR
    assert not state_path.exists()


def test_dry_run_needs_no_credentials_and_never_opens_the_sheet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (*cli.SHEET_ENV_VARS, *cli.EMAIL_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "JobSheet", UnusableSheet())
    monkeypatch.setattr(cli, "fetch_all", fake_fetch_all)
    state_path = tmp_path / "state.json"

    exit_code = main(["--dry-run", "--state", str(state_path)])

    assert exit_code == ExitCode.OK
    assert not state_path.exists()
    assert "OpenAI" in capsys.readouterr().out


@pytest.mark.usefixtures("sheet_env")
def test_missing_credentials_are_a_config_error_outside_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(cli.ENV_SHEET_ID, raising=False)
    monkeypatch.setattr(cli, "JobSheet", UnusableSheet())
    monkeypatch.setattr(cli, "fetch_all", fake_fetch_all)

    exit_code = main(["--state", str(tmp_path / "state.json")])

    assert exit_code == ExitCode.CONFIG_ERROR
