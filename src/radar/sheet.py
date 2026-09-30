"""Google Sheets output layer: header management, dedupe keys, batched writes."""

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import TypeVar

import google.auth.exceptions
import gspread
import gspread.exceptions
import gspread.utils
from google.oauth2.service_account import Credentials

from radar.models import SHEET_HEADERS, Job, Platform, Source

logger = logging.getLogger(__name__)

SHEET_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
VALUE_INPUT_OPTION = "RAW"
INSERT_DATA_OPTION = "INSERT_ROWS"
READ_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

HEADER_ROW = 1
FIRST_DATA_ROW = 2
HEADER_RANGE = "A1"

WORKSHEET_TITLE = "Job Radar"
NEW_WORKSHEET_ROWS = 1000
NEW_WORKSHEET_COLS = len(SHEET_HEADERS)

COMPANY_INDEX = SHEET_HEADERS.index("Company")
ROLE_INDEX = SHEET_HEADERS.index("Role")
APPLY_URL_INDEX = SHEET_HEADERS.index("Application URL")
APPLY_URL_COLUMN_NUMBER = APPLY_URL_INDEX + 1
POSTING_DATE_COLUMN_NUMBER: int = SHEET_HEADERS.index("Posting Date") + 1

_KEY_PROBE_DATE = date.min
_KEY_PROBE_SOURCE = Source.POOL
_KEY_PROBE_PLATFORM = Platform.OTHER
_KEY_PROBE_TEXT = ""
_ResultT = TypeVar("_ResultT")


class SheetError(Exception):
    """Raised on sheet auth, configuration, or API failure."""


def _retry_transient_read(
    operation: Callable[[], _ResultT], description: str
) -> _ResultT:
    """Retry a read when Google returns a transient HTTP status."""
    retry_count: int = len(READ_RETRY_DELAYS_SECONDS)
    attempt_number: int
    delay_seconds: float
    for attempt_number, delay_seconds in enumerate(READ_RETRY_DELAYS_SECONDS, start=1):
        try:
            return operation()
        except gspread.exceptions.APIError as exc:
            status_code: int = exc.code
            retryable: bool = status_code in (408, 429) or 500 <= status_code <= 599
            if not retryable:
                raise
            logger.warning(
                "Google Sheets %s failed with HTTP %d; retrying in %.1f seconds (%d/%d)",
                description,
                status_code,
                delay_seconds,
                attempt_number,
                retry_count,
            )
            time.sleep(delay_seconds)
    return operation()


def identity_key(company: str, role: str) -> str:
    """Return the Job.key a job with this company and role would have.

    Builds a throwaway Job so the hashing scheme lives in exactly one place.
    Location and application URL are placeholders: neither is part of the key,
    which is what lets a moved posting update its row instead of adding one.
    """
    probe = Job(
        company=company,
        role=role,
        location=_KEY_PROBE_TEXT,
        remote=False,
        posted_at=None,
        found_at=_KEY_PROBE_DATE,
        source=_KEY_PROBE_SOURCE,
        apply_url=_KEY_PROBE_TEXT,
        careers_url=_KEY_PROBE_TEXT,
        platform=_KEY_PROBE_PLATFORM,
    )
    return probe.key


def _cell(row: Sequence[str], index: int) -> str:
    if index >= len(row):
        return ""
    return row[index].strip()


def _open_spreadsheet(sheet_id: str, service_account_json: str) -> gspread.Spreadsheet:
    try:
        account_info = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        raise SheetError(f"Service account JSON is not valid JSON: {exc}") from exc

    try:
        credentials = Credentials.from_service_account_info(account_info, scopes=list(SHEET_SCOPES))
    except (ValueError, KeyError) as exc:
        raise SheetError(f"Service account JSON is not a usable key: {exc}") from exc

    try:
        client = gspread.authorize(credentials)
        return _retry_transient_read(
            lambda: client.open_by_key(sheet_id), "spreadsheet metadata read"
        )
    except google.auth.exceptions.GoogleAuthError as exc:
        raise SheetError(f"Google authentication failed: {exc}") from exc
    except gspread.exceptions.GSpreadException as exc:
        raise SheetError(f"Could not open spreadsheet {sheet_id}: {exc}") from exc


def _own_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Return the tab job-radar owns, creating it if the spreadsheet has no such tab.

    Never positional: a spreadsheet's leftmost tab may be someone else's tracker.
    """
    try:
        return _retry_transient_read(
            lambda: spreadsheet.worksheet(WORKSHEET_TITLE), "worksheet metadata read"
        )
    except gspread.exceptions.WorksheetNotFound:
        logger.info("Creating worksheet %s", WORKSHEET_TITLE)
    except gspread.exceptions.GSpreadException as exc:
        raise SheetError(f"Could not open worksheet {WORKSHEET_TITLE}: {exc}") from exc

    try:
        return spreadsheet.add_worksheet(
            title=WORKSHEET_TITLE, rows=NEW_WORKSHEET_ROWS, cols=NEW_WORKSHEET_COLS
        )
    except gspread.exceptions.GSpreadException as exc:
        raise SheetError(f"Could not create worksheet {WORKSHEET_TITLE}: {exc}") from exc


def _widen_to_headers(worksheet: gspread.Worksheet) -> None:
    missing = len(SHEET_HEADERS) - worksheet.col_count
    if missing <= 0:
        return
    try:
        worksheet.add_cols(missing)
    except gspread.exceptions.GSpreadException as exc:
        raise SheetError(f"Could not widen sheet to fit headers: {exc}") from exc


def _ensure_headers(worksheet: gspread.Worksheet) -> None:
    try:
        header: list[str] = _retry_transient_read(
            lambda: worksheet.row_values(HEADER_ROW), "header read"
        )
    except gspread.exceptions.GSpreadException as exc:
        raise SheetError(f"Could not read the header row: {exc}") from exc

    if header == SHEET_HEADERS:
        return
    if any(cell.strip() for cell in header):
        raise SheetError(
            f"Row 1 of worksheet {WORKSHEET_TITLE!r} does not match the expected schema. "
            f"Found {len(header)} columns: {header!r}. "
            f"Expected {len(SHEET_HEADERS)} columns: {SHEET_HEADERS!r}. "
            "job-radar will not overwrite row 1. Either correct it in place, or delete the "
            "whole worksheet and let the next run rebuild it, which discards its contents."
        )

    _widen_to_headers(worksheet)
    try:
        worksheet.update(
            values=[SHEET_HEADERS],
            range_name=HEADER_RANGE,
            value_input_option=VALUE_INPUT_OPTION,
        )
    except gspread.exceptions.GSpreadException as exc:
        raise SheetError(f"Could not write the header row: {exc}") from exc
    logger.info("Wrote header row to empty sheet")


class JobSheet:
    """A single worksheet holding one posting per row, keyed by company and role."""

    def __init__(self, worksheet: gspread.Worksheet):
        self._worksheet = worksheet

    @classmethod
    def open(cls, sheet_id: str, service_account_json: str) -> "JobSheet":
        """Authorize with a service account key string and open the tab job-radar owns."""
        spreadsheet = _open_spreadsheet(sheet_id, service_account_json)
        worksheet = _own_worksheet(spreadsheet)
        _ensure_headers(worksheet)
        return cls(worksheet)

    def existing_keys(self) -> dict[str, int]:
        """Map Job.key to 1-based row number for every populated data row."""
        try:
            values: list[list[str]] = _retry_transient_read(
                self._worksheet.get_all_values, "sheet contents read"
            )
        except gspread.exceptions.GSpreadException as exc:
            raise SheetError(f"Could not read sheet contents: {exc}") from exc

        keys: dict[str, int] = {}
        for row_number, row in enumerate(values[HEADER_ROW:], start=FIRST_DATA_ROW):
            company = _cell(row, COMPANY_INDEX)
            role = _cell(row, ROLE_INDEX)
            if not company and not role:
                continue
            keys.setdefault(identity_key(company, role), row_number)
        return keys

    def append(self, jobs: Sequence[Job]) -> int:
        """Append jobs in one batch call. Returns the number of rows appended."""
        if not jobs:
            return 0
        rows = [job.to_row() for job in jobs]
        try:
            self._worksheet.append_rows(
                rows,
                value_input_option=VALUE_INPUT_OPTION,
                insert_data_option=INSERT_DATA_OPTION,
            )
        except gspread.exceptions.GSpreadException as exc:
            raise SheetError(f"Could not append {len(rows)} rows: {exc}") from exc
        logger.info("Appended %d rows", len(rows))
        return len(rows)

    def update_apply_urls(self, jobs: Sequence[Job], rows: Mapping[str, int]) -> int:
        """Rewrite the Application URL cell of already-present jobs in one batch call."""
        updates = []
        for job in jobs:
            row_number = rows.get(job.key)
            if row_number is None:
                logger.warning(
                    "No sheet row for %s - %s, skipping URL update", job.company, job.role
                )
                continue
            cell = gspread.utils.rowcol_to_a1(row_number, APPLY_URL_COLUMN_NUMBER)
            updates.append({"range": cell, "values": [[job.apply_url]]})

        if not updates:
            return 0
        try:
            self._worksheet.batch_update(updates, value_input_option=VALUE_INPUT_OPTION)
        except gspread.exceptions.GSpreadException as exc:
            raise SheetError(f"Could not update {len(updates)} application URLs: {exc}") from exc
        logger.info("Updated %d application URLs", len(updates))
        return len(updates)

    def sort_by_posting_date(self) -> None:
        """Sort complete data rows by posting date descending, preserving the header."""
        request: dict[str, object] = {
            "requests": [{"sortRange": {
                "range": {
                    "sheetId": self._worksheet.id,
                    "startRowIndex": HEADER_ROW,
                    "startColumnIndex": 0,
                    "endColumnIndex": self._worksheet.col_count,
                },
                "sortSpecs": [{
                    "dimensionIndex": POSTING_DATE_COLUMN_NUMBER - 1,
                    "sortOrder": "DESCENDING",
                }],
            }}],
        }
        try:
            self._worksheet.spreadsheet.batch_update(request)
        except gspread.exceptions.GSpreadException as exc:
            raise SheetError(f"Could not sort worksheet by posting date: {exc}") from exc
