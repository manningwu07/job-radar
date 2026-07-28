"""Offline tests for the relevance filter, dedupe and ranking."""

from datetime import date, timedelta

import pytest

from radar.models import Job, Platform, Season, Source, join_locations
from radar.triage import (
    MAX_POSTING_AGE_DAYS,
    Country,
    GeoTier,
    RoleCategory,
    categorize,
    classify_location,
    is_relevant,
    merge_duplicates,
    sort_key,
    split_locations,
    target_summer_year,
    triage,
)

TODAY = date(2026, 7, 27)
TARGET_YEAR = target_summer_year(TODAY)
PAST_YEAR = TARGET_YEAR - 1
NEWEST_ALLOWED = TODAY - timedelta(days=MAX_POSTING_AGE_DAYS)
STALE = NEWEST_ALLOWED - timedelta(days=1)
FRESH = TODAY - timedelta(days=1)


def make_job(
    role: str,
    company: str = "Example Corp",
    location: str = "Austin, TX",
    remote: bool = False,
    posted_at: date | None = None,
    apply_url: str = "https://example.com/apply/1",
    found_at: date = TODAY,
    source: Source = Source.GREENHOUSE,
    platform: Platform = Platform.GREENHOUSE,
    season: Season | None = None,
) -> Job:
    return Job(
        company=company,
        role=role,
        location=location,
        remote=remote,
        posted_at=posted_at,
        found_at=found_at,
        source=source,
        apply_url=apply_url,
        careers_url="https://example.com/careers",
        platform=platform,
        season=season,
    )


ACCEPTED_ROLES = [
    "Software Engineering Intern",
    "Software Engineer Intern, Backend",
    "SDE Intern - University Program",
    "Machine Learning Systems Intern",
    "ML Infrastructure Intern",
    "Backend Infrastructure Engineering Intern",
    "Distributed Systems Intern",
    "Platform Engineering Intern",
    "AI Infrastructure Intern",
    "GPU Inference Intern",
    "Robotics Software Intern",
    "Autonomy Software Co-op",
    "Research Engineering Intern",
    "Software Engineer Co-op (Undergraduate)",
    "Student Software Developer Apprentice",
]

REJECTED_ROLES = [
    "Product Management Intern",
    "Technical Program Manager Intern",
    "Data Analyst Intern",
    "Business Intelligence Intern",
    "IT Support Intern",
    "QA Intern",
    "SDET Intern",
    "Quantitative Research Intern",
    "Quant Researcher Intern",
    "Trading Intern",
    "Software Engineer, Backend",
    "Marketing Intern",
]


@pytest.mark.parametrize("role", ACCEPTED_ROLES)
def test_accepts_interest_area_internships(role: str) -> None:
    assert is_relevant(make_job(role))


@pytest.mark.parametrize("role", REJECTED_ROLES)
def test_rejects_off_target_roles(role: str) -> None:
    assert not is_relevant(make_job(role))


def test_trading_accepted_when_software_engineering() -> None:
    assert is_relevant(make_job("Software Engineering Intern, Trading Systems"))
    assert not is_relevant(make_job("Trading Operations Intern - Machine Learning"))


def test_frontend_only_rejected_but_frontend_mention_accepted() -> None:
    assert not is_relevant(make_job("Software Engineer Intern, Frontend"))
    assert is_relevant(
        make_job("Software Engineering Intern (React, Node, Go, Kubernetes)")
    )
    assert is_relevant(make_job("Software Engineering Intern - Frontend Platform"))


def test_mobile_only_rejected_but_mobile_mention_accepted() -> None:
    assert not is_relevant(make_job("Software Engineer Intern - iOS"))
    assert not is_relevant(make_job("Mobile Software Engineering Intern"))
    assert is_relevant(make_job("Software Engineering Intern, Mobile Infrastructure"))


@pytest.mark.parametrize(
    "role",
    [
        "Part-Time Student Worker – Software Development Engineer in Test",
        "Software Engineer in Test Intern",
        "Software Engineer, Test - Internship",
        "Software Engineer - Test (Intern)",
    ],
)
def test_rejects_the_spelled_out_sdet_title(role: str) -> None:
    assert not is_relevant(make_job(role, company="Zoox"))


@pytest.mark.parametrize(
    "role",
    [
        "Software Engineer Intern, Test Infrastructure",
        "Developer Productivity Intern - Build and Test Infra",
        "Software Engineering Intern, Test Platform",
        "Software Engineer in Test Infrastructure Intern",
    ],
)
def test_accepts_test_infrastructure_roles(role: str) -> None:
    """Testing is the whole job only when `test` sits against `engineer` with nothing
    broader named. Building the systems others test on is work the user asked for."""
    assert is_relevant(make_job(role))


def test_security_only_rejected_but_security_infra_accepted() -> None:
    assert not is_relevant(make_job("Security Engineering Intern"))
    assert is_relevant(make_job("Software Engineering Intern, Security Infrastructure"))


@pytest.mark.parametrize(
    "role",
    [
        "New Grad Software Engineer (formerly Intern Program)",
        "Software Engineer, New Graduate - Intern conversions welcome",
        "Senior Software Engineer (Intern mentor)",
        "Staff Software Engineer, Infrastructure - Intern mentor",
        "Experienced Software Engineer Intern Program Lead",
    ],
)
def test_rejects_non_internship_levels_even_when_intern_appears(role: str) -> None:
    assert not is_relevant(make_job(role))


US_LOCATIONS = [
    "Mountain View, CA",
    "Foster City, CA",
    "San Francisco, California",
    "Atlanta, Georgia, United States",
    "United States",
    "US",
    "US / Canada",
    "Washington, D.C.",
    "Honolulu, HI",
    "Indianapolis, Indiana",
    "Albuquerque, New Mexico",
    "Vancouver, WA",
    "Melbourne, FL",
    "Brisbane, CA",
    "Cambridge, MA",
    "Dublin, CA",
]
FOREIGN_LOCATIONS = [
    "Bengaluru",
    "Bengaluru, India",
    "Bengaluru, IN",
    "Toronto, Canada",
    "Toronto, ON, Canada",
    "Toronto, CA",
    "Vancouver, BC",
    "Paris, France",
    "Sydney, Australia",
    "Belgrade, Serbia",
    "London",
    "London, United Kingdom",
    "Melbourne, Australia",
    "Cambridge, United Kingdom",
]
UNRECOGNIZED_LOCATIONS = [
    "Remote",
    "Remote or hybrid",
    "Unspecified",
    "Springfield",
    "",
]


@pytest.mark.parametrize("location", US_LOCATIONS)
def test_classifies_us_locations(location: str) -> None:
    assert classify_location(location) is Country.UNITED_STATES


@pytest.mark.parametrize("location", FOREIGN_LOCATIONS)
def test_classifies_foreign_locations(location: str) -> None:
    """`Bengaluru, IN` and `Toronto, CA` are the abbreviation trap: a spelled-out foreign
    place outranks a two-letter code that a US state and another country share."""
    assert classify_location(location) is Country.FOREIGN


@pytest.mark.parametrize("location", UNRECOGNIZED_LOCATIONS)
def test_unrecognized_locations_are_not_classified_either_way(location: str) -> None:
    """`or` and `in` are ordinary English words as well as state codes, so a code only
    counts as a whole comma-separated field."""
    assert classify_location(location) is Country.UNKNOWN


@pytest.mark.parametrize("location", US_LOCATIONS + UNRECOGNIZED_LOCATIONS)
def test_accepts_us_and_unstated_locations(location: str) -> None:
    assert is_relevant(make_job("Software Engineering Intern", location=location))


@pytest.mark.parametrize("location", FOREIGN_LOCATIONS)
def test_rejects_a_posting_with_no_us_location(location: str) -> None:
    assert not is_relevant(make_job("Software Engineering Intern", location=location))


def test_a_us_namesake_of_a_foreign_city_is_kept_when_its_state_is_given() -> None:
    """Northrop Grumman hires in Melbourne, FL and Brisbane, CA is in the Bay Area, so a
    city name alone never overrules the state beside it."""
    for location in ("Melbourne, FL", "Brisbane, CA"):
        assert is_relevant(make_job("Software Engineer Intern", location=location))
    assert not is_relevant(make_job("Software Engineer Intern", location="Melbourne, Australia"))


def test_rejects_a_london_only_posting() -> None:
    perplexity = make_job(
        "Internship - Search Machine Learning Engineer",
        company="Perplexity",
        location="London",
    )
    assert not is_relevant(perplexity)


def test_rejects_the_bengaluru_requisition() -> None:
    stripe = make_job("Software Engineer, Intern", company="Stripe", location="Bengaluru")
    assert not is_relevant(stripe)


def test_rejects_a_foreign_country_named_only_in_the_role_title() -> None:
    """Palantir advertises its Poland and France internships with a US board location."""
    for country in ("Poland", "France"):
        role = f"Forward Deployed Software Engineer, Internship - {country}"
        assert not is_relevant(make_job(role, company="Palantir", location="New York, NY"))


def test_keeps_the_us_sibling_of_a_country_specific_requisition() -> None:
    us_government = make_job(
        "Forward Deployed Software Engineer, Internship - US Government",
        company="Palantir",
        location="Honolulu, HI",
    )
    assert is_relevant(us_government)


def test_keeps_a_posting_that_lists_both_us_and_foreign_locations() -> None:
    mixed = make_job(
        "Software Engineer, Internship",
        company="Palantir",
        location="Palo Alto, CA; Warsaw, Poland",
    )
    assert is_relevant(mixed)
    assert merge_duplicates([mixed]).location == "Palo Alto, CA"


def test_merge_strips_foreign_cities_from_the_location_cell() -> None:
    palo_alto = make_job("Software Engineer, Internship", location="Palo Alto, CA")
    warsaw = make_job("Software Engineer, Internship", location="Warsaw, Poland")
    merged = merge_duplicates([palo_alto, warsaw])
    assert split_locations(merged.location) == ["Palo Alto, CA"]


GRADUATE_ONLY_ROLES = [
    "PhD GenAI Research Scientist Intern",
    "Software Engineering Intern, Masters",
    "Machine Learning Intern, PhD",
    "Ph.D. Software Engineering Intern",
    "Software Engineering Intern, MS/PhD",
    "Doctoral Research Engineering Intern",
    "Software Engineering Intern for Graduate Students",
    "Postgraduate Machine Learning Internship",
]
UNDERGRADUATE_ELIGIBLE_ROLES = [
    "Software Engineer Intern, Undergrad",
    "Software Engineering Intern",
    "Software Engineering Intern (BS/MS)",
    "Machine Learning Intern - open to Bachelors, Masters and PhD students",
    "Software Engineering Intern, Undergraduate and Masters",
]


@pytest.mark.parametrize("role", GRADUATE_ONLY_ROLES)
def test_rejects_graduate_exclusive_internships(role: str) -> None:
    assert not is_relevant(make_job(role))


@pytest.mark.parametrize("role", UNDERGRADUATE_ELIGIBLE_ROLES)
def test_accepts_undergraduate_eligible_internships(role: str) -> None:
    """A title naming a graduate degree alongside an undergraduate one is open to both,
    and a title naming no degree at all is the unstated majority."""
    assert is_relevant(make_job(role))


def test_accepts_target_season_and_unstated_season() -> None:
    assert is_relevant(make_job(f"Software Engineering Intern, Summer {TARGET_YEAR}"))
    assert is_relevant(make_job(f"{TARGET_YEAR} Summer Software Engineering Intern"))
    assert is_relevant(make_job("Software Engineering Intern"))


@pytest.mark.parametrize(
    "role_template",
    [
        "Software Engineering Intern, Summer {year}",
        "Software Engineering Intern, Fall {year}",
        "Software Engineering Intern, Winter {year}",
        "Software Engineering Intern {year}",
    ],
)
def test_rejects_past_seasons(role_template: str) -> None:
    assert not is_relevant(make_job(role_template.format(year=PAST_YEAR)))


def test_rejects_spring_of_target_year() -> None:
    assert not is_relevant(make_job(f"Software Engineering Intern, Spring {TARGET_YEAR}"))


@pytest.mark.parametrize("separator", [" ", ", ", "-", "_", " '"])
def test_reads_a_dated_term_through_any_separator(separator: str) -> None:
    short_year = str(PAST_YEAR)[-2:] if separator == " '" else str(PAST_YEAR)
    role = f"Machine Learning Intern/Co-op (Fall{separator}{short_year})"
    assert not is_relevant(make_job(role))


def test_rejects_past_season_stated_only_in_the_url_slug() -> None:
    fall_req = make_job(
        "Software Engineer Intern, Service Engineering",
        company="Tesla",
        apply_url=(
            "https://www.tesla.com/careers/search/job/"
            f"internship-software-engineer-service-engineering-fall-{PAST_YEAR}-266119"
        ),
    )
    assert not is_relevant(fall_req)


def test_accepts_target_season_stated_only_in_the_url_slug() -> None:
    summer_req = make_job(
        "Software Engineering Intern",
        company="Google",
        apply_url=(
            "https://www.google.com/about/careers/applications/jobs/results/"
            f"85564713261245126-software-engineering-intern-bs-summer-{TARGET_YEAR}"
        ),
    )
    assert is_relevant(summer_req)


def test_bare_numbers_in_a_url_do_not_count_as_a_season() -> None:
    numeric_req = make_job(
        "Software Engineering Intern",
        apply_url="https://boards.example.com/careers/job/2026/apply?req=2025",
    )
    assert is_relevant(numeric_req)


def test_rejects_posting_that_also_states_a_past_term() -> None:
    role = f"Software Engineering Intern - Summer {PAST_YEAR} / Summer {TARGET_YEAR}"
    assert not is_relevant(make_job(role))


def test_accepts_academic_year_range_ending_at_the_target_year() -> None:
    assert is_relevant(make_job(f"{PAST_YEAR}-{TARGET_YEAR} Software Engineering Internship"))


@pytest.mark.parametrize("season", [Season.SPRING, Season.FALL, Season.WINTER])
def test_rejects_non_summer_season_field(season: Season) -> None:
    fall_req = make_job(
        "Software Engineer Intern, Service Engineering",
        company="Tesla",
        season=season,
    )
    assert not is_relevant(fall_req)


def test_accepts_summer_season_field() -> None:
    assert is_relevant(make_job("Software Engineering Intern", season=Season.SUMMER))


def test_accepts_unstated_season_field() -> None:
    assert is_relevant(make_job("Software Engineering Intern", season=None))


def test_season_field_rejects_even_when_title_and_url_say_target_summer() -> None:
    contradictory = make_job(
        f"Software Engineering Intern, Summer {TARGET_YEAR}",
        season=Season.FALL,
        apply_url=f"https://boards.example.com/job/swe-intern-summer-{TARGET_YEAR}",
    )
    assert not is_relevant(contradictory)


def test_url_slug_rejects_even_when_the_season_field_says_summer() -> None:
    contradictory = make_job(
        "Software Engineering Intern",
        season=Season.SUMMER,
        apply_url=f"https://boards.example.com/job/swe-intern-fall-{PAST_YEAR}-266119",
    )
    assert not is_relevant(contradictory)


def test_target_summer_year_rolls_over_in_june() -> None:
    assert target_summer_year(date(2026, 5, 31)) == 2026
    assert target_summer_year(date(2026, 6, 1)) == 2027
    assert target_summer_year(date(2026, 7, 27)) == 2027
    assert target_summer_year(date(2027, 1, 15)) == 2027


def test_categorize_prefers_software_engineering_then_machine_learning() -> None:
    assert (
        categorize("Software Engineer Intern, Machine Learning")
        is RoleCategory.SOFTWARE_ENGINEERING
    )
    assert categorize("Machine Learning Intern") is RoleCategory.MACHINE_LEARNING
    assert categorize("Puppy Petting Intern") is RoleCategory.OTHER


def test_accepts_undated_posting() -> None:
    assert is_relevant(make_job("Software Engineering Intern", posted_at=None))


def test_accepts_posting_exactly_at_the_age_limit() -> None:
    assert is_relevant(make_job("Software Engineering Intern", posted_at=NEWEST_ALLOWED))


def test_rejects_posting_past_the_age_limit() -> None:
    assert not is_relevant(make_job("Software Engineering Intern", posted_at=STALE))


def test_rejects_year_old_posting_from_a_priority_company() -> None:
    stale_tesla = make_job(
        "Software Engineer Intern, Update Systems",
        company="Tesla",
        location="Palo Alto, CA",
        posted_at=TODAY - timedelta(days=346),
    )
    assert not is_relevant(stale_tesla)


def test_age_limit_uses_found_at_not_the_wall_clock() -> None:
    later_run = TODAY + timedelta(days=365)
    revisited = make_job(
        "Software Engineering Intern", posted_at=NEWEST_ALLOWED, found_at=later_run
    )
    assert not is_relevant(revisited)
    still_fresh = make_job(
        "Software Engineering Intern",
        posted_at=later_run - timedelta(days=1),
        found_at=later_run,
    )
    assert is_relevant(still_fresh)


def palantir_city_reqs() -> list[Job]:
    """The live shape: one Lever requisition per city, same role, four different URLs."""
    cities = [
        ("Palo Alto, CA", "e27af7ab", date(2026, 6, 24)),
        ("New York, NY", "7d69cf8a", date(2026, 7, 1)),
        ("Washington, D.C.", "bdcfb29f", None),
        ("Denver, CO", "373eb939", date(2026, 6, 30)),
    ]
    return [
        make_job(
            "Software Engineer, Internship",
            company="Palantir",
            location=city,
            posted_at=posted_at,
            apply_url=f"https://jobs.lever.co/palantir/{token}/apply",
        )
        for city, token, posted_at in cities
    ]


def test_merge_collapses_one_requisition_per_city_into_a_single_row() -> None:
    merged = merge_duplicates(palantir_city_reqs())
    assert split_locations(merged.location) == [
        "Palo Alto, CA",
        "New York, NY",
        "Denver, CO",
    ]
    assert merged.location.endswith("+1 more")
    assert merged.posted_at == date(2026, 7, 1)


def test_merge_orders_cities_by_geographic_preference() -> None:
    scattered = [
        make_job("Software Engineering Intern", location="Austin, TX"),
        make_job("Software Engineering Intern", location="Boston, MA"),
        make_job("Software Engineering Intern", location="Sunnyvale, CA"),
        make_job("Software Engineering Intern", location="Seattle, WA"),
    ]
    merged = merge_duplicates(scattered)
    assert split_locations(merged.location) == [
        "Sunnyvale, CA",
        "Seattle, WA",
        "Boston, MA",
    ]
    assert merge_duplicates(list(reversed(scattered))).location == merged.location


def test_triage_merges_the_city_requisitions_into_one_ranked_row() -> None:
    result = triage(palantir_city_reqs())
    assert len(result) == 1
    assert sort_key(result[0]).geo is GeoTier.BAY_AREA


def test_merge_does_not_nest_truncation_markers() -> None:
    first = make_job(
        "Software Engineering Intern",
        location=join_locations(["Seattle, WA", "Austin, TX", "Chicago, IL", "Reno, NV"]),
    )
    second = make_job(
        "Software Engineering Intern",
        location=join_locations(["Boston, MA", "Denver, CO", "Miami, FL", "Provo, UT"]),
    )
    merged = merge_duplicates([first, second])
    assert merged.location.count("more") == 1
    assert merged.location == "Seattle, WA; Boston, MA; Austin, TX; +3 more"
    assert split_locations(merged.location) == ["Seattle, WA", "Boston, MA", "Austin, TX"]


def test_merge_unions_locations_case_insensitively_keeping_the_first_spelling() -> None:
    merged = merge_duplicates(
        [
            make_job("Software Engineering Intern", location="Mountain View, CA"),
            make_job("Software Engineering Intern", location="mountain view, ca"),
        ]
    )
    assert merged.location == "Mountain View, CA"


def test_merge_takes_the_most_recent_date_and_keeps_none_only_when_all_are_undated() -> None:
    older = make_job("Software Engineering Intern", posted_at=FRESH - timedelta(days=30))
    newer = make_job("Software Engineering Intern", posted_at=FRESH)
    undated = make_job("Software Engineering Intern", posted_at=None)
    assert merge_duplicates([older, newer, undated]).posted_at == FRESH
    assert merge_duplicates([undated, newer, older]).posted_at == FRESH
    assert merge_duplicates([undated, undated]).posted_at is None


def test_merge_is_remote_when_any_member_is_remote() -> None:
    onsite = make_job("Software Engineering Intern", location="Austin, TX")
    remote = make_job("Software Engineering Intern", location="Remote", remote=True)
    assert merge_duplicates([onsite, remote]).remote
    assert not merge_duplicates([onsite, onsite]).remote


def test_merge_prefers_the_first_party_ats_url_over_simplify() -> None:
    simplify = make_job(
        "Software Engineering Intern",
        source=Source.SIMPLIFY,
        platform=Platform.OTHER,
        apply_url="https://simplify.jobs/p/abc/software-engineering-intern",
    )
    ats = make_job(
        "Software Engineering Intern",
        source=Source.GREENHOUSE,
        platform=Platform.GREENHOUSE,
        apply_url="https://boards.greenhouse.io/anduril/jobs/5148079007",
    )
    for group in ([simplify, ats], [ats, simplify]):
        merged = merge_duplicates(group)
        assert merged.apply_url == ats.apply_url
        assert merged.source is Source.GREENHOUSE
        assert merged.platform is Platform.GREENHOUSE


def test_merge_prefers_the_url_without_query_parameters() -> None:
    embedded = make_job(
        "Software Engineering Intern",
        apply_url="https://jobs.ashbyhq.com/netic/b0ea7aab?embed=true",
    )
    clean = make_job(
        "Software Engineering Intern",
        apply_url="https://jobs.ashbyhq.com/netic/b0ea7aab",
    )
    assert merge_duplicates([embedded, clean]).apply_url == clean.apply_url
    assert merge_duplicates([clean, embedded]).apply_url == clean.apply_url


def test_merge_prefers_the_shorter_host_form_for_the_same_requisition() -> None:
    long_host = make_job(
        "Software Engineering Intern",
        apply_url="https://job-boards.greenhouse.io/anduril/jobs/5148079007",
    )
    short_host = make_job(
        "Software Engineering Intern",
        apply_url="https://boards.greenhouse.io/anduril/jobs/5148079007",
    )
    assert merge_duplicates([long_host, short_host]).apply_url == short_host.apply_url
    assert merge_duplicates([short_host, long_host]).apply_url == short_host.apply_url


def test_merge_keeps_a_stated_season_over_an_unstated_one() -> None:
    unstated = make_job("Software Engineering Intern", season=None)
    stated = make_job("Software Engineering Intern", season=Season.SUMMER)
    assert merge_duplicates([unstated, stated]).season is Season.SUMMER
    assert merge_duplicates([stated, unstated]).season is Season.SUMMER
    assert merge_duplicates([unstated, unstated]).season is None


def test_merge_takes_every_url_bound_field_from_one_record() -> None:
    """A row must not advertise one board's `source` next to another board's link."""
    lever = Job(
        company="Palantir",
        role="Software Engineer Intern",
        location="New York, NY",
        remote=False,
        posted_at=FRESH,
        found_at=TODAY,
        source=Source.LEVER,
        apply_url="https://jobs.lever.co/palantir/7d69cf8a",
        careers_url="https://jobs.lever.co/palantir",
        platform=Platform.LEVER,
    )
    aggregated = Job(
        company="Palantir",
        role="Software Engineer Intern",
        location="Palo Alto, CA",
        remote=True,
        posted_at=FRESH,
        found_at=TODAY,
        source=Source.SIMPLIFY,
        apply_url="https://simplify.jobs/p/xyz",
        careers_url="https://www.palantir.com/careers",
        platform=Platform.OTHER,
    )
    merged = merge_duplicates([aggregated, lever])
    assert (merged.source, merged.platform, merged.careers_url) == (
        lever.source,
        lever.platform,
        lever.careers_url,
    )
    assert merged.apply_url == lever.apply_url
    assert split_locations(merged.location) == ["Palo Alto, CA", "New York, NY"]
    assert merged.remote


def test_merge_keeps_distinct_roles_apart() -> None:
    swe = make_job("Software Engineering Intern", company="Stripe", posted_at=FRESH)
    ml = make_job("ML Infrastructure Intern", company="Stripe", posted_at=FRESH)
    assert len(triage([swe, ml])) == 2


def anduril_year_variants() -> tuple[Job, Job]:
    """The live collision: one requisition titled with and without its year."""
    from_board = make_job(
        f"{TARGET_YEAR} Software Engineer Intern",
        company="Anduril",
        location="Costa Mesa, CA",
        posted_at=FRESH,
        apply_url="https://boards.greenhouse.io/andurilindustries/jobs/5148079007",
    )
    from_aggregator = make_job(
        "Software Engineer Intern",
        company="Anduril",
        location="Atlanta, GA",
        posted_at=FRESH - timedelta(days=17),
        source=Source.SIMPLIFY,
        platform=Platform.OTHER,
        apply_url="https://job-boards.greenhouse.io/andurilindustries/jobs/5148079007",
    )
    return from_board, from_aggregator


def test_merge_collapses_year_decorated_title_variants() -> None:
    from_board, from_aggregator = anduril_year_variants()
    assert from_board.key == from_aggregator.key

    rows = triage([from_board, from_aggregator])
    assert len(rows) == 1
    assert rows[0].role == f"{TARGET_YEAR} Software Engineer Intern"
    assert split_locations(rows[0].location) == ["Atlanta, GA", "Costa Mesa, CA"]


def test_merge_result_is_identical_in_either_input_order() -> None:
    from_board, from_aggregator = anduril_year_variants()
    assert merge_duplicates([from_board, from_aggregator]) == merge_duplicates(
        [from_aggregator, from_board]
    )


def test_merged_role_choice_preserves_the_group_key() -> None:
    from_board, from_aggregator = anduril_year_variants()
    merged = merge_duplicates([from_board, from_aggregator])
    assert merged.key == from_board.key


def test_merge_prefers_the_longer_title_when_neither_names_a_term() -> None:
    bare = make_job("Software Engineer Intern", company="Stripe")
    detailed = make_job("Software Engineer Intern, Payments Infrastructure", company="Stripe")
    assert merge_duplicates([bare, detailed]).role == detailed.role
    assert merge_duplicates([detailed, bare]).role == detailed.role


def test_roles_differing_by_more_than_year_decoration_do_not_merge() -> None:
    infra = make_job(
        f"{TARGET_YEAR} Software Engineer Intern, Infrastructure",
        company="Anduril",
        posted_at=FRESH,
    )
    plain = make_job(
        f"{TARGET_YEAR} Software Engineer Intern",
        company="Anduril",
        posted_at=FRESH,
    )
    assert infra.key != plain.key
    assert len(triage([infra, plain])) == 2


def test_season_parsing_reads_the_full_role_not_the_dedupe_identity() -> None:
    fall_req = make_job(
        f"Fall {PAST_YEAR} Software Engineering Internship",
        company="SpaceX",
        posted_at=FRESH,
    )
    bare_req = make_job("Software Engineering Internship", company="SpaceX", posted_at=FRESH)
    assert fall_req.key == bare_req.key
    assert not is_relevant(fall_req)
    assert is_relevant(bare_req)
    assert triage([fall_req, bare_req]) == [bare_req]


def test_triage_drops_irrelevant_and_sorts() -> None:
    swe = make_job(
        "Software Engineering Intern",
        company="OpenAI",
        location="San Francisco, CA",
        posted_at=date(2026, 7, 20),
    )
    research = make_job(
        "Research Engineering Intern",
        company="Tiny Lab",
        location="Ames, IA",
        posted_at=date(2026, 7, 26),
    )
    noise = make_job("Product Management Intern", company="OpenAI")
    assert triage([research, noise, swe]) == [swe, research]


def test_sort_key_orders_recent_postings_before_undated() -> None:
    recent = make_job("Software Engineering Intern", posted_at=date(2026, 7, 20))
    older = make_job("Software Engineering Intern", posted_at=date(2026, 1, 5))
    undated = make_job("Software Engineering Intern")
    assert sorted([undated, older, recent], key=sort_key) == [recent, older, undated]


def test_sort_key_orders_geography_by_preference() -> None:
    jobs = [
        make_job("Software Engineering Intern", location="Anywhere", remote=True),
        make_job("Software Engineering Intern", location="Boston, MA"),
        make_job("Software Engineering Intern", location="New York, NY"),
        make_job("Software Engineering Intern", location="Seattle, WA"),
        make_job("Software Engineering Intern", location="Mountain View, CA"),
        make_job("Software Engineering Intern", location="Ames, IA"),
    ]
    ranked = sorted(jobs, key=sort_key)
    assert [job.location for job in ranked] == [
        "Mountain View, CA",
        "Seattle, WA",
        "New York, NY",
        "Boston, MA",
        "Anywhere",
        "Ames, IA",
    ]
    assert sort_key(ranked[0]).geo is GeoTier.BAY_AREA


def test_geo_scores_multi_location_posting_by_its_best_location() -> None:
    joined = make_job(
        "Software Engineering Intern",
        location=join_locations(["Austin, TX", "Mountain View, CA", "Chicago, IL"]),
    )
    seattle_only = make_job("Software Engineering Intern", location="Seattle, WA")
    assert sort_key(joined).geo is GeoTier.BAY_AREA
    assert sort_key(joined) < sort_key(seattle_only)


def test_geo_ignores_the_truncation_marker() -> None:
    truncated = join_locations(
        ["Mountain View, CA", "Bellevue, WA", "Austin, TX", "Chicago, IL"]
    )
    assert truncated.endswith("+1 more")
    truncated_job = make_job("Software Engineering Intern", location=truncated)
    assert sort_key(truncated_job).geo is GeoTier.BAY_AREA
    off_target = join_locations(["Ames, IA", "Omaha, NE", "Boise, ID", "Reno, NV"])
    assert off_target.endswith("+1 more")
    off_target_job = make_job("Software Engineering Intern", location=off_target)
    assert sort_key(off_target_job).geo is GeoTier.OTHER


def test_geo_falls_back_to_remote_only_when_no_city_matches() -> None:
    remote_only = make_job(
        "Software Engineering Intern", location="United States", remote=True
    )
    assert sort_key(remote_only).geo is GeoTier.REMOTE
    remote_with_office = make_job(
        "Software Engineering Intern",
        location=join_locations(["Remote", "Sunnyvale, CA"]),
        remote=True,
    )
    assert sort_key(remote_with_office).geo is GeoTier.BAY_AREA


def test_ranking_is_total_so_output_order_never_depends_on_input_order() -> None:
    masters = make_job(
        "Software Engineering Intern, Masters",
        company="Apple",
        location="United States",
        posted_at=FRESH,
    )
    undergrad = make_job(
        "Software Engineer Intern, Undergrad",
        company="Apple",
        location="United States",
        posted_at=FRESH,
    )
    assert sort_key(masters)[:4] == sort_key(undergrad)[:4]
    assert triage([masters, undergrad]) == triage([undergrad, masters])


def test_tier_one_swe_bay_area_outranks_tier_three_research() -> None:
    tier_one = make_job(
        "Software Engineering Intern",
        company="OpenAI",
        location="San Francisco, CA",
        posted_at=date(2026, 6, 1),
    )
    tier_three = make_job(
        "Research Engineering Intern",
        company="Obscure Research Lab",
        location="Ames, IA",
        posted_at=date(2026, 7, 26),
    )
    assert sort_key(tier_one) < sort_key(tier_three)
