"""Verify the Tuesday orchestration ID respects Toronto time and DST."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from azure.durable_functions.models.DurableOrchestrationStatus import (
    DurableOrchestrationStatus,
)

from function_app import instance_exists, scheduled_instance_id


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


class InstanceExistsTests(unittest.TestCase):
    def test_not_found_status_is_not_an_existing_run(self) -> None:
        not_found = DurableOrchestrationStatus.from_json(
            {"message": "Instance not found"}
        )

        self.assertFalse(instance_exists(None))
        self.assertFalse(instance_exists(not_found))

    def test_known_status_is_an_existing_run(self) -> None:
        running = DurableOrchestrationStatus.from_json(
            {"instanceId": "geo-weekly-2026-10-13", "runtimeStatus": "Running"}
        )

        self.assertTrue(instance_exists(running))


if __name__ == "__main__":
    unittest.main()
