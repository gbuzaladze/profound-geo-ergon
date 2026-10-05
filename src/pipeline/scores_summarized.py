"""Export the ranked visibility cube for the configured score grain.

Visibility, share of voice, and average position come from one summarized
visibility query. Positive sentiment is a second v2 pull: that report requires
an `asset` name, so only tracked category brands can be scored. Sentiment is
left-joined onto the visibility cube; missing cells stay blank (not 0).
"""

from __future__ import annotations

import argparse
from time import perf_counter

import profound
from profound import Profound

from pipeline.combine import combine_category_exports
from pipeline.common import call_api, profound_client, ref_name, write_csv_and_sql
from pipeline.config import (
    START_DATE,
    TOP_N,
    add_country_option,
    category_id,
    countries_from_args,
    data_dir,
    drop_incomplete_dates,
    end_date,
    is_owned_name,
    lookback_start,
    owned_asset,
    owned_asset_names,
    select_country,
)
from pipeline.db import dedupe_score_rows
from pipeline.dims import write_dims

# Summarized visibility pages are smaller than prompt lists.
PAGE_SIZE = 50
# Visibility ranking metrics. Sentiment is ranked separately (different report).
RANK_METRICS: tuple[str, ...] = (
    "visibility_score",
    "share_of_voice",
    "average_position",
)
# Warehouse columns before asset. Region is the country inside one category.
SCORE_DIMENSIONS = ("date", "region", "topic", "platform")
# Profound names the LLM "model"; the warehouse column is platform.
VISIBILITY_GROUP_BY = ("date", "model", "topic", "region")
# Sentiment accepts date plus two other dimensions, so topic is a filter.
SENTIMENT_GROUP_BY = ("date", "region", "model")
SENTIMENT_FILTERS_TOPIC = True
FIELDNAMES = [
    *SCORE_DIMENSIONS,
    "asset",
    "is_owned",
    "visibility",
    "share_of_voice",
    "average_position",
    "positive_sentiment",
]


# Row helpers


def flatten_record(record: object) -> dict[str, object]:
    """Map one summarized visibility record to export columns."""
    asset = getattr(record, "asset", None)
    name = ref_name(asset)
    row = {
        "date": getattr(record, "date", None),
        "topic": ref_name(getattr(record, "topic", None)),
        # Profound calls the LLM "model"; the warehouse column is platform.
        "platform": ref_name(getattr(record, "model", None)),
        "asset": name,
        "is_owned": bool(getattr(asset, "owned", False)) or is_owned_name(name),
        "visibility": getattr(record, "visibility_score", None),
        "share_of_voice": getattr(record, "share_of_voice", None),
        "average_position": getattr(record, "average_position", None),
        "positive_sentiment": None,
    }
    if "region" in SCORE_DIMENSIONS:
        row["region"] = ref_name(getattr(record, "region", None))
    return row


def score_key(row: dict[str, object]) -> tuple[str, ...]:
    """Grain key used for joins, fills, and sort. Asset is last."""
    return tuple(str(row.get(column) or "") for column in (*SCORE_DIMENSIONS, "asset"))


def include_asset(
    names: list[str],
    owned_by_asset: dict[str, bool],
    name: str,
    owned: bool,
) -> bool:
    """Add a ranked brand to the union. Returns True when the name is new."""
    if name in owned_by_asset:
        owned_by_asset[name] = owned_by_asset[name] or owned
        return False
    names.append(name)
    owned_by_asset[name] = owned
    return True


def ensure_owned_assets(
    names: list[str],
    owned_by_asset: dict[str, bool],
    category_assets: list[tuple[str, bool]],
) -> None:
    """Include the country's owned asset, plus aliases Profound tracks here.

    The country's `owned_asset` is always kept, even outside every top N.
    An alias is added only when this category lists that exact asset, so a
    generic name is not zero-filled beside the local brand.
    """
    by_key = {name.casefold(): name for name, _owned in category_assets}
    present = {name.casefold(): name for name in owned_by_asset}
    primary = owned_asset().casefold()
    for configured in owned_asset_names():
        folded = configured.casefold()
        tracked = by_key.get(folded)
        if tracked is None and folded != primary:
            print(
                f"{configured} is not a tracked asset in this category; "
                "not adding an empty owned series."
            )
            continue
        canonical = tracked if tracked is not None else configured
        existing = present.get(folded)
        if existing is None:
            names.append(canonical)
            owned_by_asset[canonical] = True
            present[folded] = canonical
            print(
                f"{canonical} outside every top {TOP_N}; "
                f"including as asset #{len(names)} (owned)."
            )
            continue
        owned_by_asset[existing] = True


def zero_fill(
    rows: list[dict[str, object]],
    *,
    assets: list[str],
    owned_by_asset: dict[str, bool],
) -> list[dict[str, object]]:
    """Ensure every selected asset has a row for each SCORE_DIMENSIONS slice.

    Missing visibility and share_of_voice are set to 0. average_position and
    positive_sentiment stay blank when the asset was not present.

    Mutates owned_by_asset in place: an asset is owned if any series row says so.
    """
    existing: dict[tuple[str, ...], dict[str, object]] = {}
    slices: set[tuple[str, ...]] = set()

    for row in rows:
        key = score_key(row)
        if not all(key):
            continue

        existing[key] = row
        slices.add(key[:-1])
        asset = key[-1]
        if asset in owned_by_asset:
            owned_by_asset[asset] = (
                bool(row.get("is_owned"))
                or owned_by_asset[asset]
                or is_owned_name(asset)
            )
            row["is_owned"] = owned_by_asset[asset]

    filled: list[dict[str, object]] = []
    for slice_key in slices:
        context = dict(zip(SCORE_DIMENSIONS, slice_key, strict=True))
        for asset in assets:
            row = existing.get((*slice_key, asset))
            if row is not None:
                filled.append(row)
                continue
            filled.append(
                {
                    **context,
                    "asset": asset,
                    "is_owned": owned_by_asset.get(asset, False),
                    "visibility": 0.0,
                    "share_of_voice": 0.0,
                    "average_position": None,
                    "positive_sentiment": None,
                }
            )
    return filled


# Ranking


def _sort_dir(metric: str) -> str:
    """Return Profound sort direction. Average position is lower-is-better."""
    return "asc" if metric == "average_position" else "desc"


def list_topic_names(client: Profound) -> tuple[list[str], int]:
    """Return active topic names and the API-call count used to list them."""
    response = call_api(client.organizations.categories.topics, category_id())
    names = sorted(
        item.name
        for item in response
        if item.name and getattr(item, "status", "active") == "active"
    )
    if not names:
        raise SystemExit("No active topics were returned for this category.")
    return names, 1


def rank_metric(
    client: Profound,
    *,
    metric: str,
    rank_start: str,
    scan_end: str,
    topic: str | None = None,
) -> tuple[list[tuple[str, bool]], int]:
    """Return up to TOP_N (name, owned) pairs ranked by one metric."""
    ranked: list[tuple[str, bool]] = []
    seen: set[str] = set()
    cursor: str | None = None
    api_calls = 0
    topic_filter = {"field": "topic", "op": "is", "value": topic} if topic else None

    while len(ranked) < TOP_N:
        api_calls += 1
        page_limit = min(PAGE_SIZE, TOP_N - len(ranked))
        response = call_api(
            client.reports.query_visibility,
            category_id=category_id(),
            start_date=rank_start,
            end_date=scan_end,
            metrics=[metric],
            scope="all",
            filter=topic_filter,
            limit=page_limit,
            cursor=cursor,
            extra_body={"sort": {"field": metric, "dir": _sort_dir(metric)}},
        )
        page = list(response.data)
        if not page:
            break
        for record in page:
            asset = record.asset
            name = getattr(asset, "name", None)
            if not name or name in seen:
                continue
            seen.add(name)
            ranked.append(
                (name, bool(getattr(asset, "owned", False)) or is_owned_name(name))
            )
            if len(ranked) >= TOP_N:
                break
        cursor = response.info.next_cursor
        if not cursor:
            break

    return ranked, api_calls


def list_category_assets(client: Profound) -> tuple[list[tuple[str, bool]], int]:
    """Return tracked category assets as (name, owned) pairs."""
    items = call_api(client.organizations.categories.assets, category_id())
    assets = [
        (item.name, bool(getattr(item, "is_owned", False)) or is_owned_name(item.name))
        for item in items
        if getattr(item, "name", None)
    ]
    if not assets:
        raise SystemExit("No category assets were returned.")
    return assets, 1


def sentiment_share(value: object) -> float | None:
    """Return positive sentiment as a 0-1 share, matching visibility.

    Profound v2 sentiment is a 0-100 percent; convert so the CSV and
    dashboard stay on the same scale as visibility and share of voice.
    """
    if value is None or value == "":
        return None
    share = float(value)
    if share < 0:
        return None
    if share > 1:
        share /= 100
    return share


def _sentiment_key(
    record: object, asset: str, topic: str | None
) -> tuple[str, ...] | None:
    """Map one sentiment record onto the scores grain. None if a part is missing.

    When topic is passed, it came from a filter: the response is not grouped
    by topic, so the record itself has no topic name.
    """
    row = {
        "date": str(getattr(record, "date", "") or ""),
        "region": ref_name(getattr(record, "region", None)),
        "topic": (
            topic
            if topic is not None
            else ref_name(getattr(record, "topic", None))
        ),
        "platform": ref_name(getattr(record, "model", None)),
        "asset": asset,
    }
    key = score_key(row)
    if not all(key):
        return None
    return key


def _sentiment_pages(
    client: Profound,
    *,
    asset: str,
    start: str,
    scan_end: str,
    group_by: list[str],
    topic: str | None = None,
) -> tuple[list[object], int]:
    """Page one v2 sentiment query. 400/404/422 means the brand is untracked.

    Sentiment accepts at most two group_by dimensions besides date. When the
    series grain needs another dimension, pass topic as a filter instead.
    """
    rows: list[object] = []
    cursor: str | None = None
    api_calls = 0
    grouped_by_date = "date" in group_by
    while True:
        api_calls += 1
        kwargs: dict[str, object] = {
            "asset": asset,
            "category_id": category_id(),
            "start_date": start,
            "end_date": scan_end,
            "metrics": ["positive_sentiment"],
            "group_by": group_by,
            "limit": PAGE_SIZE,
        }
        if grouped_by_date:
            # interval is only valid when the query is grouped by date.
            kwargs["interval"] = "day"
        if topic:
            kwargs["filter"] = {"field": "topic", "op": "is", "value": topic}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            response = call_api(client.reports.query_sentiment, **kwargs)
        except profound.APIStatusError as error:
            if error.status_code in {400, 404, 422}:
                print(f"  {asset}: no sentiment ({error.status_code})")
                return [], api_calls
            raise
        page = list(response.data)
        if not page:
            break
        rows.extend(page)
        cursor = response.info.next_cursor
        if not cursor:
            break
    return rows, api_calls


def union_sentiment_ranks(
    client: Profound,
    *,
    names: list[str],
    owned_by_asset: dict[str, bool],
    topics: list[str],
    rank_start: str,
    scan_end: str,
    category_assets: list[tuple[str, bool]],
) -> int:
    """Union top-N category brands by positive sentiment in each topic.

    v2 sentiment cannot rank every mentioned brand; only category assets work.
    """
    by_topic: dict[str, dict[str, tuple[float, bool]]] = {topic: {} for topic in topics}
    api_calls = 0
    for name, owned in category_assets:
        records, calls = _sentiment_pages(
            client,
            asset=name,
            start=rank_start,
            scan_end=scan_end,
            group_by=["topic"],
        )
        api_calls += calls
        for record in records:
            topic = ref_name(getattr(record, "topic", None))
            share = sentiment_share(getattr(record, "positive_sentiment", None))
            if not topic or topic not in by_topic or share is None:
                continue
            by_topic[topic][name] = (share, owned)

    added = 0
    for scored in by_topic.values():
        ranked = sorted(scored.items(), key=lambda item: item[1][0], reverse=True)
        for name, (_share, owned) in ranked[:TOP_N]:
            if include_asset(names, owned_by_asset, name, owned):
                added += 1
    if added:
        print(f"Sentiment rank added {added} brands")
    return api_calls


def fetch_sentiment(
    client: Profound,
    *,
    assets: list[str],
    scan_end: str,
) -> tuple[dict[tuple[str, ...], float], int]:
    """Return positive sentiment keyed by the scores grain.

    Rows for slices that never appear in visibility are dropped on join
    (see attach_sentiment).
    """
    values: dict[tuple[str, ...], float] = {}
    api_calls = 0
    # Sentiment's two non-date dimensions are already used when the grain
    # includes region, so topic is applied as a filter instead.
    if SENTIMENT_FILTERS_TOPIC:
        topics, topic_calls = list_topic_names(client)
        api_calls += topic_calls
        topic_filters: list[str | None] = list(topics)
    else:
        topic_filters = [None]
    for asset in assets:
        row_count = 0
        for topic in topic_filters:
            records, calls = _sentiment_pages(
                client,
                asset=asset,
                start=START_DATE,
                scan_end=scan_end,
                group_by=list(SENTIMENT_GROUP_BY),
                topic=topic,
            )
            api_calls += calls
            for record in records:
                share = sentiment_share(getattr(record, "positive_sentiment", None))
                key = _sentiment_key(record, asset, topic)
                if key is None or share is None:
                    continue
                values[key] = share
                row_count += 1
        print(f"  sentiment {asset}: {row_count} rows")
    return values, api_calls


def attach_sentiment(
    rows: list[dict[str, object]],
    sentiment: dict[tuple[str, ...], float],
) -> int:
    """Left-join sentiment onto score rows. Unmatched keys stay blank."""
    matched = 0
    for row in rows:
        share = sentiment.get(score_key(row))
        if share is None:
            continue
        row["positive_sentiment"] = share
        matched += 1
    return matched


def select_assets(
    client: Profound, *, scan_end: str
) -> tuple[list[str], dict[str, bool], list[str], int]:
    """Union top-N per visibility metric and per sentiment, plus owned."""
    rank_start = lookback_start()
    owned_by_asset: dict[str, bool] = {}
    names: list[str] = []
    topics, topic_calls = list_topic_names(client)
    category_assets, asset_calls = list_category_assets(client)
    api_calls = topic_calls + asset_calls
    print(f"Topics ({len(topics)}): {', '.join(topics)}")
    print(f"Category assets ({len(category_assets)})")

    for topic in topics:
        before = len(names)
        for metric in RANK_METRICS:
            ranked, calls = rank_metric(
                client,
                metric=metric,
                rank_start=rank_start,
                scan_end=scan_end,
                topic=topic,
            )
            api_calls += calls
            for name, owned in ranked:
                include_asset(names, owned_by_asset, name, owned)
        print(f"  {topic}: +{len(names) - before} new ({len(names)} unique so far)")

    api_calls += union_sentiment_ranks(
        client,
        names=names,
        owned_by_asset=owned_by_asset,
        topics=topics,
        rank_start=rank_start,
        scan_end=scan_end,
        category_assets=category_assets,
    )

    ensure_owned_assets(names, owned_by_asset, category_assets)

    if not names:
        raise SystemExit(
            "No ranked or owned assets were returned for the lookback window."
        )

    print(
        f"Union: {len(names)} brands "
        f"(top {TOP_N} by visibility, share of voice, average position, "
        "and positive sentiment in each topic, plus owned)"
    )
    selected_keys = {name.casefold() for name in owned_by_asset}
    sentiment_assets = [
        name for name, _owned in category_assets if name.casefold() in selected_keys
    ]
    return names, owned_by_asset, sentiment_assets, api_calls


# Series export


def fetch_summarized(
    client: Profound,
    *,
    assets: list[str],
    scan_end: str,
) -> tuple[list[dict[str, object]], int]:
    """Page through visibility rows for the selected assets."""
    rows: list[dict[str, object]] = []
    cursor: str | None = None
    pages = 0
    api_calls = 0

    while True:
        api_calls += 1
        response = call_api(
            client.reports.query_visibility,
            category_id=category_id(),
            start_date=START_DATE,
            end_date=scan_end,
            assets=assets,
            metrics=list(RANK_METRICS),
            group_by=list(VISIBILITY_GROUP_BY),
            interval="day",
            scope="all",
            limit=PAGE_SIZE,
            cursor=cursor,
        )
        page = list(response.data)
        if not page:
            break

        rows.extend(flatten_record(record) for record in page)
        pages += 1
        cursor = response.info.next_cursor
        print(f"Fetched page {pages}: {len(rows)} summarized rows")
        if not cursor:
            break

    return rows, api_calls


def export(
    *, csv_output: bool = True, rebuild_dimensions: bool = True
) -> dict[str, object]:
    """Build scores and replace SQL, optionally retaining the local CSV output."""
    started = perf_counter()
    scan_end = end_date()

    with profound_client() as client:
        assets, owned_by_asset, sentiment_assets, rank_calls = select_assets(
            client, scan_end=scan_end
        )
        rows, series_calls = fetch_summarized(client, assets=assets, scan_end=scan_end)
        print(f"Sentiment series for {len(sentiment_assets)} category brands")
        sentiment, sentiment_calls = fetch_sentiment(
            client, assets=sentiment_assets, scan_end=scan_end
        )
    api_calls = rank_calls + series_calls + sentiment_calls
    rows = drop_incomplete_dates(rows)

    if not rows:
        raise SystemExit("No summarized score rows were returned.")

    before_fill = len(rows)
    rows = zero_fill(rows, assets=assets, owned_by_asset=owned_by_asset)
    print(f"Zero-filled missing asset rows: {before_fill} -> {len(rows)}")
    matched = attach_sentiment(rows, sentiment)
    print(f"Attached positive_sentiment on {matched} of {len(rows)} rows")

    # Profound can return the same brand twice with different casing.
    # Collapse before the CSV write so the file matches Azure SQL's CI primary key.
    before_collapse = len(rows)
    rows = dedupe_score_rows(rows)
    if len(rows) != before_collapse:
        print(f"Collapsed case-variant assets: {before_collapse} -> {len(rows)} rows")

    rows.sort(key=score_key)
    if csv_output:
        destination = str(
            write_csv_and_sql(
                rows,
                path=data_dir() / "fact_scores_summarized.csv",
                fieldnames=FIELDNAMES,
            )
        )
    else:
        from pipeline.db import replace_table

        replace_table("fact_scores_summarized", rows, FIELDNAMES)
        destination = "Azure SQL"
    if rebuild_dimensions:
        if csv_output:
            write_dims()
        else:
            from pipeline.db import rebuild_dimensions_from_sql

            rebuild_dimensions_from_sql()

    dates = sorted({str(row["date"]) for row in rows if row.get("date")})
    minutes = (perf_counter() - started) / 60
    print(f"Scores summarized table written to {destination}: {len(rows)} rows")
    print(f"Runtime: {minutes:.1f} minutes; API calls: {api_calls}")
    print(f"Assets ({len(assets)})")
    print(f"Rank window: {lookback_start()} through {scan_end}")
    print(f"Series window: {START_DATE} through {scan_end}")
    if dates:
        print(f"Available data range: {dates[0]} through {dates[-1]}")
    print("Grouped by: " + ", ".join((*SCORE_DIMENSIONS, "asset")))
    return {
        "table": "fact_scores_summarized",
        "rows": len(rows),
        "api_calls": api_calls,
        "minutes": round(minutes, 2),
    }


def main(argv: list[str] | None = None) -> None:
    """Export summarized scores for one or more countries."""
    parser = argparse.ArgumentParser(
        description="Export Profound summarized visibility scores."
    )
    add_country_option(parser)
    args = parser.parse_args(argv)
    for country in countries_from_args(args):
        select_country(country.slug)
        print(f"Scores: {country.name}", flush=True)
        export()
    combine_category_exports()


if __name__ == "__main__":
    main()
