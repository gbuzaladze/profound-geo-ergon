"""Owned citation hosts match Ergon domains."""

from __future__ import annotations

import unittest

from pipeline.config import is_owned_citation_host


class OwnedCitationHostTests(unittest.TestCase):
    def test_ergon_in_the_domain_is_owned(self) -> None:
        self.assertTrue(is_owned_citation_host("ergon.com", "ergon.com"))
        self.assertTrue(is_owned_citation_host("www.ergon.com", "ergon.com"))

    def test_unrelated_domain_stays_unowned(self) -> None:
        self.assertFalse(is_owned_citation_host("example.com", "example.com"))
        self.assertFalse(is_owned_citation_host("shell.com", "shell.com"))

    def test_listed_host_is_owned(self) -> None:
        self.assertTrue(is_owned_citation_host("www.ergon.com", "ergon.com"))


if __name__ == "__main__":
    unittest.main()
