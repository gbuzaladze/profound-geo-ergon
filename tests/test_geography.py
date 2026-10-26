"""Country stays the Profound market, and region is the screenshot grouping."""

from __future__ import annotations

import unittest

from pipeline.config import COUNTRIES, geography_from_row, region_for_country
from pipeline.db import _grain_columns
from pipeline.raw_citations import FIELDNAMES as CITATION_FIELDS
from pipeline.scores_summarized import FIELDNAMES as SCORE_FIELDS

EXPECTED = {
    "South Korea": "APAC",
    "Singapore": "APAC",
    "Indonesia": "APAC",
    "Australia": "APAC",
    "United Arab Emirates": "APAC",
    "Vietnam": "APAC",
    "United States": "Americas",
    "Mexico": "Americas",
    "Brazil": "Americas",
    "Germany": "Europe",
    "France": "Europe",
    "Belgium": "Europe",
    "Spain": "Europe",
    "Sweden": "Europe",
    "United Kingdom": "Europe",
}


class RegionMapTests(unittest.TestCase):
    def test_screenshot_groups(self) -> None:
        for country, region in EXPECTED.items():
            self.assertEqual(region_for_country(country), region)

    def test_every_configured_country_has_a_region(self) -> None:
        for country in COUNTRIES:
            for name in country.countries:
                self.assertEqual(region_for_country(name), EXPECTED[name])

    def test_legacy_region_column_becomes_country(self) -> None:
        self.assertEqual(geography_from_row({"region": "France"}), ("France", "Europe"))

    def test_country_and_region_stay(self) -> None:
        self.assertEqual(
            geography_from_row({"country": "Brazil", "region": "Americas"}),
            ("Brazil", "Americas"),
        )

    def test_blank_country_has_no_region(self) -> None:
        self.assertEqual(region_for_country(""), "")
        self.assertEqual(geography_from_row({"region": ""}), ("", ""))

    def test_unknown_country_exits(self) -> None:
        with self.assertRaises(SystemExit):
            region_for_country("Canada")

    def test_region_column_follows_country(self) -> None:
        self.assertEqual(SCORE_FIELDS, _grain_columns("fact_scores_summarized"))
        self.assertEqual(CITATION_FIELDS, _grain_columns("fact_raw_citations"))
        for fields in (SCORE_FIELDS, CITATION_FIELDS):
            country_at = fields.index("country")
            self.assertEqual(fields[country_at : country_at + 2], ["country", "region"])


if __name__ == "__main__":
    unittest.main()
