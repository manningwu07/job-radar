import copy
import json
from datetime import date

import gspread.exceptions
import gspread.utils
import pytest

from radar import sheet
from radar.models import SHEET_HEADERS, Job, Platform, Source, join_locations

SERVICE_ACCOUNT_JSON = json.dumps({"type": "service_account", "project_id": "job-radar"})
SHEET_ID = "sheet-abc123"
APPLY_URL_COLUMN_LETTER = "H"
DEFAULT_COL_COUNT = len(SHEET_HEADERS)
JOINED_LOCATION = join_locations(["Mountain View, CA", "Bellevue, WA", "Austin, TX", "Chicago, IL"])
STALE_HEADER_ROW = SHEET_HEADERS[:-1] + ["Notes", "Status"]
TRACKER_HEADER_ROW = [
    "Company",
    "Role",
    "Location",
    "Role Family",
    "Priority",
    "Status",
    "Date Applied",
    "Referral / Contact",
    "Resume",
    "Direct Link",
    "Follow-up",
]
APPLICATIONS_TRACKER_ROWS = [
    ["Total tracked", "Submitted", "Needs confirmation", "In progress"],
    ["8", "8", "0", "0"],
    [],
    [],
    list(TRACKER_HEADER_ROW),
    [
        "Google",
        "",
        "Mountain View, CA",
        "SWE",
        "Dream",
        "In Progress",
        "2026-07-24",
        "Brandon Xu",
        "SWE resume v3",
        "https://careers.google.com/jobs/results/1",
        "",
    ],
]


def make_row(company="", role="", location="", apply_url=""):
    row = [""] * len(SHEET_HEADERS)
    row[SHEET_HEADERS.index("Company")] = company
    row[SHEET_HEADERS.index("Role")] = role
    row[SHEET_HEADERS.index("Location")] = location
    row[SHEET_HEADERS.index("Application URL")] = apply_url
    return row


def make_job(
    company="Google",
    role="Software Engineering Intern",
    location="Mountain View, CA",
    apply_url="https://jobs.example.com/1",
):
    return Job(
        company=company,
        role=role,
        location=location,
        remote=False,
        posted_at=date(2026, 7, 1),
        found_at=date(2026, 7, 27),
        source=Source.GREENHOUSE,
        apply_url=apply_url,
        careers_url="https://example.com/careers",
        platform=Platform.GREENHOUSE,
    )


class FakeWorksheet:
    def __init__(self, values=None, col_count=DEFAULT_COL_COUNT):
        self.values = [list(row) for row in (values or [])]
        self.col_count = col_count
        self.calls = []
        self.raise_on_append = None

    def row_values(self, row_number):
        self.calls.append(("row_values", row_number))
        if row_number > len(self.values):
            return []
        return list(self.values[row_number - 1])

    def get_all_values(self):
        self.calls.append(("get_all_values",))
        return [list(row) for row in self.values]

    def add_cols(self, count):
        self.calls.append(("add_cols", count))
        self.col_count += count

    def update(self, values, range_name=None, value_input_option=None):
        self.calls.append(("update", range_name, values, value_input_option))
        start_row, start_column = gspread.utils.a1_to_rowcol(range_name)
        for row_offset, row in enumerate(values):
            for column_offset, value in enumerate(row):
                self._write_cell(start_row + row_offset, start_column + column_offset, value)

    def append_rows(self, values, value_input_option=None, insert_data_option=None):
        self.calls.append(("append_rows", values, value_input_option, insert_data_option))
        if self.raise_on_append is not None:
            raise self.raise_on_append
        self.values.extend(list(row) for row in values)

    def batch_update(self, data, value_input_option=None):
        self.calls.append(("batch_update", list(data), value_input_option))
        for entry in data:
            row_number, column_number = gspread.utils.a1_to_rowcol(entry["range"])
            for offset, value in enumerate(entry["values"][0]):
                self._write_cell(row_number, column_number + offset, value)

    def _write_cell(self, row_number, column_number, value):
        while len(self.values) < row_number:
            self.values.append([])
        row = self.values[row_number - 1]
        while len(row) < column_number:
            row.append("")
        row[column_number - 1] = value

    def call_names(self):
        return [call[0] for call in self.calls]


class FakeCredentials:
    def __init__(self, info, scopes):
        self.info = info
        self.scopes = scopes

    @classmethod
    def from_service_account_info(cls, info, scopes):
        return cls(info, list(scopes))


class FakeSpreadsheet:
    def __init__(self, tabs=None):
        self.tabs = dict(tabs or {})
        self.requested_titles = []
        self.added_worksheets = []

    def worksheet(self, title):
        self.requested_titles.append(title)
        if title not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.added_worksheets.append((title, rows, cols))
        created = FakeWorksheet(col_count=cols)
        self.tabs[title] = created
        return created


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet
        self.opened_key = None

    def open_by_key(self, key):
        self.opened_key = key
        return self.spreadsheet


class FakeAuthorizer:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet
        self.client = FakeClient(spreadsheet)
        self.credentials = None

    def __call__(self, credentials):
        self.credentials = credentials
        return self.client


def patch_google_spreadsheet(monkeypatch, spreadsheet):
    authorizer = FakeAuthorizer(spreadsheet)
    monkeypatch.setattr(sheet, "Credentials", FakeCredentials)
    monkeypatch.setattr(sheet.gspread, "authorize", authorizer)
    return authorizer


def patch_google(monkeypatch, worksheet):
    return patch_google_spreadsheet(
        monkeypatch, FakeSpreadsheet(tabs={sheet.WORKSHEET_TITLE: worksheet})
    )


def test_scopes_are_minimal():
    assert sheet.SHEET_SCOPES == ("https://www.googleapis.com/auth/spreadsheets",)


def test_worksheet_title_is_a_module_constant():
    assert sheet.WORKSHEET_TITLE == "Job Radar"


def test_open_writes_headers_on_empty_sheet(monkeypatch):
    worksheet = FakeWorksheet()
    authorizer = patch_google(monkeypatch, worksheet)

    sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)

    assert authorizer.client.opened_key == SHEET_ID
    assert authorizer.spreadsheet.requested_titles == [sheet.WORKSHEET_TITLE]
    assert authorizer.credentials.scopes == list(sheet.SHEET_SCOPES)
    assert ("update", "A1", [SHEET_HEADERS], sheet.VALUE_INPUT_OPTION) in worksheet.calls


def test_open_creates_the_dedicated_tab_when_absent(monkeypatch):
    spreadsheet = FakeSpreadsheet()
    authorizer = patch_google_spreadsheet(monkeypatch, spreadsheet)

    job_sheet = sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)

    assert authorizer.spreadsheet.requested_titles == [sheet.WORKSHEET_TITLE]
    assert len(spreadsheet.added_worksheets) == 1
    title, rows, cols = spreadsheet.added_worksheets[0]
    assert title == sheet.WORKSHEET_TITLE
    assert cols >= len(SHEET_HEADERS)
    assert rows > 1
    created = spreadsheet.tabs[sheet.WORKSHEET_TITLE]
    assert ("update", "A1", [SHEET_HEADERS], sheet.VALUE_INPUT_OPTION) in created.calls
    assert job_sheet.existing_keys() == {}


def test_open_reuses_the_dedicated_tab_and_leaves_matching_headers_untouched(monkeypatch):
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])
    authorizer = patch_google(monkeypatch, worksheet)

    sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)

    assert authorizer.spreadsheet.added_worksheets == []
    assert "update" not in worksheet.call_names()


def test_open_never_touches_a_foreign_leftmost_tab(monkeypatch):
    tracker = FakeWorksheet(values=copy.deepcopy(APPLICATIONS_TRACKER_ROWS))
    spreadsheet = FakeSpreadsheet(tabs={"Applications": tracker})
    patch_google_spreadsheet(monkeypatch, spreadsheet)

    sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)

    assert spreadsheet.requested_titles == [sheet.WORKSHEET_TITLE]
    assert [title for title, _, _ in spreadsheet.added_worksheets] == [sheet.WORKSHEET_TITLE]
    assert tracker.calls == []
    assert tracker.values == APPLICATIONS_TRACKER_ROWS


def test_open_raises_on_mismatched_headers(monkeypatch):
    worksheet = FakeWorksheet(values=[list(TRACKER_HEADER_ROW)])
    patch_google(monkeypatch, worksheet)

    with pytest.raises(sheet.SheetError) as excinfo:
        sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)

    message = str(excinfo.value)
    assert "Role Family" in message
    assert "Application URL" in message
    assert worksheet.call_names() == ["row_values"]


def test_open_refuses_a_header_from_an_earlier_schema(monkeypatch):
    worksheet = FakeWorksheet(values=[list(STALE_HEADER_ROW)])
    patch_google(monkeypatch, worksheet)

    with pytest.raises(sheet.SheetError) as excinfo:
        sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)

    message = str(excinfo.value)
    assert sheet.WORKSHEET_TITLE in message
    assert f"Found {len(STALE_HEADER_ROW)} columns" in message
    assert f"Expected {len(SHEET_HEADERS)} columns" in message
    assert "'Notes'" in message
    assert worksheet.call_names() == ["row_values"]


def test_open_rebuilds_the_tab_after_it_is_deleted(monkeypatch):
    spreadsheet = FakeSpreadsheet()
    patch_google_spreadsheet(monkeypatch, spreadsheet)
    first = sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)
    first.append([make_job()])

    del spreadsheet.tabs[sheet.WORKSHEET_TITLE]
    rebuilt = sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)

    assert [title for title, _, _ in spreadsheet.added_worksheets] == [
        sheet.WORKSHEET_TITLE,
        sheet.WORKSHEET_TITLE,
    ]
    fresh = spreadsheet.tabs[sheet.WORKSHEET_TITLE]
    assert fresh.values == [list(SHEET_HEADERS)]
    assert rebuilt.existing_keys() == {}


def test_open_widens_narrow_sheet_before_writing_headers(monkeypatch):
    worksheet = FakeWorksheet(col_count=3)
    patch_google(monkeypatch, worksheet)

    sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)

    assert ("add_cols", len(SHEET_HEADERS) - 3) in worksheet.calls
    assert worksheet.col_count == len(SHEET_HEADERS)


def test_open_raises_on_invalid_service_account_json(monkeypatch):
    worksheet = FakeWorksheet()
    patch_google(monkeypatch, worksheet)

    with pytest.raises(sheet.SheetError):
        sheet.JobSheet.open(SHEET_ID, "not json at all")


def test_open_wraps_gspread_failure(monkeypatch):
    worksheet = FakeWorksheet()
    authorizer = patch_google(monkeypatch, worksheet)

    def explode(key):
        raise gspread.exceptions.SpreadsheetNotFound("missing")

    monkeypatch.setattr(authorizer.client, "open_by_key", explode)

    with pytest.raises(sheet.SheetError):
        sheet.JobSheet.open(SHEET_ID, SERVICE_ACCOUNT_JSON)


def test_existing_keys_match_job_key():
    job = make_job(location=JOINED_LOCATION)
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS), job.to_row()])

    keys = sheet.JobSheet(worksheet).existing_keys()

    assert keys == {job.key: 2}


def test_existing_keys_collapse_rows_differing_only_by_location():
    mountain_view = make_job(location="Mountain View, CA")
    bellevue = make_job(location="Bellevue, WA")
    worksheet = FakeWorksheet(
        values=[list(SHEET_HEADERS), mountain_view.to_row(), bellevue.to_row()]
    )

    keys = sheet.JobSheet(worksheet).existing_keys()

    assert mountain_view.key == bellevue.key
    assert keys == {mountain_view.key: 2}


def test_existing_keys_collapse_rows_differing_only_by_apply_url():
    greenhouse = make_job(apply_url="https://boards.greenhouse.io/anduril/jobs/5148079007")
    job_boards = make_job(apply_url="https://job-boards.greenhouse.io/anduril/jobs/5148079007")
    worksheet = FakeWorksheet(
        values=[list(SHEET_HEADERS), greenhouse.to_row(), job_boards.to_row()]
    )

    keys = sheet.JobSheet(worksheet).existing_keys()

    assert greenhouse.key == job_boards.key
    assert keys == {greenhouse.key: 2}


def test_existing_keys_separate_rows_differing_by_company_or_role():
    baseline = make_job()
    other_company = make_job(company="Anthropic")
    other_role = make_job(role="ML Systems Intern")
    worksheet = FakeWorksheet(
        values=[
            list(SHEET_HEADERS),
            baseline.to_row(),
            other_company.to_row(),
            other_role.to_row(),
        ]
    )

    keys = sheet.JobSheet(worksheet).existing_keys()

    assert len({baseline.key, other_company.key, other_role.key}) == 3
    assert keys == {baseline.key: 2, other_company.key: 3, other_role.key: 4}


def test_existing_keys_normalize_case_and_whitespace():
    job = make_job()
    messy_row = make_row(
        company="  GOOGLE ",
        role="Software   Engineering INTERN",
        location="Somewhere Else, ZZ",
        apply_url="https://a-completely-different-host.example.com/9",
    )
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS), messy_row])

    keys = sheet.JobSheet(worksheet).existing_keys()

    assert keys == {job.key: 2}


def test_existing_keys_skip_blank_rows_and_keep_first_duplicate():
    job = make_job()
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS), job.to_row(), [], job.to_row()])

    keys = sheet.JobSheet(worksheet).existing_keys()

    assert keys == {job.key: 2}


def test_joined_location_and_url_round_trip_through_append():
    job = make_job(location=JOINED_LOCATION)
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])
    job_sheet = sheet.JobSheet(worksheet)

    job_sheet.append([job])
    stored = worksheet.values[1]

    assert stored[SHEET_HEADERS.index("Location")] == JOINED_LOCATION
    assert stored[sheet.APPLY_URL_INDEX] == job.apply_url
    assert job_sheet.existing_keys() == {job.key: 2}


def test_append_makes_one_batch_call_with_to_row_order():
    first = make_job(role="Software Engineering Intern")
    second = make_job(company="Anthropic", role="ML Systems Intern", apply_url="https://a/2")
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])

    appended = sheet.JobSheet(worksheet).append([first, second])

    assert appended == 2
    assert worksheet.call_names() == ["append_rows"]
    _, rows, value_input_option, insert_data_option = worksheet.calls[0]
    assert rows == [first.to_row(), second.to_row()]
    assert value_input_option == sheet.VALUE_INPUT_OPTION
    assert insert_data_option == sheet.INSERT_DATA_OPTION


def test_append_sends_formula_looking_cells_verbatim_as_raw():
    job = make_job(
        location="+2 more; -Remote",
        apply_url="https://jobs.example.com/apply?q=1&r=2",
    )
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])

    sheet.JobSheet(worksheet).append([job])

    _, rows, value_input_option, _ = worksheet.calls[0]
    assert rows == [job.to_row()]
    assert value_input_option == "RAW"


def test_append_empty_makes_no_api_calls():
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])

    assert sheet.JobSheet(worksheet).append([]) == 0
    assert worksheet.calls == []


def test_append_wraps_api_failure():
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])
    worksheet.raise_on_append = gspread.exceptions.GSpreadException("quota")

    with pytest.raises(sheet.SheetError):
        sheet.JobSheet(worksheet).append([make_job()])


def test_update_apply_urls_touches_only_the_url_column():
    first = make_job(apply_url="https://new/1")
    second = make_job(company="Stripe", role="Backend Intern", apply_url="https://new/2")
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])
    rows = {first.key: 4, second.key: 9}

    updated = sheet.JobSheet(worksheet).update_apply_urls([first, second], rows)

    assert updated == 2
    assert worksheet.call_names() == ["batch_update"]
    _, data, value_input_option = worksheet.calls[0]
    assert data == [
        {"range": "H4", "values": [["https://new/1"]]},
        {"range": "H9", "values": [["https://new/2"]]},
    ]
    assert value_input_option == sheet.VALUE_INPUT_OPTION
    assert all(entry["range"].startswith(APPLY_URL_COLUMN_LETTER) for entry in data)


def test_update_apply_urls_skips_jobs_with_no_known_row():
    known = make_job(apply_url="https://new/1")
    unknown = make_job(company="Figma", role="Platform Intern")
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])

    updated = sheet.JobSheet(worksheet).update_apply_urls([known, unknown], {known.key: 2})

    assert updated == 1
    _, data, _ = worksheet.calls[0]
    assert data == [{"range": "H2", "values": [["https://new/1"]]}]


def test_update_apply_urls_moves_url_without_disturbing_hand_edited_cells():
    listed = make_job(apply_url="https://boards.greenhouse.io/anduril/jobs/5148079007")
    neighbour = make_job(company="Anthropic", role="ML Systems Intern", apply_url="https://a/2")
    hand_edited = listed.to_row()
    hand_edited[SHEET_HEADERS.index("Status")] = "Applied 2026-07-20"
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS), hand_edited, neighbour.to_row()])
    job_sheet = sheet.JobSheet(worksheet)
    rows = job_sheet.existing_keys()

    moved = make_job(apply_url="https://job-boards.greenhouse.io/anduril/jobs/5148079007")
    before = copy.deepcopy(worksheet.values)
    updated = job_sheet.update_apply_urls([moved], rows)
    after = worksheet.values

    assert moved.key == listed.key
    assert updated == 1
    assert worksheet.call_names().count("batch_update") == 1
    target_row = rows[moved.key] - 1
    changed_cells = [
        (row_index, column_index)
        for row_index, row in enumerate(after)
        for column_index, value in enumerate(row)
        if before[row_index][column_index] != value
    ]
    assert changed_cells == [(target_row, sheet.APPLY_URL_INDEX)]
    assert after[target_row][sheet.APPLY_URL_INDEX] == moved.apply_url
    assert after[target_row][SHEET_HEADERS.index("Status")] == "Applied 2026-07-20"
    expected_row = list(before[target_row])
    expected_row[sheet.APPLY_URL_INDEX] = moved.apply_url
    assert after[target_row] == expected_row
    assert len(after[target_row]) == len(SHEET_HEADERS)


def test_update_apply_urls_empty_makes_no_api_calls():
    worksheet = FakeWorksheet(values=[list(SHEET_HEADERS)])

    assert sheet.JobSheet(worksheet).update_apply_urls([], {}) == 0
    assert worksheet.calls == []
