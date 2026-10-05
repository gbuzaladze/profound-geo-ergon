"""Shared Profound client, hourly quota, and CSV write helpers.

Callers use `write_csv_and_sql` for final tables: it stamps `load_date`
(America/Toronto) on every row, writes the CSV, then replaces the Azure SQL
table named after the file stem (`fact_scores_summarized.csv` -> that table).
"""

from __future__ import annotations

import csv
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import profound
from profound import Profound

from pipeline.config import HOURLY_API_LIMIT, load_configuration

_HOUR_SECONDS = 3600
# Extra second so the oldest call is fully outside the rolling hour.
_RATE_LIMIT_BUFFER_SECONDS = 1
# Sleep after Profound returns 429; the client limiter only tracks this process.
RATE_LIMIT_SLEEP_SECONDS = 3600


# Names and API client


def ref_name(value: object) -> str | None:
    """Return a plain name from a string, dict, or SDK dimension ref."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)


@contextmanager
def profound_client(*, timeout: float = 120.0) -> Iterator[Profound]:
    """Yield an authenticated Profound client.

    `timeout` is seconds per HTTP request. Auth/permission/connection failures
    become SystemExit; 429s are re-raised so callers can wait and retry.
    """
    load_configuration()
    try:
        with Profound(timeout=timeout) as client:
            yield client
    except profound.AuthenticationError as error:
        raise SystemExit("Authentication failed. Check PROFOUND_API_KEY.") from error
    except profound.PermissionDeniedError as error:
        raise SystemExit("The API key lacks access to this category.") from error
    except profound.RateLimitError:
        # Subclass of APIStatusError; callers retry 429s themselves.
        raise
    except profound.APIStatusError as error:
        raise SystemExit(
            f"Profound API request failed with HTTP {error.status_code}."
        ) from error
    except profound.APIConnectionError as error:
        raise SystemExit("Could not connect to the Profound API.") from error


# Rate limiting


class HourlyRateLimiter:
    """Pause before a call if this process has already used the hourly quota."""

    def __init__(self, limit: int = HOURLY_API_LIMIT) -> None:
        self.limit = limit
        self._calls: deque[float] = deque()

    def wait(self) -> None:
        """Block until a request slot is available, then consume it.

        Tracks call timestamps in a sliding 3600-second window so a multi-country
        run cannot exceed Profound's hourly REST quota.
        """
        while True:
            now = monotonic()
            cutoff = now - _HOUR_SECONDS
            # Drop timestamps that have aged out of the rolling hour.
            while self._calls and self._calls[0] <= cutoff:
                self._calls.popleft()
            if len(self._calls) < self.limit:
                self._calls.append(monotonic())
                return
            sleep_for = (
                self._calls[0] + _HOUR_SECONDS - now + _RATE_LIMIT_BUFFER_SECONDS
            )
            print(
                f"Hourly API limit of {self.limit} reached; "
                f"sleeping {sleep_for / 60:.1f} minutes.",
                flush=True,
            )
            sleep(sleep_for)


_shared_limiter: HourlyRateLimiter | None = None


def rate_limiter() -> HourlyRateLimiter:
    """Return the process-wide hourly limiter so country runs share one quota."""
    global _shared_limiter
    if _shared_limiter is None:
        _shared_limiter = HourlyRateLimiter()
    return _shared_limiter


def call_api(fn, *args, **kwargs):
    """Make one Profound call, waiting on the 600/hour cap and retrying 429s."""
    limiter = rate_limiter()
    while True:
        limiter.wait()
        try:
            return fn(*args, **kwargs)
        except profound.RateLimitError:
            print(
                "Profound returned 429 Too Many Requests; "
                f"sleeping {RATE_LIMIT_SLEEP_SECONDS / 60:.0f} minutes.",
                flush=True,
            )
            sleep(RATE_LIMIT_SLEEP_SECONDS)


# CSV I/O


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV file as a list of row dicts."""
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write_csv_and_sql(
    rows: list[dict[str, Any]],
    *,
    path: Path,
    fieldnames: list[str],
) -> Path:
    """Write the CSV, then replace the same table in Azure SQL when configured."""
    # Imported here because pipeline.db must not import this module.
    from pipeline.db import LOAD_DATE_COLUMN, stamp_load_date, try_replace_table

    stamped = stamp_load_date(rows)
    # Grain columns first, load_date last, matching Azure SQL column order.
    fields = [name for name in fieldnames if name != LOAD_DATE_COLUMN]
    fields.append(LOAD_DATE_COLUMN)
    written = write_csv(stamped, path=path, fieldnames=fields)
    try_replace_table(path.stem, stamped, fields)
    return written


def write_csv(
    rows: list[dict[str, Any]],
    *,
    path: Path,
    fieldnames: list[str],
) -> Path:
    """Write rows to `path`.

    Writes a sibling `.tmp.csv` first, then replaces the target so a crash
    does not leave a truncated file. If the target is open in Excel, the temp
    file is left in place instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.stem + ".tmp.csv")
    with temp_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    try:
        temp_path.replace(path)
        return path
    except PermissionError:
        print(
            f"Could not replace {path} (file is open). Left new export at {temp_path}."
        )
        return temp_path


def append_csv(
    rows: list[dict[str, Any]],
    *,
    path: Path,
    fieldnames: list[str],
    write_header: bool,
) -> None:
    """Append rows to a CSV, optionally writing the header first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if write_header else "a"
    with path.open(mode, newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
