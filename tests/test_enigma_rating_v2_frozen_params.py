from __future__ import annotations

import unittest

from app import enigma_rating_v2_frozen_params as frozen


class EnigmaRatingV2FrozenParamsTests(unittest.TestCase):
    def test_final_validation_winners_are_frozen_exactly(self) -> None:
        self.assertEqual(frozen.FROZEN_ELO_K_FACTOR, 45.0)
        self.assertEqual(frozen.FROZEN_ELO_HOME_ADVANTAGE, 110.0)
        self.assertEqual(frozen.FROZEN_ELO_DRAW_PARAMETER, 0.50)
        self.assertEqual(frozen.FROZEN_ELO_WARMUP_DAYS, 1460)
        self.assertEqual(frozen.FROZEN_DIXON_COLES_RHO, 0.24)
        self.assertEqual(frozen.FROZEN_POISSON_HOME_MULTIPLIER, 1.08)

    def test_selection_provenance_and_holdout_boundary_are_immutable_constants(self) -> None:
        self.assertEqual(
            frozen.FROZEN_SELECTION_SHA256,
            "3d7a37a3c81cf383f08057e6ecfa1b8cf18abe5a2a7421698fdcf31acb736dcc",
        )
        self.assertEqual(frozen.SELECTION_END_DATE, "2026-08-24")
        self.assertEqual(frozen.CONFIRMATION_HOLDOUT_START_DATE, "2026-08-25")
        self.assertEqual(frozen.CONFIRMATION_MIN_ELIGIBLE_TARGETS, 100)

    def test_manifest_forbids_peeking_and_retuning(self) -> None:
        manifest = frozen.frozen_tuning_manifest()
        self.assertEqual(manifest["status"], "FROZEN")
        holdout = manifest["confirmation_holdout"]
        self.assertEqual(holdout["status"], "RESERVED_UNTOUCHED")
        self.assertFalse(holdout["performance_peeking_before_minimum_targets_allowed"])
        self.assertFalse(holdout["retuning_with_holdout_data_allowed"])
        self.assertTrue(manifest["policy"]["no_further_parameter_search_before_confirmation"])

    def test_manifest_returns_defensive_copy(self) -> None:
        first = frozen.frozen_tuning_manifest()
        first["frozen_parameters"]["elo"]["k_factor"] = 999.0
        second = frozen.frozen_tuning_manifest()
        self.assertEqual(second["frozen_parameters"]["elo"]["k_factor"], 45.0)


if __name__ == "__main__":
    unittest.main()
