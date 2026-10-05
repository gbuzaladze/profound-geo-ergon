"""Load shared Profound export settings from project.toml.

This is the only place client names, category IDs, and scan windows are read.
Secrets stay in `.env`.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_SETTINGS_NAME = "project.toml"
_PIPELINE_TZ = ZoneInfo("America/Toronto")


def project_root() -> Path:
    """Return the repository root directory."""
    for candidate in (Path.cwd(), *Path(__file__).resolve().parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return Path.cwd()


def _require(section: dict[str, Any], key: str, section_name: str) -> Any:
    if key not in section:
        raise SystemExit(f"{_SETTINGS_NAME} is missing [{section_name}] {key}.")
    return section[key]


def _load_project_settings() -> dict[str, Any]:
    path = project_root() / _SETTINGS_NAME
    if not path.exists():
        raise SystemExit(
            f"{_SETTINGS_NAME} is missing. Create it at the repo root "
            "and fill in owned_asset, countries, and start_date."
        )
    with path.open("rb") as file:
        return tomllib.load(file)


def _as_uuid(value: object, key: str) -> str:
    text = str(value).strip()
    try:
        return str(UUID(text))
    except ValueError as error:
        raise SystemExit(
            f"{_SETTINGS_NAME} {key} must be a Profound category UUID."
        ) from error


def _as_iso_date(value: object, key: str) -> str:
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise SystemExit(f"{_SETTINGS_NAME} {key} must be YYYY-MM-DD.") from error


def _as_name_list(value: object, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        raise SystemExit(f"{_SETTINGS_NAME} {key} must be a list of strings.")
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item).strip()
        if not name:
            continue
        folded = name.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        names.append(name)
    return tuple(names)


def _as_positive_int(value: object, key: str) -> int:
    message = f"{_SETTINGS_NAME} {key} must be a positive integer."
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise SystemExit(message) from error
    if number < 1:
        raise SystemExit(message)
    return number


def _as_slug(value: object, key: str) -> str:
    slug = str(value).strip().casefold()
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if not slug or any(char not in allowed for char in slug):
        raise SystemExit(
            f"{_SETTINGS_NAME} {key} must be a lowercase slug "
            "(letters, digits, hyphen)."
        )
    return slug


@dataclass(frozen=True)
class Country:
    """One Profound category exported into data/{slug}/ and schema {slug}.

    `owned_asset` is the Profound brand name used for dashboard KPIs. A
    country may override the project default when Profound uses a local name.
    """

    slug: str
    name: str
    category_id: str
    owned_asset: str

    @property
    def data_dir(self) -> Path:
        return project_root() / "data" / self.slug

    @property
    def dashboard_dir(self) -> Path:
        return project_root() / "dashboard" / self.slug


def _load_countries(raw: object, default_owned_asset: str) -> tuple[Country, ...]:
    if not isinstance(raw, list) or not raw:
        raise SystemExit(
            f"{_SETTINGS_NAME} needs a [[countries]] list with at least one country."
        )
    countries: list[Country] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SystemExit(
                f"{_SETTINGS_NAME} [[countries]] entry {index} must be a table."
            )
        slug = _as_slug(_require(item, "slug", "countries"), "slug")
        if slug in seen:
            raise SystemExit(f"{_SETTINGS_NAME} country slug {slug!r} is duplicated.")
        seen.add(slug)
        name = str(_require(item, "name", "countries")).strip()
        if not name:
            raise SystemExit(f"{_SETTINGS_NAME} country {slug} name cannot be empty.")
        owned = str(item.get("owned_asset") or default_owned_asset).strip()
        if not owned:
            raise SystemExit(
                f"{_SETTINGS_NAME} country {slug} owned_asset cannot be empty."
            )
        countries.append(
            Country(
                slug=slug,
                name=name,
                category_id=_as_uuid(
                    _require(item, "category_id", "countries"), "category_id"
                ),
                owned_asset=owned,
            )
        )
    return tuple(countries)


# Loaded settings


_settings = _load_project_settings()
_project = _settings.get("project")
_ranking = _settings.get("ranking")
_api = _settings.get("api") or {}
if not isinstance(_project, dict):
    raise SystemExit(f"{_SETTINGS_NAME} needs a [project] section.")
if not isinstance(_ranking, dict):
    raise SystemExit(f"{_SETTINGS_NAME} needs a [ranking] section.")
if not isinstance(_api, dict):
    raise SystemExit(f"{_SETTINGS_NAME} [api] must be a table if present.")

# Default owned_asset. A [[countries]] table may override it per market.
ASSET_NAME = str(_require(_project, "owned_asset", "project")).strip()
if not ASSET_NAME:
    raise SystemExit(f"{_SETTINGS_NAME} owned_asset cannot be empty.")

COUNTRIES = _load_countries(_settings.get("countries"), ASSET_NAME)
_COUNTRIES_BY_SLUG = {country.slug: country for country in COUNTRIES}
OWNED_ALIASES = _as_name_list(_project.get("owned_aliases"), "owned_aliases")
OWNED_CITATION_HOSTS = _as_name_list(
    _project.get("owned_citation_hosts"), "owned_citation_hosts"
)
OWNED_CITATION_CONTAINS = _as_name_list(
    _project.get("owned_citation_contains"), "owned_citation_contains"
)
START_DATE = _as_iso_date(_require(_project, "start_date", "project"), "start_date")
TOP_N = _as_positive_int(_require(_ranking, "top_n", "ranking"), "top_n")
LOOKBACK_DAYS = _as_positive_int(
    _require(_ranking, "lookback_days", "ranking"), "lookback_days"
)
HOURLY_API_LIMIT = _as_positive_int(_api.get("hourly_limit", 600), "hourly_limit")

_OWNED_ALIAS_KEYS = frozenset(name.casefold() for name in OWNED_ALIASES if name)
_OWNED_CITATION_HOST_KEYS = frozenset(
    host.casefold().rstrip(".") for host in OWNED_CITATION_HOSTS if host
)
_OWNED_CITATION_CONTAINS = tuple(
    token.casefold() for token in OWNED_CITATION_CONTAINS if token
)
_active: Country | None = None


# Owned matching and country selection


def is_owned_name(name: str | None) -> bool:
    """True when name matches the active country's owned_asset or owned_aliases."""
    if not name:
        return False
    folded = name.casefold()
    return folded == owned_asset().casefold() or folded in _OWNED_ALIAS_KEYS


def _host_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().casefold().rstrip(".")


def is_owned_citation_host(
    hostname: object | None, domain: object | None = None
) -> bool:
    """True when hostname or domain is forced to the Owned citation category.

    An owned_citation_hosts entry matches the hostname, the registrable domain,
    or a parent of the hostname (brand.com matches www.brand.com).
    An owned_citation_contains token matches when it appears anywhere in the
    hostname or domain, so one token covers country sites that include it.
    """
    host = _host_key(hostname)
    domain_key = _host_key(domain)
    if not host and not domain_key:
        return False
    if any(token in host or token in domain_key for token in _OWNED_CITATION_CONTAINS):
        return True
    return any(
        host == entry or domain_key == entry or host.endswith("." + entry)
        for entry in _OWNED_CITATION_HOST_KEYS
    )


def owned_asset() -> str:
    """Return the active country's Profound brand name for dashboard KPIs."""
    return active_country().owned_asset


def owned_asset_names() -> tuple[str, ...]:
    """Primary owned asset plus configured aliases, de-duplicated."""
    primary = owned_asset()
    names = [primary]
    seen = {primary.casefold()}
    for alias in OWNED_ALIASES:
        folded = alias.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        names.append(alias)
    return tuple(names)


def country_by_slug(slug: str) -> Country:
    """Return a configured country, or exit if the slug is unknown."""
    country = _COUNTRIES_BY_SLUG.get(slug.strip().casefold())
    if country is None:
        known = ", ".join(item.slug for item in COUNTRIES)
        raise SystemExit(f"Unknown country {slug!r}. Configured: {known}.")
    return country


def resolve_countries(slugs: list[str] | None) -> tuple[Country, ...]:
    """Return requested countries, or every configured country when slugs is empty."""
    if not slugs:
        return COUNTRIES
    return tuple(country_by_slug(slug) for slug in slugs)


def add_country_option(parser: argparse.ArgumentParser) -> None:
    """Add a repeatable --country option. Default is every configured country."""
    slugs = ", ".join(country.slug for country in COUNTRIES)
    parser.add_argument(
        "--country",
        action="append",
        dest="countries",
        metavar="SLUG",
        help=f"Country to run ({slugs}). Repeatable. Default: all.",
    )


def countries_from_args(args: argparse.Namespace) -> tuple[Country, ...]:
    """Resolve --country from a parsed argparse namespace."""
    return resolve_countries(getattr(args, "countries", None))


def select_country(slug: str, *, create_data_dir: bool = True) -> Country:
    """Activate one country for the rest of this process.

    Subsequent `data_dir()` / `category_id()` calls use this market. CLI calls
    create `data/{slug}/`; SQL-only cloud calls can suppress that local write.
    """
    global _active
    _active = country_by_slug(slug)
    if create_data_dir:
        _active.data_dir.mkdir(parents=True, exist_ok=True)
    return _active


def active_country() -> Country:
    """Return the country selected by select_country()."""
    if _active is None:
        raise SystemExit(
            "No country is selected. Pass --country or call select_country()."
        )
    return _active


def category_id() -> str:
    """Return the active country's Profound category UUID."""
    return active_country().category_id


def data_dir() -> Path:
    """Return data/{slug}/ for the active country."""
    return active_country().data_dir


# Scan window


def pipeline_today() -> date:
    """Return the current calendar date in the reporting timezone."""
    return datetime.now(_PIPELINE_TZ).date()


def load_configuration() -> None:
    """Load local settings and require PROFOUND_API_KEY in the environment."""
    load_dotenv(project_root() / ".env")
    if not os.getenv("PROFOUND_API_KEY"):
        raise SystemExit("PROFOUND_API_KEY is missing from the environment.")
    country = active_country()
    print(
        f"Project: {country.owned_asset}  |  {country.name}  |  "
        f"category {country.category_id}  |  from {START_DATE}  |  "
        f"top {TOP_N} per metric per topic",
        flush=True,
    )


def end_date() -> str:
    """Return yesterday as the inclusive scan end.

    Today's Profound data can still be incomplete depending on pull time.
    """
    return (pipeline_today() - timedelta(days=1)).isoformat()


def lookback_start(days: int | None = None) -> str:
    """Return the inclusive start date for a trailing lookback window."""
    window = LOOKBACK_DAYS if days is None else days
    # Inclusive: a 30-day window is scan end plus the previous 29 days.
    return (date.fromisoformat(end_date()) - timedelta(days=window - 1)).isoformat()


def drop_incomplete_dates[RowT: dict](rows: list[RowT]) -> list[RowT]:
    """Drop rows dated today or later; that data may still be incomplete.

    Profound can still be filling in the pull day, so both facts scan through
    yesterday only. Comparison is string ISO dates (YYYY-MM-DD).
    """
    cutoff = pipeline_today().isoformat()
    kept = [row for row in rows if str(row.get("date") or "") < cutoff]
    dropped = len(rows) - len(kept)
    if dropped:
        print(f"Dropped {dropped} rows dated {cutoff} or later.")
    return kept

