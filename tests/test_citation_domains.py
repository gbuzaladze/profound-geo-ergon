"""Domain-file category overrides on citation rows."""

from __future__ import annotations

import unittest

from pipeline.db import _grain_columns
from pipeline.raw_citations import FIELDNAMES, apply_category_rules, load_citation_domains


class CitationDomainRuleTests(unittest.TestCase):
    def test_category_is_followed_by_mentioned(self) -> None:
        category_at = FIELDNAMES.index("category")
        self.assertEqual(FIELDNAMES[category_at : category_at + 2], ["category", "mentioned"])
        self.assertEqual(FIELDNAMES, _grain_columns("fact_raw_citations"))

    def test_matching_domain_overwrites_category(self) -> None:
        rows = apply_category_rules(
            [
                {
                    "category": "other",
                    "hostname": "www.example.org",
                    "domain": "example.org",
                }
            ],
            {"example.org": "Institution"},
        )
        self.assertEqual(rows[0]["category"], "Institutions")

    def test_unmatched_domain_keeps_profound_category(self) -> None:
        rows = apply_category_rules(
            [
                {
                    "category": "earned_media",
                    "hostname": "www.example.com",
                    "domain": "example.com",
                }
            ],
            {},
        )
        self.assertEqual(rows[0]["category"], "Earned Media")

    def test_unmatched_owned_host_stays_owned(self) -> None:
        rows = apply_category_rules(
            [
                {
                    "category": "other",
                    "hostname": "www.ergon.com",
                    "domain": "ergon.com",
                }
            ],
            {},
        )
        self.assertEqual(rows[0]["category"], "Owned")

    def test_missing_domain_file_is_empty(self) -> None:
        self.assertEqual(load_citation_domains("americas-uk-au-uae"), {})
        self.assertEqual(load_citation_domains("europe-asia"), {})


if __name__ == "__main__":
    unittest.main()
