from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.enigma_rating_v2_context import (
    _aggregate_team_history,
    _lineup_summary,
    _observation_from_snapshot,
)


class EnigmaRatingV2ContextTests(unittest.TestCase):
    def test_historical_observation_extracts_xg_for_and_xg_against(self) -> None:
        fixture = SimpleNamespace(
            home_team="Home FC",
            away_team="Away FC",
            starts_at=datetime(2026, 8, 1, 19, 0, tzinfo=timezone.utc),
        )
        statistics = [
            {"type": {"name": "Goals"}, "location": "home", "participant_id": 10, "value": 2},
            {"type": {"name": "Goals"}, "location": "away", "participant_id": 20, "value": 1},
        ]
        xg = [
            {"type": {"name": "Expected Goals (xG)"}, "location": "home", "participant_id": 10, "value": 1.72},
            {"type": {"name": "Expected Goals (xG)"}, "location": "away", "participant_id": 20, "value": 0.84},
        ]
        snapshot = SimpleNamespace(statistics=statistics, xg=xg)

        home = _observation_from_snapshot(fixture, snapshot, team_name="Home FC")
        away = _observation_from_snapshot(fixture, snapshot, team_name="Away FC")
        self.assertIsNotNone(home)
        self.assertIsNotNone(away)
        self.assertAlmostEqual(home["xg_for"], 1.72, places=8)
        self.assertAlmostEqual(home["xg_against"], 0.84, places=8)
        self.assertAlmostEqual(away["xg_for"], 0.84, places=8)
        self.assertAlmostEqual(away["xg_against"], 1.72, places=8)
        self.assertEqual(home["points"], 3.0)
        self.assertEqual(away["points"], 0.0)

    def test_aggregate_history_tracks_separate_xg_and_xga_coverage(self) -> None:
        now = datetime(2026, 8, 10, tzinfo=timezone.utc)
        rows = [
            {"starts_at": now, "points": 3.0, "goals_for": 2.0, "goals_against": 1.0, "xg_for": 1.6, "xg_against": 0.9},
            {"starts_at": now, "points": 1.0, "goals_for": 1.0, "goals_against": 1.0, "xg_for": 1.2, "xg_against": None},
        ]
        result = _aggregate_team_history(rows)
        self.assertEqual(result["history_matches"], 2)
        self.assertEqual(result["xg_for_history_matches"], 2)
        self.assertEqual(result["xg_against_history_matches"], 1)
        self.assertAlmostEqual(result["points_per_match"], 2.0, places=8)
        self.assertAlmostEqual(result["xg_for_avg"], 1.4, places=8)
        self.assertAlmostEqual(result["xg_against_avg"], 0.9, places=8)

    def test_lineup_summary_identifies_starters_without_inventing_impact(self) -> None:
        lineups = [
            {"player_id": 101, "team_id": 1, "type_id": 11},
            {"player_id": 102, "team_id": 1, "type_id": 11},
            {"player_id": 201, "team_id": 2, "type_id": 11},
            {"player_id": 202, "team_id": 2, "type_id": 12},
        ]
        result = _lineup_summary(lineups)
        self.assertEqual(result["starter_rows"], 3)
        self.assertEqual(result["starters_by_team_id"], {"1": 2, "2": 1})
        self.assertEqual(result["starter_player_ids"], [101, 102, 201])
        self.assertFalse(result["impact_scored"])
        self.assertEqual(result["impact_reason"], "PLAYER_ABSENCE_VALUE_MODEL_NOT_AVAILABLE")


if __name__ == "__main__":
    unittest.main()
