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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pipeline.common import replace_file, write_csv
from pipeline.config import COUNTRIES, project_root
from pipeline.dims import DIM_COLUMNS, collect_dim_keys

SCORE_FILE = "fact_scores_summarized.csv"
CITATION_FILE = "fact_raw_citations.csv"


def combined_dir() -> Path:
    """Return data/, the concatenated export folder."""
    return project_root() / "data"


@contextmanager
def _open_csv_destination(
    destination: Path, fieldnames: list[str]
) -> Iterator[csv.DictWriter]:
    """Write a temp CSV, then replace the destination unless that file is open."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.stem + ".tmp.csv")
    with temp_path.open("w", newline="", encoding="utf-8") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        yield writer
    replace_file(temp_path, destination)


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

    written = 0
    with _open_csv_destination(destination, fieldnames) as writer:
        for path in sources:
            with path.open(newline="", encoding="utf-8") as in_file:
                for row in csv.DictReader(in_file):
                    writer.writerow(row)
                    written += 1
    return written


def _regions_in(paths: list[Path]) -> set[str]:
    regions: set[str] = set()
    for path in paths:
        with path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                region = str(row.get("region") or "").strip()
                if region:
                    regions.add(region)
    return regions


def _write_merged_fact(
    sources: list[Path], destination: Path, replaced_regions: set[str]
) -> int:
    """Keep combined rows whose region was not in this pull, then append new rows.

    A blank region is not treated as a refreshed country, so those rows stay.
    """
    fieldnames = _header(sources[0])
    written = 0
    with _open_csv_destination(destination, fieldnames) as writer:
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
    """Delete each data/{slug}/ once after its rows are in the combined file."""
    data_root = combined_dir().resolve()
    seen: set[Path] = set()
    for path in sources:
        folder = path.parent.resolve()
        if folder in seen or folder == data_root or data_root not in folder.parents:
            continue
        seen.add(folder)
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
    rows = _write_merged_fact(sources, destination, replaced_regions)
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
    keys = collect_dim_keys(fact_paths)
    for column in DIM_COLUMNS:
        values = sorted(keys[column])
        dim_path = folder / f"dim_{column}.csv"
        written = write_csv(
            [{column: value} for value in values],
            path=dim_path,
            fieldnames=[column],
        )
        print(f"  {written.name}: {len(values)}")

    _remove_category_dirs(score_sources + citation_sources)
    legacy = folder / "all"
    if legacy.is_dir():
        _remove_tree(legacy)
        print(f"Removed {legacy}")
    return destination
