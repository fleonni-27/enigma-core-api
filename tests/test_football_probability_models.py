from __future__ import annotations

import unittest

from app.football_probability_models import (
    derive_expected_goals,
    dixon_coles_1x2,
    elo_davidson_1x2,
    elo_update,
    poisson_1x2,
)
from app.lineup_impact import quantify_lineup_impact, relative_lineup_support


class FootballProbabilityModelsTests(unittest.TestCase):
    def assert_probability_vector(self, payload: dict) -> None:
        probabilities = payload["probabilities"]
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=7)
        for value in probabilities.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_poisson_is_symmetric_for_equal_rates(self) -> None:
        result = poisson_1x2(1.35, 1.35)
        self.assert_probability_vector(result)
        self.assertAlmostEqual(result["probabilities"]["1"], result["probabilities"]["2"], places=7)

    def test_dixon_coles_adjusts_poisson_and_remains_normalized(self) -> None:
        poisson = poisson_1x2(1.25, 1.05)
        dc = dixon_coles_1x2(1.25, 1.05, rho=-0.08)
        self.assert_probability_vector(dc)
        self.assertNotEqual(poisson["probabilities"], dc["probabilities"])

    def test_expected_goals_prefers_xg_xga_when_available(self) -> None:
        result = derive_expected_goals(
            home_goals_for_avg=1.4,
            away_goals_for_avg=1.1,
            home_goals_against_avg=0.9,
            away_goals_against_avg=1.3,
            home_xg_for_avg=1.65,
            away_xg_for_avg=1.20,
            home_xg_against_avg=0.95,
            away_xg_against_avg=1.45,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["input_quality"], "FULL_XG_XGA")
        self.assertGreater(result["expected_goals"]["home"], 0.0)
        self.assertGreater(result["expected_goals"]["away"], 0.0)

    def test_expected_goals_fails_closed_when_attack_or_defence_missing(self) -> None:
        result = derive_expected_goals(
            home_goals_for_avg=1.4,
            away_goals_for_avg=None,
            home_goals_against_avg=0.9,
            away_goals_against_avg=1.3,
        )
        self.assertEqual(result["status"], "not_ready")
        self.assertIn("INSUFFICIENT_ATTACK_DEFENCE_INPUTS", result["reason_codes"])

    def test_elo_davidson_handles_draws_and_home_advantage(self) -> None:
        neutral = elo_davidson_1x2(1500, 1500, home_advantage_elo=0.0)
        home_adv = elo_davidson_1x2(1500, 1500, home_advantage_elo=65.0)
        self.assert_probability_vector(neutral)
        self.assert_probability_vector(home_adv)
        self.assertAlmostEqual(neutral["probabilities"]["1"], neutral["probabilities"]["2"], places=7)
        self.assertGreater(home_adv["probabilities"]["1"], home_adv["probabilities"]["2"])
        self.assertGreater(home_adv["probabilities"]["X"], 0.0)

    def test_elo_update_is_zero_sum(self) -> None:
        new_home, new_away = elo_update(1500, 1500, home_score=1.0, k_factor=20.0)
        self.assertAlmostEqual((new_home + new_away), 3000.0, places=8)
        self.assertGreater(new_home, 1500.0)
        self.assertLess(new_away, 1500.0)

    def test_lineup_absence_impact_is_explicit_and_bounded(self) -> None:
        result = quantify_lineup_impact(expected_xi_value=100.0, absent_value=18.0)
        self.assertAlmostEqual(result["strength_retained"], 0.82, places=8)
        self.assertAlmostEqual(result["absence_impact_pct"], 18.0, places=8)
        self.assertGreater(relative_lineup_support("1", 0.95, 0.75), 0.5)
        self.assertGreater(relative_lineup_support("X", 0.90, 0.90), 0.9)


if __name__ == "__main__":
    unittest.main()
