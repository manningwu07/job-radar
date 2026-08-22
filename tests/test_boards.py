"""Tests for the verified company board registry."""

import pytest

from radar.boards import (
    BOARDS,
    COMPANY_ALIASES,
    TARGET_COMPANIES,
    canonical_company,
    company_priority,
)
from radar.models import Platform

MINIMUM_VERIFIED_BOARDS = 30
EXPECTED_TARGET_BOARD_COUNT = 19
SUFFIX_CASES = (
    ("Palantir Technologies", "Palantir"),
    ("Google LLC", "Google"),
    ("NVIDIA Corporation", "NVIDIA"),
    ("Apple Inc.", "Apple"),
    ("Apex Technology, Inc.", "Apex Technology"),
    ("Example Corp,", "Example"),
    ("Example Ltd", "Example"),
    ("Example Co.", "Example"),
)
TARGET_ALIAS_CASES = (
    ("Alphabet", "Google"),
    ("Google DeepMind", "DeepMind"),
    ("Scale", "Scale AI"),
    ("X.AI", "xAI"),
    ("Amazon.com", "Amazon"),
    ("Amazon Web Services", "Amazon"),
    ("AWS", "Amazon"),
    ("Palantir Technologies", "Palantir"),
)
SLUG_ALIAS_CASES = (
    ("AtomicSemi", "Atomic Semi"),
    ("fab2", "Atomic Semi"),
)


def test_registry_has_verified_board_floor_and_unique_companies() -> None:
    assert len(BOARDS) >= MINIMUM_VERIFIED_BOARDS
    assert len({board.company for board in BOARDS}) == len(BOARDS)
    assert all(board.token for board in BOARDS)
    assert all(board.careers_url.startswith("https://") for board in BOARDS)


def test_registry_covers_every_supported_platform() -> None:
    supported_platforms = {
        Platform.GREENHOUSE,
        Platform.LEVER,
        Platform.ASHBY,
        Platform.SMARTRECRUITERS,
        Platform.WORKDAY,
        Platform.AMD,
    }

    assert {board.platform for board in BOARDS} == supported_platforms


def test_target_boards_are_tier_one() -> None:
    target_boards = [board for board in BOARDS if board.company in TARGET_COMPANIES]

    assert len(target_boards) == EXPECTED_TARGET_BOARD_COUNT
    assert all(board.tier == 1 for board in target_boards)


def test_company_priority_follows_declared_order() -> None:
    priorities = [company_priority(company) for company in TARGET_COMPANIES]

    assert priorities == list(range(len(TARGET_COMPANIES)))
    assert company_priority("openai") == 0
    assert company_priority("Unlisted Company") == len(TARGET_COMPANIES)


def test_every_explicit_alias_has_canonical_display_name() -> None:
    for normalized_alias, canonical_name in COMPANY_ALIASES.items():
        assert canonical_company(normalized_alias) == canonical_name


@pytest.mark.parametrize(("variant", "canonical_name"), SUFFIX_CASES)
def test_canonical_company_strips_corporate_suffixes(
    variant: str, canonical_name: str
) -> None:
    assert canonical_company(variant) == canonical_name


@pytest.mark.parametrize(("alias", "canonical_name"), TARGET_ALIAS_CASES)
def test_target_aliases_keep_target_priority(alias: str, canonical_name: str) -> None:
    assert canonical_company(alias) == canonical_name
    assert company_priority(alias) == TARGET_COMPANIES.index(canonical_name)


@pytest.mark.parametrize(("slug", "canonical_name"), SLUG_ALIAS_CASES)
def test_board_slugs_use_human_company_names(slug: str, canonical_name: str) -> None:
    assert canonical_company(slug) == canonical_name
