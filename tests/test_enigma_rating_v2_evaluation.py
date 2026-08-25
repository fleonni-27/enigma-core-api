import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

from app.enigma_rating_v2_evaluation import (
    MODEL_DC_GOALS,
    MODEL_DC_XG,
    MODEL_ELO,
    MODEL_POISSON_GOALS,
    MODEL_POISSON_XG,
    MODEL_STANDARD,
    _expected_goal_models,
    _fixture_payload,
    _form10_diagnostic,
    _paired_delta,
    _quality,
    _targets_from_baseline,
)


class EnigmaRatingV2EvaluationMetricTests(unittest.TestCase):
    def test_uniform_probabilities_match_uniform_baseline(self):
        rows = [
            {"actual": "1", "models": {MODEL_STANDARD: {"1": 1 / 3, "X": 1 / 3, "2": 1 / 3}}},
            {"actual": "X", "models": {MODEL_STANDARD: {"1": 1 / 3, "X": 1 / 3, "2": 1 / 3}}},
            {"actual": "2", "models": {MODEL_STANDARD: {"1": 1 / 3, "X": 1 / 3, "2": 1 / 3}}},
        ]
        quality = _quality(rows, MODEL_STANDARD)
        self.assertEqual(quality["sample_size"], 3)
        self.assertAlmostEqual(quality["brier_multiclass"], 2 / 3, places=6)
        self.assertAlmostEqual(quality["log_loss"], math.log(3), places=6)
        self.assertAlmostEqual(quality["skill_vs_uniform"]["brier_skill"], 0.0, places=6)
        self.assertAlmostEqual(quality["skill_vs_uniform"]["log_loss_skill"], 0.0, places=6)

    def test_paired_delta_uses_only_common_coverage_and_negative_is_better(self):
        rows = [
            {
                "actual": "1",
                "models": {
                    MODEL_STANDARD: {"1": 0.40, "X": 0.30, "2": 0.30},
                    MODEL_ELO: {"1": 0.90, "X": 0.05, "2": 0.05},
                },
            },
            {
                "actual": "2",
                "models": {
                    MODEL_STANDARD: {"1": 0.40, "X": 0.30, "2": 0.30},
                    MODEL_ELO: {"1": 0.05, "X": 0.05, "2": 0.90},
                },
            },
            {
                "actual": "X",
                "models": {
                    MODEL_STANDARD: {"1": 0.34, "X": 0.33, "2": 0.33},
                    MODEL_ELO: None,
                },
            },
        ]
        result = _paired_delta(rows, MODEL_ELO)
        self.assertEqual(result["common_sample_size"], 2)
        self.assertLess(result["brier_delta_challenger_minus_baseline"], 0.0)
        self.assertLess(result["log_loss_delta_challenger_minus_baseline"], 0.0)
        self.assertTrue(result["negative_delta_is_better"])


class EnigmaRatingV2EvaluationSignalTests(unittest.TestCase):
    @staticmethod
    def _history(*, xg_matches: int) -> dict:
        return {
            "history_matches": 10,
            "points_per_match": 1.7,
            "goals_for_avg": 1.6,
            "goals_against_avg": 1.1,
            "xg_for_avg": 1.55,
            "xg_against_avg": 1.05,
            "xg_for_history_matches": xg_matches,
            "xg_against_history_matches": xg_matches,
        }

    def test_goals_only_remains_available_when_xg_coverage_is_thin(self):
        result = _expected_goal_models(
            home_history=self._history(xg_matches=2),
            away_history=self._history(xg_matches=2),
            dixon_coles_rho=-0.08,
            home_advantage_multiplier=1.08,
        )
        self.assertIsNotNone(result[MODEL_POISSON_GOALS])
        self.assertIsNotNone(result[MODEL_DC_GOALS])
        self.assertIsNone(result[MODEL_POISSON_XG])
        self.assertIsNone(result[MODEL_DC_XG])

    def test_full_xg_xga_ablation_requires_auditable_coverage(self):
        result = _expected_goal_models(
            home_history=self._history(xg_matches=3),
            away_history=self._history(xg_matches=3),
            dixon_coles_rho=-0.08,
            home_advantage_multiplier=1.08,
        )
        for model in (MODEL_POISSON_GOALS, MODEL_DC_GOALS, MODEL_POISSON_XG, MODEL_DC_XG):
            self.assertIsNotNone(result[model])
            self.assertAlmostEqual(sum(result[model].values()), 1.0, places=6)

    def test_fixture_payload_defines_xga_as_opponent_historical_xg(self):
        fixture = SimpleNamespace(
            starts_at=datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc),
            home_team="Home",
            away_team="Away",
        )
        statistics = [
            {"type": {"name": "Goals"}, "location": "home", "value": 2, "participant_id": 1},
            {"type": {"name": "Goals"}, "location": "away", "value": 1, "participant_id": 2},
        ]
        xg = [
            {"type": {"name": "Expected Goals (xG)"}, "location": "home", "value": 1.8, "participant_id": 1},
            {"type": {"name": "Expected Goals (xG)"}, "location": "away", "value": 0.7, "participant_id": 2},
        ]
        snapshot = SimpleNamespace(statistics=statistics, xg=xg)
        payload = _fixture_payload(fixture, snapshot)
        self.assertEqual(payload["actual"], "1")
        self.assertAlmostEqual(payload["home_observation"]["xg_for"], 1.8)
        self.assertAlmostEqual(payload["home_observation"]["xg_against"], 0.7)
        self.assertAlmostEqual(payload["away_observation"]["xg_for"], 0.7)
        self.assertAlmostEqual(payload["away_observation"]["xg_against"], 1.8)

    def test_form10_is_diagnostic_not_arbitrary_probability_model(self):
        rows = [
            {
                "actual": "1",
                "models": {MODEL_STANDARD: {"1": 0.5, "X": 0.25, "2": 0.25}},
                "context_audit": {"form10_ready": True},
            },
            {
                "actual": "2",
                "models": {MODEL_STANDARD: {"1": 0.4, "X": 0.3, "2": 0.3}},
                "context_audit": {"form10_ready": False},
            },
        ]
        diagnostic = _form10_diagnostic(rows)
        self.assertEqual(diagnostic["eligible_targets"], 1)
        self.assertEqual(diagnostic["coverage_pct"], 50.0)
        self.assertTrue(diagnostic["policy"]["form10_is_not_converted_into_an_arbitrary_probability_model"])


class EnigmaRatingV2EvaluationContractTests(unittest.TestCase):
    def test_baseline_predictions_are_partitioned_and_ordered(self):
        baseline = {
            "predictions": {
                "validation": [
                    {
                        "fixture_id": 2,
                        "sportmonks_fixture_id": 102,
                        "starts_at": "2026-01-02T12:00:00+00:00",
                        "league": "Serie A",
                        "home_team": "B",
                        "away_team": "C",
                        "actual": "X",
                        "probabilities": {"1": 0.3, "X": 0.4, "2": 0.3},
                    }
                ],
                "test": [
                    {
                        "fixture_id": 3,
                        "sportmonks_fixture_id": 103,
                        "starts_at": "2026-01-03T12:00:00+00:00",
                        "league": "Serie A",
                        "home_team": "C",
                        "away_team": "D",
                        "actual": "2",
                        "probabilities": {"1": 0.2, "X": 0.3, "2": 0.5},
                    }
                ],
            }
        }
        targets = _targets_from_baseline(baseline)
        self.assertEqual([row["partition"] for row in targets], ["validation", "test"])
        self.assertEqual(targets[0]["models"][MODEL_STANDARD]["X"], 0.4)

    def test_route_and_production_wrapper_contract_is_static_and_stable(self):
        evaluation_source = Path("app/enigma_rating_v2_evaluation.py").read_text(encoding="utf-8")
        main_source = Path("app/main_v017.py").read_text(encoding="utf-8")
        self.assertIn('@router.get("/evaluation-v1")', evaluation_source)
        self.assertIn('app.version = "0.53.0"', main_source)
        self.assertIn("from app.enigma_rating_v2_evaluation import router as enigma_rating_v2_evaluation_router", main_source)
        self.assertIn("app.include_router(enigma_rating_v2_evaluation_router)", main_source)
        self.assertIn('"same_timestamp_results_never_feed_other_same_timestamp_targets": True', evaluation_source)
        self.assertIn('"production_standard_model_unchanged": True', evaluation_source)
        self.assertNotIn("session.add(", evaluation_source)
        self.assertNotIn("session.commit(", evaluation_source)


if __name__ == "__main__":
    unittest.main()
