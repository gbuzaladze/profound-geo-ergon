"""Verify SQL-backed citation refreshes."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from pipeline.raw_citations import run_incremental_sql


class SqlIncrementalCitationTests(unittest.TestCase):
    def test_keeps_older_sql_rows_and_replaces_watermark_window(self) -> None:
        older = [{"date": "2026-09-12", "url": "https://old.example"}]
        refreshed = [{"date": "2026-09-13", "url": "https://new.example"}]

        @contextmanager
        def fake_client():
            yield object()

        with (
            patch("pipeline.db.latest_table_date", return_value="2026-09-13"),
            patch("pipeline.db.read_rows_before", return_value=older),
            patch("pipeline.raw_citations.end_date", return_value="2026-09-14"),
            patch("pipeline.raw_citations.profound_client", fake_client),
            patch("pipeline.raw_citations.rate_limiter", return_value=object()),
            patch(
                "pipeline.raw_citations.fetch_citation_rows",
                return_value=(refreshed, 2, 1),
            ),
        ):
            rows, answers, api_calls, start, scan_end = run_incremental_sql()

        self.assertEqual(rows, older + refreshed)
        self.assertEqual((answers, api_calls), (2, 1))
        self.assertEqual((start, scan_end), ("2026-09-13", "2026-09-14"))


if __name__ == "__main__":
    unittest.main()
