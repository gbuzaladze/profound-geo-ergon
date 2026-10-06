"""Load pipeline tables into the single dbo schema.

Both Profound categories write the same dbo tables. A category export replaces
only the rows for its own regions; the other category stays. Dimension tables
are rebuilt from both facts. `load_date` is America/Toronto wall time
(DATETIME2, no offset). Citations have no primary key (duplicate rows are
valid). Scores collapse case-variant asset names before insert because Azure
SQL collation is case-insensitive.

    python -m pipeline.db              # load combined data/*.csv into dbo
    python -m pipeline.db --ensure-only
"""

from __future__ import annotations

import argparse
import csv
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from mssql_python import connect

from pipeline.config import active_country, project_root

# Bulk copy: 5k rows per TDS batch; 1 hour covers the large citation CSVs.
_BULK_BATCH_SIZE = 5000
_BULK_TIMEOUT_SECONDS = 3600
_CONNECT_TIMEOUT_SECONDS = 60

# Set by --skip-db so exporters can still write CSVs without Azure credentials.
_SKIP_ENV = "AZURE_SQL_SKIP"
_SQL_REQUIRED_ENV = (
    "AZURE_SQL_SERVER",
    "AZURE_SQL_DATABASE",
)
_SQL_PASSWORD_ENV = (
    "AZURE_SQL_USERNAME",
    "AZURE_SQL_PASSWORD",
)
_FLOAT_COLUMNS = frozenset(
    {
        "visibility",
        "share_of_voice",
        "average_position",
        "positive_sentiment",
    }
)

_SCORES_TABLE = "fact_scores_summarized"
# Both categories share dbo. Country is the region column, not a schema.
SQL_SCHEMA = "dbo"
# Category exports replace their own slice. Dimension tables are a full rebuild.
_SLICE_TABLES = frozenset({_SCORES_TABLE, "fact_raw_citations", "dim_prompt"})
LOAD_DATE_COLUMN = "load_date"
_LOAD_DATE_DDL = (LOAD_DATE_COLUMN, "DATETIME2 NOT NULL")
LOAD_TZ = ZoneInfo("America/Toronto")

# DDL used when a table is missing. Existing tables are left in place and
# only widened (see _WIDEN_TO_MAX) so CSV URLs/paths are not truncated.
# load_date is America/Toronto wall time; it is not part of any primary key.
TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    _SCORES_TABLE: (
        ("date", "DATE NOT NULL"),
        ("region", "NVARCHAR(200) NOT NULL"),
        ("topic", "NVARCHAR(200) NOT NULL"),
        ("platform", "NVARCHAR(200) NOT NULL"),
        ("asset", "NVARCHAR(200) NOT NULL"),
        ("is_owned", "BIT NOT NULL"),
        ("visibility", "DECIMAL(38, 16) NOT NULL"),
        ("share_of_voice", "DECIMAL(38, 16) NOT NULL"),
        ("average_position", "DECIMAL(38, 16) NULL"),
        ("positive_sentiment", "DECIMAL(38, 16) NULL"),
        _LOAD_DATE_DDL,
    ),
    "fact_raw_citations": (
        ("date", "DATE NOT NULL"),
        ("topic", "NVARCHAR(200) NOT NULL"),
        ("platform", "NVARCHAR(200) NOT NULL"),
        ("category", "NVARCHAR(200) NULL"),
        ("subcategory", "NVARCHAR(200) NULL"),
        ("pag", "BIT NULL"),
        ("mentioned", "NVARCHAR(MAX) NULL"),
        ("url", "NVARCHAR(MAX) NULL"),
        ("hostname", "NVARCHAR(200) NULL"),
        ("domain", "NVARCHAR(200) NULL"),
        ("path", "NVARCHAR(MAX) NULL"),
        ("author", "NVARCHAR(200) NULL"),
        ("tags", "NVARCHAR(MAX) NULL"),
        ("region", "NVARCHAR(200) NULL"),
        _LOAD_DATE_DDL,
    ),
    "dim_prompt": (
        ("prompt", "NVARCHAR(MAX) NOT NULL"),
        ("topic", "NVARCHAR(200) NOT NULL"),
        ("tags", "NVARCHAR(MAX) NULL"),
        ("regions", "NVARCHAR(MAX) NULL"),
        ("platforms", "NVARCHAR(MAX) NULL"),
        _LOAD_DATE_DDL,
    ),
    "dim_date": (("date", "DATE NOT NULL"), _LOAD_DATE_DDL),
    "dim_topic": (("topic", "NVARCHAR(200) NOT NULL"), _LOAD_DATE_DDL),
    "dim_platform": (("platform", "NVARCHAR(200) NOT NULL"), _LOAD_DATE_DDL),
    "dim_region": (("region", "NVARCHAR(200) NOT NULL"), _LOAD_DATE_DDL),
}

# Text grain columns are matched case-insensitively. date is not folded.
# Region is the country inside a multi-country Profound category.
_SCORE_GRAIN = ("date", "region", "topic", "platform", "asset")
# Slicer dims are dim_{column}, except dim_prompt which is a prompt snapshot.
_SLICER_DIMENSIONS = {
    table: table.removeprefix("dim_")
    for table in TABLES
    if table.startswith("dim_") and table != "dim_prompt"
}
_PRIMARY_KEYS = {table: (column,) for table, column in _SLICER_DIMENSIONS.items()}
_PRIMARY_KEYS[_SCORES_TABLE] = _SCORE_GRAIN
_DATE_INDEXED = (_SCORES_TABLE, "fact_raw_citations")
# Existing databases were created with NVARCHAR(200/2000). Widen to MAX so
# long citation URLs and prompt text are not truncated.
_WIDEN_TO_MAX = {
    "fact_raw_citations": (
        ("mentioned", True),
        ("url", True),
        ("path", True),
        ("tags", True),
    ),
    "dim_prompt": (
        ("prompt", False),
        ("tags", True),
        ("regions", True),
        ("platforms", True),
    ),
}
_NOT_NULL_COLUMNS = {
    table: frozenset(name for name, ddl in columns if "NOT NULL" in ddl)
    for table, columns in TABLES.items()
}

_skip_notice_shown = False
_enabled_notice_shown = False


# Connection


def skip_sql() -> bool:
    """True when AZURE_SQL_SKIP is set (CSV-only run)."""
    return os.getenv(_SKIP_ENV, "").strip() in {"1", "true", "yes"}


def set_skip_sql(skip: bool) -> None:
    """Honor --skip-db by setting AZURE_SQL_SKIP for this process."""
    if skip:
        os.environ[_SKIP_ENV] = "1"
    else:
        os.environ.pop(_SKIP_ENV, None)


def _sql_env() -> dict[str, str] | None:
    """Return Azure SQL settings, or None when SQL is unused.

    Server and database are always required. Username/password remain supported
    for local CLI runs; when both are omitted, Azure managed identity is used.
    """
    load_dotenv(project_root() / ".env")
    keys = _SQL_REQUIRED_ENV + _SQL_PASSWORD_ENV
    values = {key: (os.getenv(key) or "").strip() for key in keys}
    required_present = [key for key in _SQL_REQUIRED_ENV if values[key]]
    if not required_present and not any(values[key] for key in _SQL_PASSWORD_ENV):
        return None
    required_missing = [key for key in _SQL_REQUIRED_ENV if not values[key]]
    if required_missing:
        joined = ", ".join(required_missing)
        raise SystemExit(
            f"Azure SQL configuration is missing {joined}. Set both "
            "AZURE_SQL_SERVER and AZURE_SQL_DATABASE."
        )
    password_present = [key for key in _SQL_PASSWORD_ENV if values[key]]
    if len(password_present) == 1:
        missing = next(key for key in _SQL_PASSWORD_ENV if not values[key])
        raise SystemExit(
            f"Azure SQL configuration is missing {missing}. Set both SQL login "
            "values, or omit both to use managed identity."
        )
    return values


def _qualified(schema: str, table: str) -> str:
    """Bracketed `[schema].[table]` identifier for Azure SQL."""
    return f"[{schema}].[{table}]"


def _connect(settings: dict[str, str]):
    """Open an encrypted Azure SQL connection.

    Local SQL login credentials remain supported. Azure-hosted runs omit them
    and authenticate with the Function App's managed identity.
    """
    if not settings["AZURE_SQL_USERNAME"]:
        client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip()
        identity = f";User Id={client_id}" if client_id else ""
        return connect(
            "Server="
            f"{settings['AZURE_SQL_SERVER']};"
            f"Database={settings['AZURE_SQL_DATABASE']};"
            "Encrypt=yes;TrustServerCertificate=no;"
            "Connection Timeout=60;Authentication=ActiveDirectoryMSI"
            f"{identity}"
        )
    return connect(
        server=settings["AZURE_SQL_SERVER"],
        database=settings["AZURE_SQL_DATABASE"],
        uid=settings["AZURE_SQL_USERNAME"],
        pwd=settings["AZURE_SQL_PASSWORD"],
        encrypt="yes",
        trust_server_certificate="no",
        multisubnetfailover="yes",
        timeout=_CONNECT_TIMEOUT_SECONDS,
    )


@contextmanager
def sql_connection():
    """Yield an Azure SQL connection, or skip when SQL is not configured."""
    if skip_sql():
        yield None
        return
    settings = _sql_env()
    if settings is None:
        yield None
        return
    conn = _connect(settings)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Schema


def _table_column_sql(table: str) -> str:
    """Column definitions for CREATE TABLE, in TABLES order."""
    return ", ".join(f"[{name}] {ddl}" for name, ddl in TABLES[table])


def ensure_schema_and_tables(cursor, schema: str) -> None:
    """Create the schema, if needed, and the reporting tables when they are missing.

    Existing tables are not rebuilt, except `fact_raw_citations` when
    `subcategory` and `pag` are missing or not immediately after `category`.
    Narrow text columns are widened in place, and `load_date` is added when
    absent.
    """
    # CREATE SCHEMA must be its own batch; run it only when the schema is new.
    cursor.execute("SELECT 1 FROM sys.schemas WHERE name = ?", (schema,))
    if cursor.fetchone() is None:
        cursor.execute(f"CREATE SCHEMA [{schema}]")
    for table in TABLES:
        col_sql = _table_column_sql(table)
        pk = _PRIMARY_KEYS.get(table)
        pk_sql = ""
        if pk:
            pk_cols = ", ".join(f"[{name}]" for name in pk)
            pk_sql = f", CONSTRAINT [PK_{table}] PRIMARY KEY ({pk_cols})"
        cursor.execute(
            f"""
            IF OBJECT_ID(N'{schema}.{table}', N'U') IS NULL
            CREATE TABLE {_qualified(schema, table)} (
                {col_sql}{pk_sql}
            )
            """
        )
        _widen_existing_text_columns(cursor, schema, table)
        _ensure_load_date_column(cursor, schema, table)
        if table == "fact_raw_citations":
            _ensure_citation_class_columns(cursor, schema)
        if table in _DATE_INDEXED:
            # Nonclustered date index for Power BI / slicer filters on large facts.
            index_name = f"IX_{table}_date"
            cursor.execute(
                f"""
                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = ? AND object_id = OBJECT_ID(N'{schema}.{table}')
                )
                CREATE INDEX [{index_name}] ON {_qualified(schema, table)} ([date])
                """,
                (index_name,),
            )


def _column_max_length(cursor, schema: str, table: str, column: str) -> int | None:
    """Return sys.columns.max_length, or None if the column is missing."""
    cursor.execute(
        """
        SELECT c.max_length
        FROM sys.columns AS c
        JOIN sys.tables AS t ON t.object_id = c.object_id
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ? AND c.name = ?
        """,
        (schema, table, column),
    )
    row = cursor.fetchone()
    return None if row is None else int(row[0])


def _widen_existing_text_columns(cursor, schema: str, table: str) -> None:
    """Widen citation/prompt text columns that were created narrower than the CSVs."""
    for column, nullable in _WIDEN_TO_MAX.get(table, ()):
        length = _column_max_length(cursor, schema, table, column)
        # -1 is NVARCHAR(MAX); missing columns are skipped.
        if length is None or length == -1:
            continue
        null_sql = "NULL" if nullable else "NOT NULL"
        cursor.execute(
            f"""
            ALTER TABLE {_qualified(schema, table)}
            ALTER COLUMN [{column}] NVARCHAR(MAX) {null_sql}
            """
        )


def _ensure_load_date_column(cursor, schema: str, table: str) -> None:
    """Add load_date to existing tables. Nullable until the next full replace."""
    if _column_max_length(cursor, schema, table, LOAD_DATE_COLUMN) is not None:
        return
    cursor.execute(
        f"ALTER TABLE {_qualified(schema, table)} ADD [{LOAD_DATE_COLUMN}] DATETIME2 NULL"
    )


def _table_column_names(cursor, schema: str, table: str) -> list[str]:
    """Return column names in CREATE TABLE order."""
    cursor.execute(
        """
        SELECT c.name
        FROM sys.columns AS c
        JOIN sys.tables AS t ON t.object_id = c.object_id
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
        ORDER BY c.column_id
        """,
        (schema, table),
    )
    return [str(row[0]) for row in cursor.fetchall()]


def _columns_match(actual: list[str], desired: list[str]) -> bool:
    """True when two column lists are the same names in the same order."""
    return [name.casefold() for name in actual] == [name.casefold() for name in desired]


def _stage_reordered_table(cursor, schema: str, table: str) -> tuple[str, str]:
    """Create an empty copy of `table` in the current column order.

    Returns the staging name and its qualified identifier. The caller copies
    rows, then `_swap_reordered_table` replaces the original.
    """
    staging = f"{table}__reorder"
    qualified_stage = _qualified(schema, staging)
    cursor.execute(
        f"IF OBJECT_ID(N'{schema}.{staging}', N'U') IS NOT NULL "
        f"DROP TABLE {qualified_stage}"
    )
    cursor.execute(f"CREATE TABLE {qualified_stage} ({_table_column_sql(table)})")
    return staging, qualified_stage


def _swap_reordered_table(cursor, schema: str, table: str, staging: str) -> None:
    """Drop the original table and rename the staged copy into its place."""
    cursor.execute(f"DROP TABLE {_qualified(schema, table)}")
    cursor.execute(f"EXEC sp_rename N'{schema}.{staging}', N'{table}'")


def _ensure_citation_class_columns(cursor, schema: str) -> None:
    """Put subcategory and pag immediately after category on an existing table.

    SQL Server appends new columns, so a table created before those columns
    is copied into a new table with the current column order. Rows keep their
    existing values; the new columns stay null until the next citation load.
    """
    table = "fact_raw_citations"
    desired = _columns(table)
    actual = _table_column_names(cursor, schema, table)
    if _columns_match(actual, desired):
        return
    staging, qualified_stage = _stage_reordered_table(cursor, schema, table)
    present = {name.casefold() for name in actual}
    shared = [name for name in desired if name.casefold() in present]
    if shared:
        cols = ", ".join(f"[{name}]" for name in shared)
        cursor.execute(
            f"INSERT INTO {qualified_stage} ({cols}) "
            f"SELECT {cols} FROM {_qualified(schema, table)}"
        )
    _swap_reordered_table(cursor, schema, table, staging)


# Value conversion


def _as_bool(value: object) -> bool:
    """Treat CSV `true`/`1`/`yes` (any case) as True; everything else False."""
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "1", "yes"}


def _as_optional_bool(value: object) -> bool | None:
    """Parse TRUE/FALSE. Blank stays NULL so an unmatched domain is not FALSE."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _as_float(value: object) -> float | None:
    """Parse a numeric cell; blank CSV values become SQL NULL."""
    if value is None or value == "":
        return None
    return float(value)


def _as_date(value: object) -> date | None:
    """Normalize CSV strings and Python dates to datetime.date.

    ISO timestamps are truncated to YYYY-MM-DD so `2026-09-10T00:00:00` still
    loads as a DATE.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _as_text(value: object) -> str | None:
    """Stringify a cell; empty strings become SQL NULL."""
    if value is None:
        return None
    text = str(value)
    return text if text else None


def load_now() -> datetime:
    """Current America/Toronto wall time, naive, for DATETIME2.

    DATETIME2 does not store a timezone offset, so tzinfo is stripped after
    converting. DST is handled by ZoneInfo (EDT vs EST).
    """
    return datetime.now(LOAD_TZ).replace(tzinfo=None)


def stamp_load_date(
    rows: list[dict[str, Any]], *, when: datetime | None = None
) -> list[dict[str, Any]]:
    """Copy rows and set the same Toronto `load_date` on every row.

    One timestamp per table replace, so CSV and SQL can be compared by load.
    """
    stamped_at = when or load_now()
    return [{**row, LOAD_DATE_COLUMN: stamped_at} for row in rows]


def _columns(table: str) -> list[str]:
    """SQL column names in CREATE TABLE order, including load_date."""
    return [name for name, _ddl in TABLES[table]]


def _grain_columns(table: str) -> list[str]:
    """Business keys/metrics only; load_date is a load stamp, not grain."""
    return [name for name in _columns(table) if name != LOAD_DATE_COLUMN]


def _to_naive_toronto(value: datetime) -> datetime:
    """Drop tzinfo after converting aware values into America/Toronto."""
    if value.tzinfo is None:
        return value
    return value.astimezone(LOAD_TZ).replace(tzinfo=None)


def _as_datetime(value: object) -> datetime | None:
    """Parse a load_date cell into naive Toronto time.

    Trailing Z is treated as UTC, then converted. Naive ISO strings are kept
    as-is (already Toronto wall time from this pipeline).
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _to_naive_toronto(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _to_naive_toronto(datetime.fromisoformat(text))


def convert_value(column: str, value: object, *, table: str) -> object:
    """Coerce a CSV or in-memory cell to the SQL column type.

    NOT NULL numeric cells that are blank become 0; NOT NULL text becomes "".
    Missing load_date is filled with the current Toronto timestamp.
    """
    not_null = column in _NOT_NULL_COLUMNS[table]
    if column == LOAD_DATE_COLUMN:
        return _as_datetime(value) or load_now()
    if column == "is_owned":
        return _as_bool(value)
    if column == "pag":
        return _as_optional_bool(value)
    if column in _FLOAT_COLUMNS:
        number = _as_float(value)
        if number is None and not_null:
            return 0.0
        return number
    if column == "date":
        return _as_date(value)
    text = _as_text(value)
    if text is None and not_null:
        return ""
    return text


def row_tuple(
    row: dict[str, Any], fieldnames: list[str], *, table: str
) -> tuple[object, ...]:
    """Map a row dict onto the table column order."""
    return tuple(convert_value(name, row.get(name), table=table) for name in fieldnames)


# Score grain
# Azure SQL default collation is case-insensitive, so "Brand" and "brand"
# share a primary key. Collapse before insert so CSV and SQL stay aligned.


def _score_key(row: dict[str, Any]) -> tuple[str, ...]:
    """Case-insensitive scores grain matching Azure SQL CI collation."""
    parts: list[str] = []
    for column in _SCORE_GRAIN:
        value = str(row.get(column) or "").strip()
        if column != "date":
            value = value.casefold()
        parts.append(value)
    return tuple(parts)


def _asset_name_rank(name: str) -> int:
    """Prefer display casing when two spellings share one CI key.

    Lower is better: Title Case, then mixed (`AbbVie`), then ALL CAPS
    (`FDA`), then lowercase. Singleton names are left alone by
    `_prefer_asset_name` so `FDA` is not rewritten to `Fda`.
    """
    if not name:
        return 4
    letters = [char for char in name if char.isalpha()]
    rest = name[1:]
    if name[:1].isupper() and rest and rest.islower():
        return 0
    if letters and all(char.islower() for char in letters):
        return 3
    if letters and all(char.isupper() for char in letters):
        return 2
    return 1


def _prefer_asset_name(current: str, incoming: str) -> str:
    """Keep a singleton spelling as-is; if two exist, prefer Title Case."""
    if not current:
        return incoming
    if incoming and _asset_name_rank(incoming) < _asset_name_rank(current):
        return incoming
    return current


def _merge_score_row(
    current: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Union two case-variant score rows that share one SQL key."""
    merged = dict(current)
    merged["is_owned"] = _as_bool(current.get("is_owned")) or _as_bool(
        incoming.get("is_owned")
    )
    merged["asset"] = _prefer_asset_name(
        str(current.get("asset") or ""), str(incoming.get("asset") or "")
    )
    # Take the higher rate; zero-fill treats missing as 0.
    for column in ("visibility", "share_of_voice"):
        merged[column] = max(
            _as_float(current.get(column)) or 0.0,
            _as_float(incoming.get(column)) or 0.0,
        )
    # Sentiment/position stay blank unless one of the variants has a value.
    for column in ("average_position", "positive_sentiment"):
        if merged.get(column) in (None, "") and incoming.get(column) not in (None, ""):
            merged[column] = incoming.get(column)
    return merged


def dedupe_score_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse case-variant asset names onto one row per scores grain."""
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = _score_key(row)
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(row)
        else:
            merged[key] = _merge_score_row(existing, row)
    return list(merged.values())


def _collapse_scores_if_needed(
    table: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Dedupe scores only; other tables keep duplicate rows as-is."""
    if table != _SCORES_TABLE:
        return rows
    collapsed = dedupe_score_rows(rows)
    if len(collapsed) != len(rows):
        print(
            f"  {table}: collapsed {len(rows)} rows to {len(collapsed)} "
            "unique grains (case-insensitive asset names)",
            flush=True,
        )
    return collapsed


# Bulk load


def _iter_row_tuples(
    rows: Iterable[dict[str, Any]], fieldnames: list[str], *, table: str
) -> Iterator[tuple[object, ...]]:
    for row in rows:
        yield row_tuple(row, fieldnames, table=table)


def _iter_csv_tuples(
    path: Path, fieldnames: list[str], *, table: str, load_at: datetime
) -> Iterator[tuple[object, ...]]:
    """Stream CSV rows without loading the whole file (citations are large)."""
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            row[LOAD_DATE_COLUMN] = load_at
            yield row_tuple(row, fieldnames, table=table)


def _rewrite_csv_load_date(path: Path, table: str, load_at: datetime) -> None:
    """Rewrite a CSV so it includes the same load_date just written to SQL.

    Used by `python -m pipeline.db` so on-disk files match the database after a
    CSV-only reload. Seconds precision keeps the stamp readable in Excel.
    """
    grain = _grain_columns(table)
    fields = grain + [LOAD_DATE_COLUMN]
    stamp = load_at.isoformat(timespec="seconds")
    temp_path = path.with_name(path.stem + ".tmp.csv")
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        with temp_path.open("w", newline="", encoding="utf-8") as dest:
            writer = csv.DictWriter(dest, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in reader:
                row[LOAD_DATE_COLUMN] = stamp
                writer.writerow(row)
    temp_path.replace(path)


def _category_regions() -> list[str]:
    """Return the countries named on the active Profound category."""
    country = active_country()
    if not country.regions:
        raise SystemExit(f"{country.slug} has no regions in its name.")
    return list(country.regions)


def _placeholders(count: int) -> str:
    """Comma-separated parameter markers for an IN list."""
    return ", ".join("?" for _ in range(count))


def _apply_staged_rows(
    cursor,
    *,
    table: str,
    qualified: str,
    qualified_stage: str,
    columns: str,
    mode: str,
) -> None:
    """Move staged rows onto the target, either for one category or the whole table."""
    if mode == "replace":
        cursor.execute(f"TRUNCATE TABLE {qualified}")
    elif mode == "slice" and table == "dim_prompt":
        # Prompt regions are a pipe-joined list, so this category is matched
        # by those tokens. Prompts for the other category stay.
        cursor.execute(
            f"""
            DELETE target
            FROM {qualified} AS target
            WHERE EXISTS (
                SELECT 1
                FROM STRING_SPLIT(target.[regions], '|') AS existing_region
                WHERE LTRIM(RTRIM(existing_region.value)) <> ''
                  AND LTRIM(RTRIM(existing_region.value)) IN (
                      SELECT LTRIM(RTRIM(incoming_region.value))
                      FROM {qualified_stage} AS staged
                      CROSS APPLY STRING_SPLIT(staged.[regions], '|') AS incoming_region
                      WHERE LTRIM(RTRIM(incoming_region.value)) <> ''
                  )
            )
            """
        )
    elif mode == "slice":
        cursor.execute(
            f"""
            DELETE target
            FROM {qualified} AS target
            WHERE target.[region] IN (
                SELECT DISTINCT staged.[region]
                FROM {qualified_stage} AS staged
                WHERE staged.[region] IS NOT NULL
                  AND LTRIM(RTRIM(staged.[region])) <> ''
            )
            """
        )
    else:
        raise ValueError(f"Unknown SQL replace mode {mode!r}.")
    cursor.execute(
        f"INSERT INTO {qualified} ({columns}) SELECT {columns} FROM {qualified_stage}"
    )


def _replace_with_rows(
    cursor,
    conn,
    *,
    schema: str,
    table: str,
    fieldnames: list[str],
    rows: Iterable[tuple[object, ...]],
    mode: str = "replace",
) -> int:
    """Stage rows, then atomically merge or replace the target. Returns rows copied."""
    qualified = _qualified(schema, table)
    stage = f"__stage_{table}_{uuid4().hex}"
    qualified_stage = _qualified(schema, stage)
    columns = ", ".join(f"[{name}]" for name in fieldnames)
    cursor.execute(f"SELECT TOP 0 * INTO {qualified_stage} FROM {qualified}")
    # bulkcopy opens its own connection, so the staging table must be committed.
    conn.commit()
    try:
        result = cursor.bulkcopy(
            qualified_stage,
            rows,
            batch_size=_BULK_BATCH_SIZE,
            timeout=_BULK_TIMEOUT_SECONDS,
            column_mappings=fieldnames,
            table_lock=True,
            keep_nulls=True,
        )
        copied = int(result.get("rows_copied") or 0)
        _apply_staged_rows(
            cursor,
            table=table,
            qualified=qualified,
            qualified_stage=qualified_stage,
            columns=columns,
            mode=mode,
        )
        cursor.execute(f"DROP TABLE {qualified_stage}")
        conn.commit()
        return copied
    except Exception:
        conn.rollback()
        cursor.execute(
            f"IF OBJECT_ID(N'{schema}.{stage}', N'U') IS NOT NULL "
            f"DROP TABLE {qualified_stage}"
        )
        conn.commit()
        raise


def sql_is_configured() -> bool:
    """True when this process should write Azure SQL."""
    return not skip_sql() and _sql_env() is not None


def replace_table(
    table: str,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
    *,
    conn=None,
) -> int | None:
    """Merge one category into dbo, or replace a dimension table.

    Returns rows copied, or None when SQL is skipped.
    """
    global _skip_notice_shown, _enabled_notice_shown
    if not sql_is_configured():
        if not _skip_notice_shown:
            print("Azure SQL is not configured; writing CSVs only.")
            _skip_notice_shown = True
        return None

    schema = SQL_SCHEMA
    if table not in TABLES:
        raise SystemExit(f"Unknown SQL table {table!r}.")
    grain = _grain_columns(table)
    provided = [name for name in fieldnames if name != LOAD_DATE_COLUMN]
    if provided != grain:
        raise SystemExit(
            f"{table} column order {provided} does not match SQL grain {grain}."
        )

    own_connection = conn is None
    if own_connection:
        settings = _sql_env()
        assert settings is not None
        conn = _connect(settings)
    try:
        cursor = conn.cursor()
        ensure_schema_and_tables(cursor, schema)
        conn.commit()
        prepared = _collapse_scores_if_needed(table, rows)
        # Dual-write already stamps rows; fill only if the caller omitted it.
        if not prepared or prepared[0].get(LOAD_DATE_COLUMN) in (None, ""):
            prepared = stamp_load_date(prepared)
        sql_fields = _columns(table)
        copied = _replace_with_rows(
            cursor,
            conn,
            schema=schema,
            table=table,
            fieldnames=sql_fields,
            rows=_iter_row_tuples(prepared, sql_fields, table=table),
            mode="slice" if table in _SLICE_TABLES else "replace",
        )
        if not _enabled_notice_shown:
            print(f"Azure SQL: {settings_label()}", flush=True)
            _enabled_notice_shown = True
        print(f"  SQL [{schema}].[{table}]: {copied} rows", flush=True)
        return copied
    finally:
        if own_connection:
            conn.close()


def settings_label() -> str:
    """Return server/database for logs (never the password)."""
    settings = _sql_env()
    if settings is None:
        return "unconfigured"
    return f"{settings['AZURE_SQL_SERVER']}/{settings['AZURE_SQL_DATABASE']}"


def latest_table_date(table: str) -> str:
    """Return the latest date for the active category's regions in dbo."""
    if table not in _DATE_INDEXED:
        raise ValueError(f"{table!r} is not a date-indexed fact table.")
    regions = _category_regions()
    placeholders = _placeholders(len(regions))
    with sql_connection() as conn:
        if conn is None:
            raise RuntimeError("Azure SQL must be configured for cloud execution.")
        cursor = conn.cursor()
        ensure_schema_and_tables(cursor, SQL_SCHEMA)
        conn.commit()
        cursor.execute(
            f"""
            SELECT MAX([date])
            FROM {_qualified(SQL_SCHEMA, table)}
            WHERE [region] IN ({placeholders})
            """,
            tuple(regions),
        )
        row = cursor.fetchone()
    value = None if row is None else row[0]
    if value is None:
        names = ", ".join(regions)
        raise RuntimeError(
            f"[{SQL_SCHEMA}].[{table}] has no dated rows for {names}. "
            "Run a full CLI load first."
        )
    return _as_date(value).isoformat()


def read_rows_before(table: str, before_date: str) -> list[dict[str, Any]]:
    """Read this category's rows older than a date from the combined dbo table."""
    if table not in _DATE_INDEXED:
        raise ValueError(f"{table!r} is not a date-indexed fact table.")
    regions = _category_regions()
    placeholders = _placeholders(len(regions))
    fields = _grain_columns(table)
    columns = ", ".join(f"[{name}]" for name in fields)
    with sql_connection() as conn:
        if conn is None:
            raise RuntimeError("Azure SQL must be configured for cloud execution.")
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {columns}
            FROM {_qualified(SQL_SCHEMA, table)}
            WHERE [date] < ? AND [region] IN ({placeholders})
            """,
            (before_date, *regions),
        )
        rows = cursor.fetchall()
    return [dict(zip(fields, row, strict=True)) for row in rows]


def rebuild_dimensions_from_sql() -> None:
    """Replace shared dimensions from distinct keys in both dbo fact tables."""
    with sql_connection() as conn:
        if conn is None:
            raise RuntimeError("Azure SQL must be configured for cloud execution.")
        cursor = conn.cursor()
        ensure_schema_and_tables(cursor, SQL_SCHEMA)
        conn.commit()
        for table, column in _SLICER_DIMENSIONS.items():
            cursor.execute(
                f"""
                SELECT [{column}]
                FROM {_qualified(SQL_SCHEMA, _SCORES_TABLE)}
                WHERE [{column}] IS NOT NULL
                UNION
                SELECT [{column}]
                FROM {_qualified(SQL_SCHEMA, "fact_raw_citations")}
                WHERE [{column}] IS NOT NULL
                """
            )
            rows = [{column: row[0]} for row in cursor.fetchall()]
            rows.sort(key=lambda row: str(row[column]))
            replace_table(table, rows, [column], conn=conn)


def load_csvs(data_folder: Path, conn) -> None:
    """Replace every present CSV in data/ into the dbo tables."""
    cursor = conn.cursor()
    ensure_schema_and_tables(cursor, SQL_SCHEMA)
    conn.commit()
    loaded = 0
    load_at = load_now()
    for table in TABLES:
        path = data_folder / f"{table}.csv"
        if not path.is_file():
            print(f"  skip [{SQL_SCHEMA}].[{table}] (no {path.name})")
            continue
        fieldnames = _columns(table)
        started = perf_counter()
        # Scores must be collapsed in memory; other tables stream from disk.
        if table == _SCORES_TABLE:
            with path.open(newline="", encoding="utf-8") as file:
                dict_rows = list(csv.DictReader(file))
            source_rows = stamp_load_date(
                _collapse_scores_if_needed(table, dict_rows), when=load_at
            )
            source = _iter_row_tuples(source_rows, fieldnames, table=table)
        else:
            source = _iter_csv_tuples(path, fieldnames, table=table, load_at=load_at)
        copied = _replace_with_rows(
            cursor,
            conn,
            schema=SQL_SCHEMA,
            table=table,
            fieldnames=fieldnames,
            rows=source,
            mode="replace",
        )
        _rewrite_csv_load_date(path, table, load_at)
        seconds = perf_counter() - started
        print(
            f"  SQL [{SQL_SCHEMA}].[{table}]: {copied} rows from {path.name} "
            f"({seconds:.1f}s)",
            flush=True,
        )
        loaded += 1
    if not loaded:
        print(f"  No CSVs found in {data_folder}")


def try_replace_table(
    table: str, rows: list[dict[str, Any]], fieldnames: list[str]
) -> None:
    """Write one table to Azure SQL after a CSV export; no-op when SQL is skipped."""
    replace_table(table, rows, fieldnames)


# CLI


def main(argv: list[str] | None = None) -> None:
    """Create the dbo tables and load combined data/*.csv into Azure SQL."""
    parser = argparse.ArgumentParser(
        description="Load combined data/*.csv files into the dbo schema."
    )
    parser.add_argument(
        "--ensure-only",
        action="store_true",
        help="Create dbo tables; do not load CSVs.",
    )
    args = parser.parse_args(argv)
    if skip_sql():
        raise SystemExit("AZURE_SQL_SKIP is set; unset it to load Azure SQL.")
    if _sql_env() is None:
        raise SystemExit(
            "Azure SQL settings are missing from .env "
            "(AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, "
            "AZURE_SQL_PASSWORD)."
        )

    print(f"Azure SQL: {settings_label()}", flush=True)
    with sql_connection() as conn:
        if conn is None:
            raise SystemExit("Could not open an Azure SQL connection.")
        print("=== dbo ===", flush=True)
        if args.ensure_only:
            cursor = conn.cursor()
            ensure_schema_and_tables(cursor, SQL_SCHEMA)
            conn.commit()
            print("  Ensured schema [dbo] and tables.")
            return
        load_csvs(project_root() / "data", conn)


if __name__ == "__main__":
    main()
