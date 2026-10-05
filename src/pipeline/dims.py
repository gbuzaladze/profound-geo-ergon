"""Build Power BI dimension tables from exported fact CSVs.

Dims are the union of keys in both facts so a slicer on DIM_COLUMNS filters
scores and citations together. Written after each fact export.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.common import read_csv, write_csv_and_sql
from pipeline.config import (
    add_country_option,
    countries_from_args,
    data_dir,
    select_country,
)

# Slicer columns present on both facts. Region is the country inside a
# Profound category that covers several markets.
DIM_COLUMNS = ("date", "topic", "platform", "region")
_DIM_LABELS = {
    "date": "dates",
    "topic": "topics",
    "platform": "platforms",
    "region": "regions",
}


def _fact_paths() -> list[Path]:
    """Both facts contribute keys; a missing file is skipped."""
    return [
        data_dir() / "fact_scores_summarized.csv",
        data_dir() / "fact_raw_citations.csv",
    ]


def collect_dim_keys() -> dict[str, set[str]]:
    """Return distinct DIM_COLUMNS values from available facts."""
    keys = {column: set() for column in DIM_COLUMNS}
    for path in _fact_paths():
        if not path.exists():
            continue
        for row in read_csv(path):
            for column in DIM_COLUMNS:
                value = str(row.get(column) or "").strip()
                if value:
                    keys[column].add(value)
    return keys


def _english_list(items: tuple[str, ...] | list[str]) -> str:
    """Join names as 'a, b, and c' for CLI help text."""
    names = list(items)
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + ", and " + names[-1]


def write_dims() -> None:
    """Write one dimension CSV per DIM_COLUMNS entry from both fact tables."""
    keys = collect_dim_keys()
    if not any(keys.values()):
        print("No fact CSVs found; skipped dimension tables.")
        return

    folder = data_dir()
    written = []
    for column in DIM_COLUMNS:
        values = sorted(keys[column])
        written.append(
            write_csv_and_sql(
                [{column: value} for value in values],
                path=folder / f"dim_{column}.csv",
                fieldnames=[column],
            )
        )
    summary = ", ".join(
        f"{len(keys[column])} {_DIM_LABELS.get(column, column + 's')}"
        for column in DIM_COLUMNS
    )
    print(f"Dimension tables written: {summary}")
    for path in written:
        print(f"  {path}")


def main(argv: list[str] | None = None) -> None:
    """Rebuild dimension CSVs from existing fact exports."""
    names = [f"dim_{column}" for column in DIM_COLUMNS]
    parser = argparse.ArgumentParser(description=f"Rebuild {_english_list(names)}.")
    add_country_option(parser)
    args = parser.parse_args(argv)
    for country in countries_from_args(args):
        select_country(country.slug)
        print(f"Dimensions: {country.name}", flush=True)
        write_dims()


if __name__ == "__main__":
    main()
