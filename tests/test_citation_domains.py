"""Domain-file category, subcategory, and pag on citation rows."""

from __future__ import annotations

import unittest

from pipeline.db import _columns_match, _grain_columns, convert_value
from pipeline.raw_citations import (
    FIELDNAMES,
    apply_category_rules,
    load_citation_domains,
)


class CitationDomainRuleTests(unittest.TestCase):
    def test_columns_follow_category(self) -> None:
        self.assertEqual(
            FIELDNAMES[FIELDNAMES.index("category") : FIELDNAMES.index("category") + 3],
            ["category", "subcategory", "pag"],
        )
        self.assertEqual(FIELDNAMES, _grain_columns("fact_raw_citations"))

    def test_matching_domain_overwrites_category(self) -> None:
        domains = {
            "example.org": ("Institution", "Institution BR", "TRUE"),
        }
        rows = apply_category_rules(
            [
                {
                    "category": "other",
                    "hostname": "www.example.org",
                    "domain": "example.org",
                }
            ],
            domains,
        )
        self.assertEqual(rows[0]["category"], "Institutions")
        self.assertEqual(rows[0]["subcategory"], "Institution BR")
        self.assertEqual(rows[0]["pag"], "TRUE")

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
        self.assertIsNone(rows[0]["subcategory"])
        self.assertIsNone(rows[0]["pag"])

    def test_unmatched_owned_host_stays_owned_with_blank_class(self) -> None:
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
        self.assertIsNone(rows[0]["subcategory"])
        self.assertIsNone(rows[0]["pag"])

    def test_missing_domain_file_is_empty(self) -> None:
        self.assertEqual(load_citation_domains("americas-uk-au-uae"), {})
        self.assertEqual(load_citation_domains("europe-asia"), {})

    def test_pag_blank_is_null_in_sql(self) -> None:
        self.assertIs(convert_value("pag", "TRUE", table="fact_raw_citations"), True)
        self.assertIs(convert_value("pag", "FALSE", table="fact_raw_citations"), False)
        self.assertIsNone(convert_value("pag", "", table="fact_raw_citations"))
        self.assertIsNone(convert_value("pag", None, table="fact_raw_citations"))

    def test_column_order_comparison_is_case_insensitive(self) -> None:
        self.assertTrue(_columns_match(["Category", "PAG"], ["category", "pag"]))
        self.assertFalse(_columns_match(["category", "mentioned"], ["category", "pag"]))


if __name__ == "__main__":
    unittest.main()
