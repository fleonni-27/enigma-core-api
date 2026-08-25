from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app import enigma_rating_v2_tuning as tuning


class EnigmaRatingV2TuningContractTests(unittest.TestCase):
    def test_future_holdout_is_hard_boundary_for_tuning(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirmation holdout"):
            tuning.build_enigma_rating_v2_tuning_v1(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 8, 25),
            )

    def test_grid_sizes_are_explicit_and_bounded(self) -> None:
        self.assertEqual(
            len(tuning.ELO_K_GRID)
            * len(tuning.ELO_HOME_ADVANTAGE_GRID)
            * len(tuning.ELO_DRAW_PARAMETER_GRID),
            125,
        )
        self.assertEqual(len(tuning.DIXON_COLES_RHO_GRID), 9)
        self.assertEqual(tuning.CONFIRMATION_HOLDOUT_START.isoformat(), "2026-08-25")
        self.assertEqual(tuning.CONFIRMATION_MIN_TARGETS, 100)

    def test_brier_is_primary_and_log_loss_is_tie_break(self) -> None:
        better_brier = {
            "parameters": {"x": 1},
            "validation": {"brier_multiclass": 0.60, "log_loss": 1.20},
        }
        better_log_loss_only = {
            "parameters": {"x": 2},
            "validation": {"brier_multiclass": 0.61, "log_loss": 0.90},
        }
        ranked = tuning._top_candidates(
            [better_log_loss_only, better_brier], "validation", limit=2
        )
        self.assertEqual(ranked[0]["parameters"]["x"], 1)


class EnigmaRatingV2TuningFunctionalTests(unittest.TestCase):
    def test_elo_grid_scores_only_after_minimum_history(self) -> None:
        league_key = "BRA_SERIE_A"
        home = "Home"
        away = "Away"
        groups = []
        for index in range(5):
            groups.append(
                [
                    {
                        "fixture_id": index + 1,
                        "starts_at": datetime(2026, 1, index + 1, tzinfo=timezone.utc),
                        "league_key": league_key,
                        "home_team": home,
                        "away_team": away,
                        "home_score": 1.0 if index % 2 == 0 else 0.0,
                    }
                ]
            )
        target_fixture_id = 99
        groups.append(
            [
                {
                    "fixture_id": target_fixture_id,
                    "starts_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
                    "league_key": league_key,
                    "home_team": home,
                    "away_team": away,
                    "home_score": 1.0,
                }
            ]
        )
        targets = [
            {
                "fixture_id": target_fixture_id,
                "actual": "1",
            }
        ]
        candidates = tuning._elo_grid_search(groups, targets)
        self.assertEqual(len(candidates), 125)
        self.assertTrue(
            all(candidate["validation"]["sample_size"] == 1 for candidate in candidates)
        )

    def test_dixon_coles_grid_reports_goals_and_xg_separately(self) -> None:
        contexts = [
            {
                "fixture_id": 1,
                "actual": "1",
                "league": "Serie A",
                "goals_expected": (1.5, 0.9),
                "xg_expected": (1.7, 0.8),
            },
            {
                "fixture_id": 2,
                "actual": "X",
                "league": "Serie A",
                "goals_expected": (1.1, 1.0),
                "xg_expected": None,
            },
        ]
        candidates = tuning._dixon_coles_grid_search(contexts)
        self.assertEqual(len(candidates), 9)
        for candidate in candidates:
            self.assertEqual(candidate["validation_goals_only"]["sample_size"], 2)
            self.assertEqual(candidate["validation_xg_xga_secondary"]["sample_size"], 1)


if __name__ == "__main__":
    unittest.main()
