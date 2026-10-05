"""Export active Visibility prompts for the Profound category.

`dim_prompt` is a snapshot of currently active prompts (not a history). Tags,
regions, and platforms are pipe-joined to match citation `tags`.
"""

from __future__ import annotations

import argparse
from time import perf_counter

from profound import Profound

from pipeline.common import call_api, profound_client, ref_name, write_csv_and_sql
from pipeline.config import (
    add_country_option,
    category_id,
    countries_from_args,
    data_dir,
    select_country,
)

# Prompt lists are small; one large page avoids extra API calls.
PAGE_SIZE = 1000
FIELDNAMES = [
    "prompt",
    "topic",
    "tags",
    "regions",
    "platforms",
]


def join_refs(value: object) -> str | None:
    """Join named refs with a pipe separator, matching citation tags."""
    if value is None:
        return None
    if isinstance(value, list):
        names = [ref_name(item) for item in value]
        return "|".join(name for name in names if name) or None
    return ref_name(value)


def flatten_prompt(item: object) -> dict[str, object]:
    """Map one Profound prompt to export columns."""
    return {
        "prompt": getattr(item, "prompt", None),
        "topic": ref_name(getattr(item, "topic", None)),
        "tags": join_refs(getattr(item, "tags", None)),
        "regions": join_refs(getattr(item, "regions", None)),
        "platforms": join_refs(getattr(item, "platforms", None)),
    }


def fetch_prompts(client: Profound) -> tuple[list[dict[str, object]], int]:
    """Page through active Visibility prompts for this category."""
    rows: list[dict[str, object]] = []
    cursor: str | None = None
    api_calls = 0
    while True:
        api_calls += 1
        # Active Visibility prompts only; retired prompts are omitted.
        response = call_api(
            client.organizations.categories.prompts,
            category_id(),
            analysis_type=["visibility"],
            status=["active"],
            order_by="prompt",
            order_dir="asc",
            limit=PAGE_SIZE,
            cursor=cursor,
        )
        page = list(response.data)
        if not page:
            break
        rows.extend(flatten_prompt(item) for item in page)
        cursor = response.info.next_cursor
        if not cursor:
            break
    return rows, api_calls


def export(*, csv_output: bool = True) -> dict[str, object]:
    """Replace active prompts in SQL, optionally retaining the local CSV output."""
    started = perf_counter()
    with profound_client() as client:
        rows, api_calls = fetch_prompts(client)
    if not rows:
        raise SystemExit("No active visibility prompts were returned.")

    rows.sort(
        key=lambda row: (str(row.get("topic") or ""), str(row.get("prompt") or ""))
    )
    if csv_output:
        destination = str(
            write_csv_and_sql(
                rows, path=data_dir() / "dim_prompt.csv", fieldnames=FIELDNAMES
            )
        )
    else:
        from pipeline.db import replace_table

        replace_table("dim_prompt", rows, FIELDNAMES)
        destination = "Azure SQL"
    minutes = (perf_counter() - started) / 60
    print(f"Prompts table written to {destination}: {len(rows)} rows")
    print(f"Runtime: {minutes:.1f} minutes; API calls: {api_calls}")
    return {
        "table": "dim_prompt",
        "rows": len(rows),
        "api_calls": api_calls,
        "minutes": round(minutes, 2),
    }


def main(argv: list[str] | None = None) -> None:
    """Export active Visibility prompts for one or more countries."""
    parser = argparse.ArgumentParser(description="Export active Visibility prompts.")
    add_country_option(parser)
    args = parser.parse_args(argv)
    for country in countries_from_args(args):
        select_country(country.slug)
        print(f"Prompts: {country.name}", flush=True)
        export()


if __name__ == "__main__":
    main()
