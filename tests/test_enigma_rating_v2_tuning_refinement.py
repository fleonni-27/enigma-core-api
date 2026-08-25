from __future__ import annotations

import unittest
from datetime import date

from app import enigma_rating_v2_tuning_refinement as refinement


class EnigmaRatingV2TuningRefinementTests(unittest.TestCase):
    def test_refinement_grid_is_bounded_and_contains_coarse_winner(self) -> None:
        self.assertEqual(
            len(refinement.REFINEMENT_ELO_K_GRID)
            * len(refinement.REFINEMENT_ELO_HOME_ADVANTAGE_GRID)
            * len(refinement.REFINEMENT_ELO_DRAW_PARAMETER_GRID),
            125,
        )
        self.assertEqual(len(refinement.REFINEMENT_DIXON_COLES_RHO_GRID), 6)
        self.assertIn(30.0, refinement.REFINEMENT_ELO_K_GRID)
        self.assertIn(95.0, refinement.REFINEMENT_ELO_HOME_ADVANTAGE_GRID)
        self.assertIn(0.50, refinement.REFINEMENT_ELO_DRAW_PARAMETER_GRID)
        self.assertIn(0.12, refinement.REFINEMENT_DIXON_COLES_RHO_GRID)

    def test_refinement_cannot_cross_confirmation_holdout(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirmation holdout"):
            refinement.build_enigma_rating_v2_tuning_refinement_v1(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 8, 25),
            )

    def test_refinement_version_is_explicit(self) -> None:
        self.assertEqual(
            refinement.ENIGMA_RATING_V2_REFINEMENT_VERSION,
            "enigma_rating_v2_tuning_v1_refinement1",
        )
        self.assertEqual(refinement.COARSE_ELO_WINNER["elo_k_factor"], 30.0)
        self.assertEqual(refinement.COARSE_DIXON_COLES_WINNER["dixon_coles_rho"], 0.12)


if __name__ == "__main__":
    unittest.main()
