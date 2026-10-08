"""Verify the Tuesday orchestration ID respects Toronto time and DST."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from function_app import scheduled_instance_id


class ScheduledInstanceIdTests(unittest.TestCase):
    def test_waits_until_six_am_toronto_on_tuesday_in_winter(self) -> None:
        before = datetime(2026, 1, 13, 10, 59, tzinfo=UTC)
        due = datetime(2026, 1, 13, 11, 0, tzinfo=UTC)

        self.assertIsNone(scheduled_instance_id(before))
        self.assertEqual(scheduled_instance_id(due), "geo-weekly-2026-01-13")

    def test_skips_other_weekdays(self) -> None:
        monday = datetime(2026, 1, 12, 11, 0, tzinfo=UTC)

        self.assertIsNone(scheduled_instance_id(monday))

    def test_handles_toronto_daylight_saving_time(self) -> None:
        due = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)

        self.assertEqual(scheduled_instance_id(due), "geo-weekly-2026-07-14")


if __name__ == "__main__":
    unittest.main()
