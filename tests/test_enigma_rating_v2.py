from __future__ import annotations

import unittest

from app.enigma_rating_v2 import build_enigma_rating_v2
from app.main_v017 import app


class EnigmaRatingV2Tests(unittest.TestCase):
    def _full_payload(self) -> dict:
        return {
            "selection": "1",
            "calibrated_probability": 0.58,
            "market_probability": 0.49,
            "home_goals_for_avg": 1.8,
            "away_goals_for_avg": 1.1,
            "home_goals_against_avg": 0.9,
            "away_goals_against_avg": 1.4,
            "home_xg_for_avg": 1.95,
            "away_xg_for_avg": 1.18,
            "home_xg_against_avg": 0.88,
            "away_xg_against_avg": 1.47,
            "home_points_per_match_10": 2.1,
            "away_points_per_match_10": 1.2,
            "home_elo": 1580.0,
            "away_elo": 1490.0,
            "home_expected_xi_value": 100.0,
            "home_absent_value": 4.0,
            "away_expected_xi_value": 100.0,
            "away_absent_value": 16.0,
        }

    def test_full_evidence_reaches_full_component_coverage(self) -> None:
        result = build_enigma_rating_v2(**self._full_payload())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["version"], "enigma_rating_v2_research_v1")
        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertEqual(result["missing_components"], [])
        self.assertIn("poisson", result["signal_probabilities"])
        self.assertIn("dixon_coles", result["signal_probabilities"])
        self.assertIn("elo", result["signal_probabilities"])
        self.assertEqual(result["signal_probabilities"]["expected_goals"]["input_quality"], "FULL_XG_XGA")

    def test_missing_research_inputs_are_not_imputed(self) -> None:
        result = build_enigma_rating_v2(
            selection="X",
            calibrated_probability=0.35,
            market_probability=0.31,
            home_goals_for_avg=1.2,
            away_goals_for_avg=1.1,
            home_goals_against_avg=1.0,
            away_goals_against_avg=1.2,
        )
        self.assertLess(result["coverage_pct"], 100.0)
        self.assertIn("elo", result["missing_components"])
        self.assertIn("xg_xga", result["missing_components"])
        self.assertIn("recent_form_10", result["missing_components"])
        self.assertIn("lineup_impact", result["missing_components"])
        self.assertTrue(result["policy"]["missing_components_are_not_imputed"])

    def test_rating_v2_does_not_promote_or_replace_standard_model(self) -> None:
        result = build_enigma_rating_v2(**self._full_payload())
        self.assertTrue(result["policy"]["research_only"])
        self.assertTrue(result["policy"]["standard_36_features_unchanged"])
        self.assertEqual(result["policy"]["production_model_version_unchanged"], "baseline_1x2_temporal_v1")
        self.assertTrue(result["policy"]["rating_does_not_override_decision_engine"])

    def test_lineup_strength_can_be_supplied_directly(self) -> None:
        payload = self._full_payload()
        payload.pop("home_expected_xi_value")
        payload.pop("home_absent_value")
        payload.pop("away_expected_xi_value")
        payload.pop("away_absent_value")
        payload["home_lineup_strength"] = 0.91
        payload["away_lineup_strength"] = 0.79
        result = build_enigma_rating_v2(**payload)
        detail = result["components"]["lineup_impact"]["detail"]
        self.assertEqual(detail["home_strength_retained"], 0.91)
        self.assertEqual(detail["away_strength_retained"], 0.79)

    def test_v2_routes_are_registered_on_production_wrapper(self) -> None:
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/rating/enigma-v2", paths)
        self.assertIn("/rating/context-v2/{sportmonks_fixture_id}", paths)
        self.assertIn("/rating/enigma-v2/fixture/{sportmonks_fixture_id}", paths)
        self.assertEqual(app.version, "0.50.0")

    def test_invalid_lineup_strength_fails_closed(self) -> None:
        payload = self._full_payload()
        payload["home_lineup_strength"] = 1.2
        with self.assertRaises(ValueError):
            build_enigma_rating_v2(**payload)


if __name__ == "__main__":
    unittest.main()
