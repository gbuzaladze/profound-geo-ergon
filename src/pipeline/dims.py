"""Build Power BI dimension tables from exported fact CSVs.

Dims are the union of keys in both facts so a slicer on dim_topic/date/platform
filters scores and citations together. Written after each fact export.
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

DIM_DATE_FIELDS = ["date"]
DIM_TOPIC_FIELDS = ["topic"]
DIM_PLATFORM_FIELDS = ["platform"]


def _fact_paths() -> list[Path]:
    """Both facts contribute keys; a missing file is skipped."""
    return [
        data_dir() / "fact_scores_summarized.csv",
        data_dir() / "fact_raw_citations.csv",
    ]


def collect_dim_keys() -> tuple[set[str], set[str], set[str]]:
    """Return distinct date, topic, and platform values from available facts."""
    dates: set[str] = set()
    topics: set[str] = set()
    platforms: set[str] = set()
    for path in _fact_paths():
        if not path.exists():
            continue
        for row in read_csv(path):
            date_value = str(row.get("date") or "").strip()
            topic_value = str(row.get("topic") or "").strip()
            platform_value = str(row.get("platform") or "").strip()
            if date_value:
                dates.add(date_value)
            if topic_value:
                topics.add(topic_value)
            if platform_value:
                platforms.add(platform_value)
    return dates, topics, platforms


def date_rows(dates: list[str]) -> list[dict[str, object]]:
    """Map distinct ISO dates to dim_date rows."""
    return [{"date": value} for value in dates]


def write_dims() -> None:
    """Write dim_date, dim_topic, and dim_platform from both fact tables."""
    date_values, topic_values, platform_values = collect_dim_keys()
    dates = sorted(date_values)
    topics = sorted(topic_values)
    platforms = sorted(platform_values)
    if not dates and not topics and not platforms:
        print("No fact CSVs found; skipped dimension tables.")
        return

    folder = data_dir()
    written = [
        write_csv_and_sql(
            date_rows(dates), path=folder / "dim_date.csv", fieldnames=DIM_DATE_FIELDS
        ),
        write_csv_and_sql(
            [{"topic": value} for value in topics],
            path=folder / "dim_topic.csv",
            fieldnames=DIM_TOPIC_FIELDS,
        ),
        write_csv_and_sql(
            [{"platform": value} for value in platforms],
            path=folder / "dim_platform.csv",
            fieldnames=DIM_PLATFORM_FIELDS,
        ),
    ]
    print(
        "Dimension tables written: "
        f"{len(dates)} dates, {len(topics)} topics, {len(platforms)} platforms"
    )
    for path in written:
        print(f"  {path}")


def main(argv: list[str] | None = None) -> None:
    """Rebuild dimension CSVs from existing fact exports."""
    parser = argparse.ArgumentParser(
        description="Rebuild dim_date, dim_topic, and dim_platform."
    )
    add_country_option(parser)
    args = parser.parse_args(argv)
    for country in countries_from_args(args):
        select_country(country.slug)
        print(f"Dimensions: {country.name}", flush=True)
        write_dims()


if __name__ == "__main__":
    main()
