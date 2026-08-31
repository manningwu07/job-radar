# job-radar

job-radar scans a broad Summer 2027 internship pool three times a day, filters for relevant software engineering roles, and appends unseen applications to Google Sheets. It does not maintain or crawl a company allowlist.

The pooled feed contains applications across Greenhouse, Lever, Ashby, SmartRecruiters, Workday, and company career sites. job-radar identifies the application platform from each URL and writes it to the `Platform` column.

## Pipeline

```text
pooled feed -> normalize -> filter and rank -> dedupe state -> Google Sheet -> email digest
```

The cron runs at 09:00, 13:00, and 17:00 Pacific during daylight saving time. GitHub Actions uses UTC, so the runs occur one hour earlier locally during standard time.

One failed pool fetch fails the run before touching the sheet or state. Sheet writes happen before `data/state.json` changes, so a failed sheet write gets retried on the next run.

## Local use

Requires `uv` and Task.

```bash
task install
task test
task dry-run
```

`task dry-run` fetches the live pool and runs the full filter without credentials. It does not write the sheet, send email, or change state.

`task run` performs the same path used by the scheduled GitHub Action. It requires the Google environment variables below.

## Google Sheets setup

1. Create a Google Cloud project.
2. Enable the Google Sheets API.
3. Create a service account and download its JSON key.
4. Create a spreadsheet.
5. Share the spreadsheet with the service account's `client_email` as an editor.
6. Copy the spreadsheet ID from the URL segment between `/d/` and `/edit`.

The pipeline owns a worksheet named `Job Radar`. It creates that tab if needed and leaves other tabs untouched. It writes the header only when the owned tab is empty. A nonempty mismatched header fails loudly instead of overwriting data.

Set these GitHub Actions secrets:

| Secret | Required | Value |
| --- | --- | --- |
| `JOB_RADAR_SHEET_ID` | Yes | Spreadsheet ID only |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | Complete service account JSON |
| `SMTP_USER` | No | Gmail or Google Workspace sender |
| `SMTP_PASSWORD` | No | Gmail app password |
| `NOTIFY_EMAIL` | No | Digest recipient |

Missing email settings skip email but do not block the scan or sheet update.

## Sheet columns

| Column | Value |
| --- | --- |
| Company | Company name from the pooled listing, with legal suffixes removed |
| Role | Posting title |
| Location | Up to three merged locations |
| Remote? | `Yes` when the listing indicates remote work |
| Posting Date | Published date when present |
| Date Found | First scan date in Pacific time |
| Source | `pool` |
| Application URL | Preferred direct application link |
| Careers URL | Company careers page when supplied |
| Platform | Greenhouse, Lever, Ashby, SmartRecruiters, Workday, or Other |
| Status | Starts as `Not Applied`; later edits are preserved |

The sheet is append-only except for stale application URLs. The pipeline never rewrites `Status`.

## Selection rules

A listing already comes from the curated internship and student-opportunity pool. Its
title does not need to contain `intern`, `internship`, or `co-op`. It must:

- match software engineering, machine learning, infrastructure, distributed systems, platform, robotics software, or research engineering;
- target undergraduate candidates or leave degree level unstated;
- include a US location, remote eligibility, or an unqualified location;
- target Spring or Summer 2027, or leave the term unstated;
- be no more than 180 days old when a posting date exists.

Product management, analytics, IT support, QA, quantitative research, senior roles, graduate-only roles, and narrow frontend, mobile, or security roles are excluded.

The ranking is company-neutral. It orders by role category, posting recency, geographic preference, company name, and role name.

## Dedupe and state

The identity key is normalized company plus normalized role. Location and URL do not affect identity, which merges multi-city copies and lets a moved application update its existing row.

`data/state.json` records seen keys. The scheduled workflow commits that file after a successful sheet update. To report a posting again, remove its entry from state and remove its old sheet row if you do not want a duplicate.

## Commands

```bash
task test
task dry-run
task run
```

`task test` is offline. `task dry-run` reads the live pool but writes nothing. `task run` writes Google Sheets, optionally sends email, and saves state.
