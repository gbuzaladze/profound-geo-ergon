"""Verify managed-identity configuration and atomic Azure SQL replacement."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pipeline.db import _replace_with_rows, _sql_env


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
            schema="canada",
            table="dim_date",
            fieldnames=["date", "load_date"],
            rows=[("2026-09-14", "2026-09-15")],
        )

        bulk_index = cursor.events.index("BULKCOPY")
        truncate_index = next(
            index
            for index, event in enumerate(cursor.events)
            if event == "TRUNCATE TABLE [canada].[dim_date]"
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
                schema="canada",
                table="dim_date",
                fieldnames=["date", "load_date"],
                rows=[],
            )

        self.assertNotIn("TRUNCATE TABLE [canada].[dim_date]", cursor.events)
        self.assertEqual(connection.rollbacks, 1)


if __name__ == "__main__":
    unittest.main()
