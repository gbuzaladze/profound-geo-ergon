"""Owned-asset selection stays on the country brand, not a generic alias."""

from __future__ import annotations

import unittest

from pipeline.config import select_country
from pipeline.scores_summarized import ensure_owned_assets


class EnsureOwnedAssetsTests(unittest.TestCase):
    def test_mexico_keeps_local_brand_and_tracked_products(self) -> None:
        select_country("mexico", create_data_dir=False)
        names: list[str] = []
        owned: dict[str, bool] = {}

        ensure_owned_assets(
            names,
            owned,
            [("Novartis - Mexico", True), ("Kesimpta", False)],
        )

        self.assertEqual(names, ["Novartis - Mexico", "Kesimpta"])
        self.assertNotIn("Novartis", names)
        self.assertNotIn("Fabhalta", names)
        self.assertTrue(owned["Novartis - Mexico"])
        self.assertTrue(owned["Kesimpta"])

    def test_brazil_skips_generic_novartis_alias(self) -> None:
        select_country("brazil", create_data_dir=False)
        names: list[str] = ["Novartis - Brazil"]
        owned = {"Novartis - Brazil": True}

        ensure_owned_assets(names, owned, [("Novartis - Brazil", True)])

        self.assertEqual(names, ["Novartis - Brazil"])
        self.assertNotIn("Novartis", names)

    def test_canada_keeps_novartis_when_unranked(self) -> None:
        select_country("canada", create_data_dir=False)
        names: list[str] = []
        owned: dict[str, bool] = {}

        ensure_owned_assets(names, owned, [("Kesimpta", False)])

        self.assertEqual(names, ["Novartis", "Kesimpta"])
        self.assertTrue(owned["Novartis"])


if __name__ == "__main__":
    unittest.main()
