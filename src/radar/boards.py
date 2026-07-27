"""Verified ATS board registry and company priority helpers."""

import re
from dataclasses import dataclass

from radar.models import Platform, normalize

TIER_ONE = 1
TIER_TWO = 2
TIER_THREE = 3
CORPORATE_SUFFIX_PATTERN = re.compile(
    r"(?:,\s*|\s+)(?:inc\.?|llc|corporation|corp\.?|technologies|ltd\.?|co\.)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Board:
    """Describe one verified public ATS job board."""

    company: str
    platform: Platform
    token: str
    careers_url: str
    tier: int


TARGET_COMPANIES: tuple[str, ...] = (
    "OpenAI",
    "Anthropic",
    "xAI",
    "NVIDIA",
    "Google",
    "DeepMind",
    "Databricks",
    "Tesla",
    "Apple",
    "Zoox",
    "Waymo",
    "Scale AI",
    "Cloudflare",
    "Stripe",
    "Figma",
    "Vercel",
    "Linear",
    "Anduril",
    "Palantir",
    "Snowflake",
    "Amazon",
    "Roblox",
)

COMPANY_ALIASES: dict[str, str] = {
    **{normalize(company): company for company in TARGET_COMPANIES},
    normalize("Alphabet"): "Google",
    normalize("Google DeepMind"): "DeepMind",
    normalize("Scale"): "Scale AI",
    normalize("X.AI"): "xAI",
    normalize("Amazon.com"): "Amazon",
    normalize("Amazon Web Services"): "Amazon",
    normalize("AWS"): "Amazon",
    normalize("Aquatic Capital"): "Aquatic",
    normalize("AtomicSemi"): "Atomic Semi",
    normalize("fab2"): "Atomic Semi",
    normalize("Jump Trading Group"): "Jump Trading",
    normalize("SPREEAI"): "SpreeAI",
    normalize("Susquehanna Investment Group"): "Susquehanna",
    normalize("Virtu Financial"): "Virtu",
}

BOARDS: tuple[Board, ...] = (
    Board("OpenAI", Platform.ASHBY, "openai", "https://openai.com/careers/", TIER_ONE),
    Board(
        "Anthropic",
        Platform.GREENHOUSE,
        "anthropic",
        "https://www.anthropic.com/careers",
        TIER_ONE,
    ),
    Board("xAI", Platform.GREENHOUSE, "xai", "https://x.ai/careers", TIER_ONE),
    Board(
        "NVIDIA",
        Platform.WORKDAY,
        "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite",
        "https://www.nvidia.com/en-us/about-nvidia/careers/",
        TIER_ONE,
    ),
    Board(
        "DeepMind",
        Platform.GREENHOUSE,
        "deepmind",
        "https://deepmind.google/about/careers/",
        TIER_ONE,
    ),
    Board(
        "Databricks",
        Platform.GREENHOUSE,
        "databricks",
        "https://www.databricks.com/company/careers/open-positions",
        TIER_ONE,
    ),
    Board("Zoox", Platform.LEVER, "zoox", "https://zoox.com/careers/", TIER_ONE),
    Board(
        "Waymo",
        Platform.GREENHOUSE,
        "waymo",
        "https://waymo.com/careers/",
        TIER_ONE,
    ),
    Board(
        "Scale AI",
        Platform.GREENHOUSE,
        "scaleai",
        "https://scale.com/careers",
        TIER_ONE,
    ),
    Board(
        "Cloudflare",
        Platform.GREENHOUSE,
        "cloudflare",
        "https://www.cloudflare.com/careers/jobs/",
        TIER_ONE,
    ),
    Board(
        "Stripe",
        Platform.GREENHOUSE,
        "stripe",
        "https://stripe.com/jobs/search",
        TIER_ONE,
    ),
    Board(
        "Figma",
        Platform.GREENHOUSE,
        "figma",
        "https://www.figma.com/careers/",
        TIER_ONE,
    ),
    Board(
        "Vercel",
        Platform.GREENHOUSE,
        "vercel",
        "https://vercel.com/careers",
        TIER_ONE,
    ),
    Board(
        "Linear",
        Platform.ASHBY,
        "linear",
        "https://linear.app/careers",
        TIER_ONE,
    ),
    Board(
        "Anduril",
        Platform.GREENHOUSE,
        "andurilindustries",
        "https://www.anduril.com/open-roles/",
        TIER_ONE,
    ),
    Board(
        "Palantir",
        Platform.LEVER,
        "palantir",
        "https://www.palantir.com/careers/",
        TIER_ONE,
    ),
    Board(
        "Snowflake",
        Platform.ASHBY,
        "snowflake",
        "https://careers.snowflake.com/",
        TIER_ONE,
    ),
    Board(
        "Roblox",
        Platform.GREENHOUSE,
        "roblox",
        "https://careers.roblox.com/jobs",
        TIER_ONE,
    ),
    Board(
        "SpaceX",
        Platform.GREENHOUSE,
        "spacex",
        "https://www.spacex.com/careers/",
        TIER_TWO,
    ),
    Board(
        "Discord",
        Platform.GREENHOUSE,
        "discord",
        "https://discord.com/careers",
        TIER_TWO,
    ),
    Board(
        "Reddit",
        Platform.GREENHOUSE,
        "reddit",
        "https://www.redditinc.com/careers",
        TIER_TWO,
    ),
    Board(
        "Datadog",
        Platform.GREENHOUSE,
        "datadog",
        "https://careers.datadoghq.com/",
        TIER_TWO,
    ),
    Board(
        "MongoDB",
        Platform.GREENHOUSE,
        "mongodb",
        "https://www.mongodb.com/company/careers",
        TIER_TWO,
    ),
    Board(
        "Cockroach Labs",
        Platform.GREENHOUSE,
        "cockroachlabs",
        "https://www.cockroachlabs.com/careers/",
        TIER_TWO,
    ),
    Board(
        "Cursor",
        Platform.ASHBY,
        "cursor",
        "https://www.cursor.com/careers",
        TIER_TWO,
    ),
    Board(
        "Perplexity",
        Platform.ASHBY,
        "perplexity",
        "https://www.perplexity.ai/hub/careers",
        TIER_TWO,
    ),
    Board(
        "Replit",
        Platform.ASHBY,
        "replit",
        "https://replit.com/careers",
        TIER_TWO,
    ),
    Board(
        "Cohere",
        Platform.ASHBY,
        "cohere",
        "https://cohere.com/careers",
        TIER_TWO,
    ),
    Board(
        "Cerebras",
        Platform.ASHBY,
        "cerebras",
        "https://www.cerebras.ai/careers",
        TIER_TWO,
    ),
    Board(
        "Shield AI",
        Platform.LEVER,
        "shieldai",
        "https://shield.ai/careers/",
        TIER_TWO,
    ),
    Board(
        "Duolingo",
        Platform.GREENHOUSE,
        "duolingo",
        "https://careers.duolingo.com/",
        TIER_THREE,
    ),
    Board(
        "Coinbase",
        Platform.GREENHOUSE,
        "coinbase",
        "https://www.coinbase.com/careers/positions",
        TIER_THREE,
    ),
    Board(
        "Dropbox",
        Platform.GREENHOUSE,
        "dropbox",
        "https://jobs.dropbox.com/",
        TIER_THREE,
    ),
    Board(
        "GitLab",
        Platform.GREENHOUSE,
        "gitlab",
        "https://about.gitlab.com/jobs/",
        TIER_THREE,
    ),
    Board(
        "Spotify",
        Platform.LEVER,
        "spotify",
        "https://www.lifeatspotify.com/jobs",
        TIER_THREE,
    ),
    Board(
        "Wise",
        Platform.SMARTRECRUITERS,
        "Wise",
        "https://www.wise.jobs/",
        TIER_THREE,
    ),
    Board(
        "Salesforce",
        Platform.WORKDAY,
        "salesforce.wd12.myworkdayjobs.com|salesforce|External_Career_Site",
        "https://careers.salesforce.com/",
        TIER_THREE,
    ),
)


def canonical_company(company: str) -> str:
    """Return a stable display name after removing aliases and legal suffixes."""
    stripped_company = company.strip().rstrip(" ,").strip()
    while stripped_company:
        without_suffix = CORPORATE_SUFFIX_PATTERN.sub("", stripped_company)
        if without_suffix == stripped_company:
            break
        stripped_company = without_suffix.rstrip(" ,").strip()
    return COMPANY_ALIASES.get(normalize(stripped_company), stripped_company)


def company_priority(company: str) -> int:
    """Return the target-list rank, or the list length for other companies."""
    normalized_company = normalize(canonical_company(company))
    for priority, target in enumerate(TARGET_COMPANIES):
        if normalize(target) == normalized_company:
            return priority
    return len(TARGET_COMPANIES)
