"""Owned citation hosts include country-code Novartis domains."""

from __future__ import annotations

import unittest

from pipeline.config import is_owned_citation_host


class OwnedCitationHostTests(unittest.TestCase):
    def test_novartis_in_the_domain_is_owned(self) -> None:
        self.assertTrue(is_owned_citation_host("novartis.com.br", "novartis.com.br"))
        self.assertTrue(
            is_owned_citation_host("www.novartis.com.mx", "novartis.com.mx")
        )
        self.assertTrue(is_owned_citation_host("www.novartis.com", "novartis.com"))

    def test_unrelated_domain_stays_unowned(self) -> None:
        self.assertFalse(is_owned_citation_host("example.com.br", "example.com.br"))

    def test_listed_product_host_is_still_owned(self) -> None:
        hosts = (
            ("www.kisqali.com", "kisqali.com"),
            ("fabhalta.com", "fabhalta.com"),
            ("www.fabhalta-hcp.com", "fabhalta-hcp.com"),
            ("fabhalta-id.com", "fabhalta-id.com"),
            ("zolgensma.com", "zolgensma.com"),
            ("zolgensma-hcp.com", "zolgensma-hcp.com"),
            ("zolgensma-enrollment.com", "zolgensma-enrollment.com"),
            ("zolgensma-itvisma-copayassist.com", "zolgensma-itvisma-copayassist.com"),
            ("zolgensmacopayassist.com", "zolgensmacopayassist.com"),
            ("zolgensmareimbursement.com", "zolgensmareimbursement.com"),
        )
        for hostname, domain in hosts:
            with self.subTest(domain=domain):
                self.assertTrue(is_owned_citation_host(hostname, domain))


if __name__ == "__main__":
    unittest.main()
