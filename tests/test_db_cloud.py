"""Verify managed-identity configuration and atomic Azure SQL replacement."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pipeline.db import _connect, _replace_with_rows, _sql_env

RESUMING_ERROR = RuntimeError(
    "Database 'geo' on server 'geo.database.windows.net' is not currently "
    "available. Please retry the connection later."
)


class FakeConnection:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeCursor:
    def __init__(self, *, fail_bulk: bool = False) -> None:
        self.events: list[str] = []
        self.fail_bulk = fail_bulk

    def execute(self, statement: str, *_args) -> None:
        self.events.append(" ".join(statement.split()))

    def bulkcopy(self, _table: str, _rows, **_kwargs):
        self.events.append("BULKCOPY")
        if self.fail_bulk:
            raise RuntimeError("bulk failed")
        return {"rows_copied": 1}


class SqlConfigurationTests(unittest.TestCase):
    def test_server_and_database_enable_managed_identity(self) -> None:
        values = {
            "AZURE_SQL_SERVER": "server.database.windows.net",
            "AZURE_SQL_DATABASE": "analytics",
        }
        with (
            patch.dict(os.environ, values, clear=True),
            patch("pipeline.db.load_dotenv"),
        ):
            settings = _sql_env()

        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(settings["AZURE_SQL_USERNAME"], "")
        self.assertEqual(settings["AZURE_SQL_PASSWORD"], "")


class AtomicReplacementTests(unittest.TestCase):
    def test_target_is_truncated_only_after_staging_bulk_copy(self) -> None:
        cursor = FakeCursor()
        connection = FakeConnection()

        copied = _replace_with_rows(
            cursor,
            connection,
            schema="europe-asia",
            table="dim_date",
            fieldnames=["date", "load_date"],
            rows=[("2026-09-14", "2026-09-15")],
        )

        bulk_index = cursor.events.index("BULKCOPY")
        truncate_index = next(
            index
            for index, event in enumerate(cursor.events)
            if event == "TRUNCATE TABLE [europe-asia].[dim_date]"
        )
        self.assertEqual(copied, 1)
        self.assertLess(bulk_index, truncate_index)

    def test_failed_bulk_copy_does_not_truncate_target(self) -> None:
        cursor = FakeCursor(fail_bulk=True)
        connection = FakeConnection()

        with self.assertRaisesRegex(RuntimeError, "bulk failed"):
            _replace_with_rows(
                cursor,
                connection,
                schema="europe-asia",
                table="dim_date",
                fieldnames=["date", "load_date"],
                rows=[],
            )

        self.assertNotIn("TRUNCATE TABLE [europe-asia].[dim_date]", cursor.events)
        self.assertEqual(connection.rollbacks, 1)

    def test_category_slice_deletes_only_staged_countries(self) -> None:
        cursor = FakeCursor()
        connection = FakeConnection()

        _replace_with_rows(
            cursor,
            connection,
            schema="dbo",
            table="fact_scores_summarized",
            fieldnames=["date", "country", "load_date"],
            rows=[("2026-09-14", "Germany", "2026-09-15")],
            mode="slice",
        )

        statements = " ".join(cursor.events)
        self.assertNotIn("TRUNCATE TABLE", statements)
        self.assertIn("DELETE target", statements)
        self.assertIn("target.[country] IN", statements)
        self.assertIn("INSERT INTO [dbo].[fact_scores_summarized]", statements)


class ConnectRetryTests(unittest.TestCase):
    def test_retries_while_paused_database_resumes(self) -> None:
        connection = FakeConnection()
        with (
            patch(
                "pipeline.db._open_connection",
                side_effect=[RESUMING_ERROR, RESUMING_ERROR, connection],
            ) as opener,
            patch("pipeline.db.sleep") as sleeper,
        ):
            self.assertIs(_connect({}), connection)

        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_other_errors_are_not_retried(self) -> None:
        with (
            patch(
                "pipeline.db._open_connection",
                side_effect=RuntimeError("Login failed for user"),
            ) as opener,
            patch("pipeline.db.sleep") as sleeper,
        ):
            with self.assertRaisesRegex(RuntimeError, "Login failed"):
                _connect({})

        self.assertEqual(opener.call_count, 1)
        sleeper.assert_not_called()

    def test_gives_up_after_last_attempt(self) -> None:
        with (
            patch(
                "pipeline.db._open_connection", side_effect=RESUMING_ERROR
            ) as opener,
            patch("pipeline.db.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "not currently available"):
                _connect({})

        self.assertEqual(opener.call_count, 10)


if __name__ == "__main__":
    unittest.main()
