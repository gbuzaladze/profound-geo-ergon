"""Combined score export concatenates category CSVs without dropping rows."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from pipeline.combine import concat_csvs


class ConcatCsvTests(unittest.TestCase):
    def test_concat_keeps_every_row_and_rejects_a_different_header(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "a.csv"
            second = root / "b.csv"
            first.write_text("date,region\n2026-09-10,United States\n", encoding="utf-8")
            second.write_text("date,region\n2026-09-10,France\n", encoding="utf-8")
            destination = root / "all.csv"

            written = concat_csvs([first, second], destination)

            self.assertEqual(written, 2)
            with destination.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(
                [row["region"] for row in rows],
                ["United States", "France"],
            )

            mismatch = root / "c.csv"
            mismatch.write_text("date\n2026-09-11\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                concat_csvs([first, mismatch], destination)


if __name__ == "__main__":
    unittest.main()
