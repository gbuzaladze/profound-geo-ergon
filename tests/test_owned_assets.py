"""Owned-asset selection stays on the configured Ergon brand."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pipeline.config import select_country
from pipeline.scores_summarized import ensure_owned_assets


class EnsureOwnedAssetsTests(unittest.TestCase):
    def test_ergon_is_kept_when_unranked(self) -> None:
        select_country("europe-asia", create_data_dir=False)
        names: list[str] = []
        owned: dict[str, bool] = {}

        ensure_owned_assets(names, owned, [("Cargill", False), ("Shell", False)])

        self.assertEqual(names, ["Ergon"])
        self.assertNotIn("Cargill", names)
        self.assertTrue(owned["Ergon"])

    def test_tracked_ergon_is_marked_owned(self) -> None:
        select_country("americas-uk-au-uae", create_data_dir=False)
        names: list[str] = ["Ergon"]
        owned = {"Ergon": False}

        ensure_owned_assets(names, owned, [("Ergon", True)])

        self.assertEqual(names, ["Ergon"])
        self.assertTrue(owned["Ergon"])

    def test_alias_with_any_visibility_is_included(self) -> None:
        select_country("europe-asia", create_data_dir=False)
        names: list[str] = []
        owned: dict[str, bool] = {}

        with patch(
            "pipeline.scores_summarized.owned_asset_names",
            return_value=("Ergon", "Alias With Mentions", "Alias Absent"),
        ):
            ensure_owned_assets(
                names,
                owned,
                [("Ergon", True), ("Alias With Mentions", False)],
                {"Alias With Mentions": 1e-4, "Alias Absent": 0.0},
            )

        self.assertEqual(names, ["Ergon", "Alias With Mentions"])
        self.assertNotIn("Alias Absent", names)
        self.assertTrue(owned["Alias With Mentions"])


if __name__ == "__main__":
    unittest.main()
