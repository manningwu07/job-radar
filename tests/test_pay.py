"""Check employer pay parsing and the company/pay eligibility boundary."""

from dataclasses import replace
from datetime import date

import pytest

from radar.models import Job, Platform, Source
from radar.pay import minimum_hourly_pay, plain_text
from radar.triage import is_relevant


@pytest.mark.parametrize(("text", "expected"), (
    ("Base pay $30.00 - $45.00 per hour", 30.0),
    ("The hourly rate range for this position is $29.99 - $60.00.", 29.99),
    ("Base pay $30/hr", 30.0),
    ("Base salary $62,400 to $100,000 per year", 30.0),
    ("Base pay $5,200 per month", 30.0),
    ("A $4,800 housing stipend per month", None),
    ("The monthly rate for this internship is $12,500 + a housing stipend. Fertility HRA up to $10,000 lifetime.", 12500 * 12 / 2080),
    ("An annual bonus is paid. Benefits: fertility HRA $10,000.", None),
    ("Salary $50,000 - $90,000, period unspecified", None),
    ("Salary CAD $30 per hour", None),
    ("Base pay $32/hour in NY; $28/hour in Ohio", 28.0),
))
def test_minimum_pay_uses_explicit_period_and_the_low_end(
    text: str, expected: float | None,
) -> None:
    assert minimum_hourly_pay(text) == expected


def test_reversed_employer_pay_range_fails_loudly() -> None:
    with pytest.raises(ValueError, match="maximum below"):
        minimum_hourly_pay("Base pay $45 to $30 per hour")


def test_salary_in_an_unrelated_script_is_not_pay_evidence() -> None:
    text: str = plain_text('<p>Salary not posted.</p><script>{"salary":"$100/hour"}</script>')
    assert minimum_hourly_pay(text) is None


def test_inline_html_does_not_split_salary_digits() -> None:
    text: str = plain_text('<p>$20 min hourly wage - $3<span>5</span> max hourly wage</p>')
    assert minimum_hourly_pay(text) == 20.0


@pytest.mark.parametrize(("company", "hourly_min", "expected"), (
    ("Deloitte", None, True),
    ("Deloitte", 20.0, True),
    ("Zoox", 29.99, False),
    ("Zoox", 30.0, True),
    ("Smaller Employer", None, False),
    ("Smaller Employer", 29.99, False),
    ("Smaller Employer", 30.0, True),
))
def test_company_pay_rule_keeps_prominent_employers_or_verified_30_dollars(
    company: str, hourly_min: float | None, expected: bool,
) -> None:
    job: Job = Job(
        company=company, role="Software Engineer Intern", location="Austin, TX",
        remote=False, posted_at=None, found_at=date(2026, 9, 30), source=Source.POOL,
        apply_url="https://example.com/jobs/1", careers_url="", platform=Platform.OTHER,
        hourly_pay_min=hourly_min,
    )
    assert is_relevant(job) is expected
    assert not is_relevant(replace(job, role="Senior Software Engineer"))
