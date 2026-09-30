"""Read employer postings and verify minimum base pay for smaller companies."""

import logging
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from html import unescape
from html.parser import HTMLParser
from urllib.parse import ParseResult, parse_qs, urlparse

import requests

from radar.models import Job
from radar.triage import is_prominent, is_role_relevant

logger: logging.Logger = logging.getLogger(__name__)
REQUEST_TIMEOUT: int = 15
MAX_WORKERS: int = 12
ANNUAL_HOURS: int = 2080
MONEY: str = r"\$\s*(?P<low>[\d,]+(?:\.\d+)?)\s*(?:(?:-|–|—|to)\s*\$?\s*(?P<high>[\d,]+(?:\.\d+)?))?"
MONEY_PATTERN: re.Pattern[str] = re.compile(MONEY, re.IGNORECASE)
UNIT_PATTERN: re.Pattern[str] = re.compile(
    r"(?<!\w)(hourly|per hour|an hour|/\s*hour|/\s*hr|per year|annually|annual|per annum|per month|monthly|per week|weekly)\b",
    re.IGNORECASE,
)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.ignored: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored += 1
        elif tag in {"p", "div", "li", "br", "section", "h1", "h2", "h3"}:
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        elif tag in {"p", "div", "li", "section", "h1", "h2", "h3"}:
            self.chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.chunks.append(data)


def plain_text(html: str) -> str:
    """Return visible text from HTML, excluding scripts and styles."""
    parser: _Text = _Text()
    parser.feed(unescape(html))
    return re.sub(r"\s+", " ", "".join(parser.chunks)).strip()


def minimum_hourly_pay(text: str) -> float | None:
    """Return conservative USD base-pay minimum; annual amounts use 2080 hours."""
    rates: list[float] = []
    match: re.Match[str]
    for match in MONEY_PATTERN.finditer(text):
        prefix: str = text[max(0, match.start() - 120):match.start()]
        suffix: str = text[match.end():match.end() + 70]
        if re.search(r"(?:stipend|bonus|allowance|fertility|insurance|HRA|HSA|FSA).{0,50}$", prefix, re.IGNORECASE):
            continue
        following: str = re.sub(r"^\s*(?:min|max|minimum|maximum)\s+", "", suffix, flags=re.IGNORECASE).lstrip()
        unit: re.Match[str] | None = UNIT_PATTERN.match(following)
        if unit is None:
            if not re.search(r"\b(?:salary|compensation|pay|hourly|rate)\b", prefix, re.IGNORECASE):
                continue
            prior_units: list[re.Match[str]] = list(UNIT_PATTERN.finditer(prefix))
            unit = prior_units[-1] if prior_units else None
            if unit is not None and re.search(r"[.$]|\n\s*\n", prefix[unit.end():]):
                unit = None
        if unit is None:
            continue
        amount: float = float(match.group("low").replace(",", ""))
        if match.group("high") is not None and float(match.group("high").replace(",", "")) < amount:
            raise ValueError("Employer pay range has its maximum below its minimum")
        if re.search(r"(?:CAD|AUD|SGD)\s*$", prefix, re.IGNORECASE):
            continue
        label: str = unit.group(0).casefold()
        if "hour" in label or "hr" in label:
            rate: float = amount
        elif "month" in label:
            rate = amount * 12 / ANNUAL_HOURS
        elif "week" in label:
            rate = amount / 40
        else:
            rate = amount / ANNUAL_HOURS
        if 0 < rate <= 500:
            rates.append(rate)
    return min(rates) if rates else None


@dataclass(frozen=True, slots=True)
class PostingEvidence:
    title: str
    text: str
    url: str
    hourly_pay_min: float | None
    unavailable_reason: str = ""


def inspect_posting(job: Job) -> PostingEvidence:
    """Read one employer posting; return pay evidence or an explicit access failure."""
    parsed: ParseResult = urlparse(job.apply_url)
    host: str = parsed.hostname or ""
    parts: list[str] = [part for part in parsed.path.split("/") if part]
    url: str = job.apply_url
    kind: str = "html"
    if host.endswith(".myworkdayjobs.com") and "job" in parts:
        job_index: int = parts.index("job")
        site: str = parts[job_index - 1]
        tenant: str = host.split(".")[0]
        url = f"https://{host}/wday/cxs/{tenant}/{site}/" + "/".join(parts[job_index:])
        kind = "workday"
    elif host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        query: dict[str, list[str]] = parse_qs(parsed.query)
        if "jobs" in parts:
            url = f"https://boards-api.greenhouse.io/v1/boards/{parts[0]}/jobs/{parts[parts.index('jobs') + 1]}"
            kind = "greenhouse"
        elif "for" in query and "token" in query:
            url = f"https://boards-api.greenhouse.io/v1/boards/{query['for'][0]}/jobs/{query['token'][0]}"
            kind = "greenhouse"
    elif host == "jobs.lever.co" and len(parts) >= 2:
        url = f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}"
        kind = "lever"
    elif host == "jobs.ashbyhq.com" and len(parts) >= 2:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{parts[0]}"
        kind = "ashby"
    try:
        response: requests.Response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        title: str = job.role
        if kind == "html":
            text: str = plain_text(response.text)
            title_match: re.Match[str] | None = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.DOTALL | re.IGNORECASE)
            if title_match:
                title = plain_text(title_match.group(1))
        else:
            raw_payload: object = response.json()
            if not isinstance(raw_payload, dict):
                raise ValueError("Employer API response must be a JSON object")
            payload: dict[str, object] = raw_payload
            if kind == "workday":
                raw_posting: object = payload["jobPostingInfo"]
                if not isinstance(raw_posting, dict):
                    raise ValueError("Workday response has no posting object")
                posting: dict[str, object] = raw_posting
                title = str(posting["title"])
                text = plain_text(str(posting["jobDescription"]))
            elif kind == "greenhouse":
                title = str(payload["title"])
                text = plain_text(str(payload["content"]))
            elif kind == "ashby":
                raw_jobs: object = payload["jobs"]
                if not isinstance(raw_jobs, list):
                    raise ValueError("Ashby response has no jobs list")
                matching: list[dict[str, object]] = [
                    item for item in raw_jobs
                    if isinstance(item, dict) and parts[1] in str(item.get("jobUrl", ""))
                ]
                if len(matching) != 1:
                    raise ValueError("Ashby posting is no longer in the employer's live feed")
                title = str(matching[0]["title"])
                text = str(matching[0]["descriptionPlain"])
            else:
                title = str(payload["text"])
                text = plain_text(str(payload["description"])) + " " + plain_text(str(payload.get("additional", "")))
                raw_lists: object = payload.get("lists", [])
                if not isinstance(raw_lists, list):
                    raise ValueError("Lever response has an invalid qualifications list")
                section: object
                for section in raw_lists:
                    if not isinstance(section, dict):
                        raise ValueError("Lever qualification section must be an object")
                    text += " " + plain_text(str(section["text"]) + " " + str(section["content"]))
                salary: object = payload.get("salaryRange")
                if isinstance(salary, dict) and salary.get("currency") == "USD":
                    text += f" Base pay ${salary['min']} to ${salary['max']} per {salary['interval']}."
        return PostingEvidence(title, text, url, minimum_hourly_pay(text))
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        return PostingEvidence(job.role, "", url, None, str(error))


def enrich_pay(jobs: Sequence[Job]) -> list[Job]:
    """Verify pay for eligible smaller-company jobs; log excluded unknown-pay results."""
    candidates: dict[str, Job] = {
        job.apply_url: job for job in jobs
        if is_role_relevant(job) and not is_prominent(job.company)
    }
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        evidence: dict[str, PostingEvidence] = dict(zip(candidates, pool.map(inspect_posting, candidates.values()), strict=True))
    unknown_count: int = sum(item.hourly_pay_min is None for item in evidence.values())
    failed_count: int = sum(bool(item.unavailable_reason) for item in evidence.values())
    logger.info("Checked pay for %d postings: %d unknown, %d employer read failures; unknown pay is excluded", len(candidates), unknown_count, failed_count)
    return [replace(job, hourly_pay_min=evidence[job.apply_url].hourly_pay_min) if job.apply_url in evidence else job for job in jobs]
