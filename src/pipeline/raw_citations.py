"""Export raw Visibility citation rows matching the Profound Citations UI.

One row per citation on a Visibility answer. Duplicate URLs on the same day
are kept (no primary key). Full mode backfills START_DATE through yesterday
and can resume from a checkpoint. Incremental mode re-pulls from the last
date already in the CSV through yesterday and keeps older rows.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from urllib.parse import urlparse

import profound
import tldextract
from profound import Profound

from pipeline.citation_author import apply_authors, author_from_url
from pipeline.common import (
    RATE_LIMIT_SLEEP_SECONDS,
    HourlyRateLimiter,
    append_csv,
    profound_client,
    rate_limiter,
    read_csv,
    ref_name,
    write_csv_and_sql,
)
from pipeline.combine import combine_category_exports
from pipeline.config import (
    HOURLY_API_LIMIT,
    START_DATE,
    active_country,
    add_country_option,
    category_id,
    countries_from_args,
    data_dir,
    drop_incomplete_dates,
    end_date,
    is_owned_citation_host,
    project_root,
    select_country,
)
from pipeline.dims import write_dims


def output_path() -> Path:
    return data_dir() / "fact_raw_citations.csv"


def partial_path() -> Path:
    return data_dir() / "fact_raw_citations.partial.csv"


def checkpoint_path() -> Path:
    return data_dir() / "fact_raw_citations.checkpoint.json"


PAGE_SIZE = 200
# Profound answers_v2 fields needed to flatten citations and map model -> platform.
INCLUDE = [
    "date",
    "model",
    "topic",
    "region",
    "tags",
    "mentions",
    "citations",
    "citation_details",
]
FIELDNAMES = [
    "date",
    "topic",
    "platform",
    "category",
    "subcategory",
    "pag",
    "mentioned",
    "url",
    "hostname",
    "domain",
    "path",
    "author",
    "tags",
    "region",
]
# Citations UI is Visibility only; other analysis types are out of scope.
VISIBILITY_FILTER = {
    "field": "analysis_type",
    "op": "is",
    "value": "visibility",
}


# Row helpers


def join_values(value: object) -> str | None:
    """Join list-like values with a pipe separator."""
    if value is None:
        return None
    if isinstance(value, list):
        return "|".join(str(item) for item in value) or None
    return str(value)


def as_dict(value: object) -> dict[str, Any]:
    """Convert an SDK object or mapping to a plain dict."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {
        key: getattr(value, key)
        for key in ("url", "hostname", "path", "citation_category")
        if hasattr(value, key)
    }


def hostname_from_url(url: str) -> str | None:
    """Return the URL hostname, or None if the URL has no network location."""
    return urlparse(url).hostname


def domain_from_hostname(hostname: object) -> str | None:
    """Return the registrable domain using the Public Suffix List."""
    if not isinstance(hostname, str):
        return None
    host = hostname.strip().lower()
    if not host:
        return None
    extracted = tldextract.extract(host)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}"
    if extracted.suffix:
        return extracted.suffix
    if extracted.domain:
        return extracted.domain
    return host


# Title-casing would leave these as "Institution" or "Earned Institutions".
# Profound's category, including the domain-file spelling, is Institutions.
CATEGORY_LABELS = {
    "earned institutions": "Institutions",
    "institution": "Institutions",
}


def display_category(value: object) -> str | None:
    """Turn API category keys into display labels (earned_media -> Earned Media).

    A standalone "pr" token (any casing) is always "PR", e.g. pr_wire -> PR Wire.
    institution and earned_institutions are both labeled Institutions.
    """
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    override = CATEGORY_LABELS.get(key.replace("_", " ").casefold())
    if override:
        return override
    words = key.replace("_", " ").split()
    return " ".join("PR" if word.casefold() == "pr" else word.title() for word in words)


def category_label(category: object, hostname: object, domain: object) -> str | None:
    """Return the export category, forcing Owned for configured hosts."""
    if is_owned_citation_host(hostname, domain):
        return "Owned"
    return display_category(category)


def _domain_key(value: object) -> str:
    """Normalize a domain for lookup. A trailing dot is not part of the name."""
    return str(value or "").strip().casefold().rstrip(".")


def citation_domains_path(slug: str) -> Path:
    """Return citation-domains/citation-domains-{slug}.csv."""
    return project_root() / "citation-domains" / f"citation-domains-{slug}.csv"


def _pag_label(value: object) -> str:
    """Return TRUE, FALSE, or blank. Blank is not the same as FALSE."""
    text = str(value or "").strip().casefold()
    if text == "true":
        return "TRUE"
    if text == "false":
        return "FALSE"
    return ""


def load_citation_domains(slug: str) -> dict[str, tuple[str, str, str]]:
    """Load domain → (category, subcategory, pag) for one country.

    A missing file means that country has no list yet. Keys use `_domain_key`.
    """
    path = citation_domains_path(slug)
    if not path.is_file():
        return {}
    table: dict[str, tuple[str, str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            domain = _domain_key(row.get("domain"))
            if not domain:
                continue
            table[domain] = (
                str(row.get("category") or "").strip(),
                str(row.get("subcategory") or "").strip(),
                _pag_label(row.get("pag")),
            )
    return table


def citation_row(
    *,
    day: object,
    topic: object,
    platform: object,
    category: object,
    mentioned: object,
    url: object,
    hostname: object,
    path: object,
    tags: object,
    region: object,
) -> dict[str, object]:
    """Build one export row, deriving domain from hostname and author from URL.

    subcategory and pag stay blank here. apply_category_rules fills them when
    the domain is in the country file.
    """
    domain = domain_from_hostname(hostname)
    return {
        "date": day,
        "topic": topic,
        "platform": platform,
        "category": category_label(category, hostname, domain),
        "subcategory": None,
        "pag": None,
        "mentioned": mentioned,
        "url": url,
        "hostname": hostname,
        "domain": domain,
        "path": path,
        "author": author_from_url(url, hostname),
        "tags": tags,
        "region": region,
    }


def apply_category_rules(
    rows: list[dict[str, object]],
    domains: dict[str, tuple[str, str, str]] | None = None,
) -> list[dict[str, object]]:
    """Apply Owned-host labels, then the country domain file.

    A listed domain overwrites category and sets subcategory and pag. The
    file category is shown with Profound's labels, so Institution becomes
    Institutions. Any other domain keeps the Profound category, including
    Owned-host overrides, with subcategory and pag blank. A blank category
    cell in the file does not wipe the Profound label.
    """
    if domains is None:
        domains = load_citation_domains(active_country().slug)
    for row in rows:
        hostname = row.get("hostname")
        domain = row.get("domain") or domain_from_hostname(hostname)
        row["domain"] = domain
        row["category"] = category_label(row.get("category"), hostname, domain)
        file_category, subcategory, pag = domains.get(
            _domain_key(domain), ("", "", "")
        )
        labeled = display_category(file_category)
        if labeled:
            row["category"] = labeled
        row["subcategory"] = subcategory or None
        row["pag"] = pag or None
    return rows


def _answer_field(payload: dict[str, Any], answer: object, key: str) -> object:
    """Prefer the serialized payload, then the SDK object attribute."""
    return payload.get(key) or getattr(answer, key, None)


def flatten_answer(answer: object) -> list[dict[str, object]]:
    """Turn one answer's citations into flat CSV rows."""
    payload = answer.to_dict() if hasattr(answer, "to_dict") else {}
    day = _answer_field(payload, answer, "date")
    platform = ref_name(_answer_field(payload, answer, "model"))
    topic = ref_name(_answer_field(payload, answer, "topic"))
    region = ref_name(_answer_field(payload, answer, "region"))
    tags = join_values(_answer_field(payload, answer, "tags"))
    mentioned = join_values(_answer_field(payload, answer, "mentions"))
    details = _answer_field(payload, answer, "citation_details") or []
    citations = _answer_field(payload, answer, "citations") or []

    rows: list[dict[str, object]] = []
    if details:
        for detail in details:
            item = as_dict(detail)
            url = item.get("url")
            hostname = item.get("hostname")
            if not hostname and isinstance(url, str):
                hostname = hostname_from_url(url)
            rows.append(
                citation_row(
                    day=day,
                    topic=topic,
                    platform=platform,
                    category=item.get("citation_category"),
                    mentioned=mentioned,
                    url=url,
                    hostname=hostname,
                    path=item.get("path"),
                    tags=tags,
                    region=region,
                )
            )
        return rows

    for url in citations:
        if not isinstance(url, str):
            continue
        hostname = hostname_from_url(url)
        rows.append(
            citation_row(
                day=day,
                topic=topic,
                platform=platform,
                category=None,
                mentioned=mentioned,
                url=url,
                hostname=hostname,
                path=urlparse(url).path or None,
                tags=tags,
                region=region,
            )
        )
    return rows


def sort_key(row: dict[str, object]) -> tuple[str, str, str, str]:
    """Sort ascending by date, region, topic, then platform."""
    return (
        str(row.get("date") or ""),
        str(row.get("region") or ""),
        str(row.get("topic") or ""),
        str(row.get("platform") or ""),
    )


# CLI


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse full vs incremental export flags."""
    parser = argparse.ArgumentParser(description="Export Profound raw citation rows.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--full",
        action="store_true",
        help="Original backfill from START_DATE (pauses at the hourly API limit).",
    )
    mode.add_argument(
        "--incremental",
        action="store_true",
        help="Refresh from the last date already in data/{country}/fact_raw_citations.csv.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="With --full, ignore any checkpoint and start over.",
    )
    parser.add_argument(
        "--enrich-authors",
        action="store_true",
        help=(
            "Recompute author on the existing fact_raw_citations.csv "
            "(no Profound pull), then rewrite CSV and Azure SQL."
        ),
    )
    add_country_option(parser)
    return parser.parse_args(argv)


# Checkpoints


def load_checkpoint() -> dict[str, Any] | None:
    """Return a valid full-pull checkpoint, or None."""
    if not checkpoint_path().exists() or not partial_path().exists():
        return None
    payload = json.loads(checkpoint_path().read_text(encoding="utf-8"))
    if payload.get("start_date") != START_DATE:
        return None
    return payload


def save_checkpoint(
    *,
    cursor: str | None,
    start: str,
    scan_end: str,
    answers_scanned: int,
    api_calls: int,
) -> None:
    """Persist pagination state so a full pull can resume."""
    checkpoint_path().write_text(
        json.dumps(
            {
                "cursor": cursor,
                "start_date": start,
                "end_date": scan_end,
                "answers_scanned": answers_scanned,
                "api_calls": api_calls,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def clear_checkpoint() -> None:
    """Remove full-pull resume files."""
    checkpoint_path().unlink(missing_ok=True)
    partial_path().unlink(missing_ok=True)


# API paging


def request_answers(
    client: Profound,
    *,
    start: str,
    scan_end: str,
    cursor: str | None,
    limiter: HourlyRateLimiter,
) -> Any:
    """Fetch one answers page, waiting on the hourly cap or a 429."""
    while True:
        limiter.wait()
        try:
            return client.prompts.answers_v2(
                category_id=category_id(),
                start_date=start,
                end_date=scan_end,
                include=INCLUDE,
                filter=VISIBILITY_FILTER,
                limit=PAGE_SIZE,
                cursor=cursor,
            )
        except profound.RateLimitError:
            print(
                "Profound returned 429 Too Many Requests; "
                f"sleeping {RATE_LIMIT_SLEEP_SECONDS / 60:.0f} minutes.",
                flush=True,
            )
            sleep(RATE_LIMIT_SLEEP_SECONDS)


def fetch_citation_rows(
    client: Profound,
    *,
    start: str,
    scan_end: str,
    limiter: HourlyRateLimiter,
    cursor: str | None = None,
    answers_scanned: int = 0,
    api_calls: int = 0,
    checkpoint: bool = False,
    existing_rows: list[dict[str, object]] | None = None,
) -> tuple[list[dict[str, object]], int, int]:
    """Page through Visibility answers and flatten citation rows."""
    rows: list[dict[str, object]] = list(existing_rows or [])

    while True:
        api_calls += 1
        response = request_answers(
            client,
            start=start,
            scan_end=scan_end,
            cursor=cursor,
            limiter=limiter,
        )
        page = list(response.data)
        if not page:
            break

        page_rows: list[dict[str, object]] = []
        for answer in page:
            page_rows.extend(flatten_answer(answer))
            answers_scanned += 1
        rows.extend(page_rows)

        cursor = response.info.next_cursor
        total = response.info.total_results
        print(
            f"Scanned {answers_scanned}"
            + (f"/{total}" if total is not None else "")
            + f" answers; collected {len(rows)} citation rows",
            flush=True,
        )

        if checkpoint:
            append_csv(
                page_rows,
                path=partial_path(),
                fieldnames=FIELDNAMES,
                write_header=(
                    not partial_path().exists() or partial_path().stat().st_size == 0
                ),
            )
            save_checkpoint(
                cursor=cursor,
                start=start,
                scan_end=scan_end,
                answers_scanned=answers_scanned,
                api_calls=api_calls,
            )

        if not cursor:
            break

    return rows, answers_scanned, api_calls


# Export modes


def last_date_in_rows(rows: list[dict[str, object]], *, path: Path) -> str:
    """Return the latest date string in citation rows."""
    dates = [str(row.get("date") or "") for row in rows if row.get("date")]
    if not dates:
        raise SystemExit(f"{path} has no dated rows. Run a --full pull first.")
    return max(dates)


def run_full(*, fresh: bool) -> tuple[list[dict[str, object]], int, int, str, str]:
    """Original backfill from START_DATE, with hourly-limit pauses and resume."""
    scan_end = end_date()
    cursor: str | None = None
    answers_scanned = 0
    api_calls = 0
    existing_rows: list[dict[str, object]] | None = None
    start = START_DATE

    if fresh:
        clear_checkpoint()
        print("Starting a fresh full pull.", flush=True)
    else:
        checkpoint = load_checkpoint()
        if checkpoint:
            cursor = checkpoint.get("cursor")
            answers_scanned = int(checkpoint.get("answers_scanned") or 0)
            api_calls = int(checkpoint.get("api_calls") or 0)
            scan_end = str(checkpoint.get("end_date") or scan_end)
            existing_rows = read_csv(partial_path())
            print(
                f"Resuming full pull from checkpoint: "
                f"{answers_scanned} answers already scanned, "
                f"{len(existing_rows)} rows on disk.",
                flush=True,
            )
            if not cursor:
                print("Checkpoint already finished paging; finalizing.", flush=True)
                return existing_rows, answers_scanned, api_calls, start, scan_end
        elif checkpoint_path().exists() and not partial_path().exists():
            raise SystemExit(
                "Found a checkpoint without a partial CSV. "
                "Rerun with --full --fresh to start over."
            )

    print(
        f"Full pull {start} through {scan_end} "
        f"(pauses at {HOURLY_API_LIMIT} API calls/hour).",
        flush=True,
    )
    limiter = rate_limiter()
    with profound_client() as client:
        rows, answers_scanned, api_calls = fetch_citation_rows(
            client,
            start=start,
            scan_end=scan_end,
            limiter=limiter,
            cursor=cursor,
            answers_scanned=answers_scanned,
            api_calls=api_calls,
            checkpoint=True,
            existing_rows=existing_rows,
        )
    return rows, answers_scanned, api_calls, start, scan_end


def run_incremental() -> tuple[list[dict[str, object]], int, int, str, str]:
    """Replace the last stored date through yesterday; keep older rows.

    The last date is re-fetched because that day's Profound answers can still
    change after the previous pull.
    """
    if not output_path().exists():
        raise SystemExit(f"{output_path()} does not exist. Run a --full pull first.")

    existing = drop_incomplete_dates(read_csv(output_path()))
    start = last_date_in_rows(existing, path=output_path())
    scan_end = end_date()
    kept = [row for row in existing if str(row.get("date") or "") < start]
    print(
        f"Incremental pull {start} through {scan_end} ({len(kept)} older rows kept).",
        flush=True,
    )

    limiter = rate_limiter()
    with profound_client() as client:
        new_rows, answers_scanned, api_calls = fetch_citation_rows(
            client,
            start=start,
            scan_end=scan_end,
            limiter=limiter,
        )
    return kept + new_rows, answers_scanned, api_calls, start, scan_end


def run_incremental_sql() -> tuple[list[dict[str, object]], int, int, str, str]:
    """Use SQL as the watermark, then build a complete replacement dataset."""
    from pipeline.db import latest_table_date, read_rows_before

    start = latest_table_date("fact_raw_citations")
    scan_end = end_date()
    kept = read_rows_before("fact_raw_citations", start)
    print(
        f"SQL incremental pull {start} through {scan_end} "
        f"({len(kept)} older rows kept).",
        flush=True,
    )
    limiter = rate_limiter()
    with profound_client() as client:
        new_rows, answers_scanned, api_calls = fetch_citation_rows(
            client,
            start=start,
            scan_end=scan_end,
            limiter=limiter,
        )
    return kept + new_rows, answers_scanned, api_calls, start, scan_end


def export(
    *,
    mode: str | None = None,
    fresh: bool = False,
    csv_output: bool = True,
    state_source: str = "csv",
    rebuild_dimensions: bool = True,
) -> dict[str, object]:
    """Export raw citations for the active country.

    `mode` is `full`, `incremental`, or None (incremental if the CSV exists).
    `fresh` only applies to full pulls: ignore checkpoint and start over.
    Cloud execution uses `state_source="sql"` with `csv_output=False`.
    """
    started = perf_counter()
    if state_source not in {"csv", "sql"}:
        raise ValueError("state_source must be 'csv' or 'sql'.")
    if state_source == "sql" and mode != "incremental":
        raise ValueError("SQL-backed citation exports must use incremental mode.")
    resolved = mode or ("incremental" if output_path().exists() else "full")
    print(f"Mode: {resolved}", flush=True)

    if resolved == "full":
        rows, answers_scanned, api_calls, start, scan_end = run_full(fresh=fresh)
    elif state_source == "sql":
        rows, answers_scanned, api_calls, start, scan_end = run_incremental_sql()
    else:
        rows, answers_scanned, api_calls, start, scan_end = run_incremental()

    rows = drop_incomplete_dates(rows)
    if not rows:
        raise SystemExit("No citation rows were returned for the scan window.")

    rows = apply_category_rules(rows)
    # Incremental pulls only look up the refreshed dates. --enrich-authors
    # still walks the whole file.
    rows = apply_authors(
        rows, live_from=start if resolved == "incremental" else None
    )
    rows.sort(key=sort_key)
    if csv_output:
        destination = str(
            write_csv_and_sql(rows, path=output_path(), fieldnames=FIELDNAMES)
        )
    else:
        from pipeline.db import replace_table

        replace_table("fact_raw_citations", rows, FIELDNAMES)
        destination = "Azure SQL"
    if rebuild_dimensions:
        if csv_output:
            write_dims()
        else:
            from pipeline.db import rebuild_dimensions_from_sql

            rebuild_dimensions_from_sql()
    if resolved == "full":
        # Checkpoint is only for a paused backfill; incremental has no partial file.
        clear_checkpoint()

    minutes = (perf_counter() - started) / 60
    print(f"Raw citations table written to {destination}: {len(rows)} rows")
    print(f"Runtime: {minutes:.1f} minutes; API calls: {api_calls}")
    print(f"Answers scanned: {answers_scanned}")
    print(f"Scan window: {start} through {scan_end}")
    print("Sorted by: date, region, topic, platform")
    print("Prompt type filter: visibility")
    return {
        "table": "fact_raw_citations",
        "rows": len(rows),
        "api_calls": api_calls,
        "answers_scanned": answers_scanned,
        "minutes": round(minutes, 2),
        "start": start,
        "end": scan_end,
    }


def _author_fill_counts(rows: list[dict[str, object]]) -> dict[str, dict[str, int]]:
    """Count citation rows and filled authors by Reddit/YouTube/Instagram/other."""
    from pipeline.citation_author import _host_matches, _hostname

    counts = {
        name: {"rows": 0, "filled": 0}
        for name in ("reddit", "youtube", "instagram", "other")
    }
    for row in rows:
        url = str(row.get("url") or "")
        host = _hostname(url, row.get("hostname"))
        if _host_matches(host, "reddit.com"):
            platform = "reddit"
        elif host == "youtu.be" or _host_matches(host, "youtube.com"):
            platform = "youtube"
        elif _host_matches(host, "instagram.com"):
            platform = "instagram"
        else:
            platform = "other"
        counts[platform]["rows"] += 1
        if str(row.get("author") or "").strip():
            counts[platform]["filled"] += 1
    return counts


def enrich_authors_existing() -> dict[str, object]:
    """Recompute author on the on-disk citations CSV and rewrite CSV + SQL."""
    started = perf_counter()
    path = output_path()
    if not path.exists():
        raise SystemExit(f"{path} does not exist. Run a citations export first.")

    print(f"Reading {path}", flush=True)
    rows: list[dict[str, object]] = list(read_csv(path))
    before = _author_fill_counts(rows)
    print(
        "Author fill before: "
        + ", ".join(
            f"{name} {stats['filled']}/{stats['rows']}" for name, stats in before.items()
        ),
        flush=True,
    )

    rows = apply_authors(rows)
    after = _author_fill_counts(rows)
    print(
        "Author fill after: "
        + ", ".join(
            f"{name} {stats['filled']}/{stats['rows']}" for name, stats in after.items()
        ),
        flush=True,
    )

    rows = apply_category_rules(rows)
    rows.sort(key=sort_key)
    destination = str(
        write_csv_and_sql(rows, path=path, fieldnames=FIELDNAMES)
    )
    write_dims()
    minutes = (perf_counter() - started) / 60
    print(f"Author enrichment written to {destination}: {len(rows)} rows")
    print(f"Runtime: {minutes:.1f} minutes")
    return {
        "table": "fact_raw_citations",
        "rows": len(rows),
        "minutes": round(minutes, 2),
        "before": before,
        "after": after,
    }


def main(argv: list[str] | None = None) -> None:
    """Export raw citations in full or incremental mode for one or more countries."""
    args = parse_args(argv)
    if args.fresh and not args.full:
        raise SystemExit("--fresh can only be used with --full.")
    if args.enrich_authors and (args.full or args.incremental or args.fresh):
        raise SystemExit("--enrich-authors cannot be combined with pull flags.")

    for country in countries_from_args(args):
        select_country(country.slug)
        print(f"Citations: {country.name}", flush=True)
        if args.enrich_authors:
            enrich_authors_existing()
            continue
        mode = "full" if args.full else ("incremental" if args.incremental else None)
        export(mode=mode, fresh=args.fresh)
    # Author enrichment rewrites the combined CSV in place. A pull writes
    # data/{slug}/ first, so those folders are merged only after every category.
    if not args.enrich_authors:
        combine_category_exports()


def main_full() -> None:
    """Entry point for the original full backfill."""
    main(["--full"])


if __name__ == "__main__":
    main()
