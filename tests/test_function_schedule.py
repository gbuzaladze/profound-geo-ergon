"""Verify the daily orchestration ID respects Toronto time and DST."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from function_app import scheduled_instance_id


class ScheduledInstanceIdTests(unittest.TestCase):
    def test_waits_until_six_am_toronto_in_winter(self) -> None:
        before = datetime(2026, 1, 15, 10, 59, tzinfo=UTC)
        due = datetime(2026, 1, 15, 11, 0, tzinfo=UTC)

        self.assertIsNone(scheduled_instance_id(before))
        self.assertEqual(scheduled_instance_id(due), "geo-daily-2026-01-15")

    def test_handles_toronto_daylight_saving_time(self) -> None:
        due = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)

        self.assertEqual(scheduled_instance_id(due), "geo-daily-2026-07-15")


if __name__ == "__main__":
    unittest.main()
