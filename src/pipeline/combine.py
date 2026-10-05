"""Concatenate per-category CSV exports into data/.

Each Profound category is written under data/{slug}/ during the pull. This
module merges those files into data/ and then removes the category folders.
Refreshing one category keeps the other category's rows already in data/,
matched by region.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
from pathlib import Path

from pipeline.config import COUNTRIES, project_root

SCORE_FILE = "fact_scores_summarized.csv"
CITATION_FILE = "fact_raw_citations.csv"
DIM_FILES = (
    ("dim_date.csv", "date"),
    ("dim_topic.csv", "topic"),
    ("dim_platform.csv", "platform"),
    ("dim_region.csv", "region"),
)


def combined_dir() -> Path:
    """Return data/, the concatenated export folder."""
    return project_root() / "data"


def _header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        header = next(reader, None)
    if not header:
        raise SystemExit(f"{path} has no header.")
    return header


def concat_csvs(sources: list[Path], destination: Path) -> int:
    """Stream-concatenate CSVs that share one header. Returns the row count."""
    if not sources:
        raise SystemExit("No category CSVs to combine.")
    fieldnames = _header(sources[0])
    for path in sources[1:]:
        header = _header(path)
        if header != fieldnames:
            raise SystemExit(
                f"{path.name} columns {header} do not match {sources[0].name}."
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.stem + ".tmp.csv")
    written = 0
    with temp_path.open("w", newline="", encoding="utf-8") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for path in sources:
            with path.open(newline="", encoding="utf-8") as in_file:
                for row in csv.DictReader(in_file):
                    writer.writerow(row)
                    written += 1
    try:
        temp_path.replace(destination)
    except PermissionError:
        print(
            f"Could not replace {destination} (file is open). "
            f"Left new export at {temp_path}."
        )
        return written
    return written


def _union_column(sources: list[Path], column: str) -> list[str]:
    values: set[str] = set()
    for path in sources:
        with path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                value = str(row.get(column) or "").strip()
                if value:
                    values.add(value)
    return sorted(values)


def _regions_in(paths: list[Path]) -> set[str]:
    regions: set[str] = set()
    for path in paths:
        with path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                region = str(row.get("region") or "").strip()
                if region:
                    regions.add(region)
    return regions


def _write_combined_scores(
    sources: list[Path], destination: Path, replaced_regions: set[str]
) -> int:
    """Write new category rows plus combined rows for regions not refreshed."""
    fieldnames = _header(sources[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.stem + ".tmp.csv")
    written = 0
    with temp_path.open("w", newline="", encoding="utf-8") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if destination.is_file():
            with destination.open(newline="", encoding="utf-8") as existing:
                for row in csv.DictReader(existing):
                    region = str(row.get("region") or "").strip()
                    if region in replaced_regions:
                        continue
                    writer.writerow(row)
                    written += 1
        for path in sources:
            with path.open(newline="", encoding="utf-8") as in_file:
                for row in csv.DictReader(in_file):
                    writer.writerow(row)
                    written += 1
    try:
        temp_path.replace(destination)
    except PermissionError:
        print(
            f"Could not replace {destination} (file is open). "
            f"Left new export at {temp_path}."
        )
    return written


def _remove_tree(folder: Path) -> None:
    """Delete a directory. OneDrive can deny Python's rmdir on an empty folder."""
    if not folder.exists():
        return
    try:
        shutil.rmtree(folder)
    except PermissionError:
        subprocess.run(
            ["cmd", "/c", "rmdir", "/s", "/q", str(folder)],
            check=False,
        )
    if folder.exists():
        raise SystemExit(f"Could not remove {folder}.")


def _remove_category_dirs(sources: list[Path]) -> None:
    """Delete data/{slug}/ after its rows are in the combined file."""
    data_root = combined_dir().resolve()
    for path in sources:
        folder = path.parent.resolve()
        if folder == data_root or data_root not in folder.parents:
            continue
        _remove_tree(folder)
        print(f"Removed {folder}")


def _category_sources(filename: str) -> list[Path]:
    """Return data/{slug}/{filename} for categories that exported that file."""
    return [
        country.data_dir / filename
        for country in COUNTRIES
        if (country.data_dir / filename).is_file()
    ]


def _merge_fact(sources: list[Path], destination: Path, label: str) -> int:
    """Replace combined rows for regions in this pull and keep the other category."""
    replaced_regions = _regions_in(sources)
    rows = _write_combined_scores(sources, destination, replaced_regions)
    print(f"Combined {label} written to {destination}: {rows} rows")
    return rows


def combine_category_exports() -> Path | None:
    """Rebuild data/*.csv from category folders, then remove those folders."""
    score_sources = _category_sources(SCORE_FILE)
    citation_sources = _category_sources(CITATION_FILE)
    if not score_sources and not citation_sources:
        print("No category CSVs found; skipped combined export.")
        return None

    folder = combined_dir()
    destination: Path | None = None
    if score_sources:
        destination = folder / SCORE_FILE
        _merge_fact(score_sources, destination, "scores")
    if citation_sources:
        citation_destination = folder / CITATION_FILE
        _merge_fact(citation_sources, citation_destination, "citations")
        destination = destination or citation_destination

    fact_paths = [
        path
        for path in (folder / SCORE_FILE, folder / CITATION_FILE)
        if path.is_file()
    ]
    for filename, column in DIM_FILES:
        values = _union_column(fact_paths, column)
        dim_path = folder / filename
        written = concat_csvs_from_rows(
            dim_path, [column], [{column: value} for value in values]
        )
        print(f"  {written.name}: {len(values)}")

    _remove_category_dirs(score_sources + citation_sources)
    legacy = folder / "all"
    if legacy.is_dir():
        _remove_tree(legacy)
        print(f"Removed {legacy}")
    return destination


def concat_csvs_from_rows(
    destination: Path, fieldnames: list[str], rows: list[dict[str, str]]
) -> Path:
    """Write a small CSV, replacing the target unless it is open."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.stem + ".tmp.csv")
    with temp_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    try:
        temp_path.replace(destination)
    except PermissionError:
        print(
            f"Could not replace {destination} (file is open). "
            f"Left new export at {temp_path}."
        )
        return temp_path
    return destination
