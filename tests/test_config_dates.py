"""Verify reporting windows consistently use the Toronto calendar date."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from pipeline.config import drop_incomplete_dates, end_date


class ReportingDateTests(unittest.TestCase):
    def test_end_date_is_previous_toronto_calendar_day(self) -> None:
        with patch("pipeline.config.pipeline_today", return_value=date(2026, 9, 15)):
            self.assertEqual(end_date(), "2026-09-14")

    def test_incomplete_rows_use_toronto_calendar_cutoff(self) -> None:
        rows = [{"date": "2026-09-14"}, {"date": "2026-09-15"}]

        with patch("pipeline.config.pipeline_today", return_value=date(2026, 9, 15)):
            kept = drop_incomplete_dates(rows)

        self.assertEqual(kept, [{"date": "2026-09-14"}])


if __name__ == "__main__":
    unittest.main()
