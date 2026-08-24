from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.daily_prediction_runner_v2 import _run_health
from app.decision_engine_v2 import _candidate_rank_key, _candidate_timing_audit


class DecisionEngineV2PolicyTests(unittest.TestCase):
    def test_bet_candidate_outranks_higher_ev_failed_candidate(self) -> None:
        failed = {
            "decision": "NO_BET",
            "bookmaker": "A",
            "market_name": "1X2",
            "value": {"expected_value_decimal": 0.20, "edge_probability_points": 0.12},
            "market": {"overround": 0.15},
        }
        valid = {
            "decision": "BET",
            "bookmaker": "B",
            "market_name": "1X2",
            "value": {"expected_value_decimal": 0.05, "edge_probability_points": 0.06},
            "market": {"overround": 0.06},
        }
        ranked = sorted([failed, valid], key=_candidate_rank_key)
        self.assertIs(ranked[0], valid)

    def test_higher_ev_wins_inside_same_decision_class(self) -> None:
        low = {
            "decision": "BET",
            "bookmaker": "A",
            "market_name": "1X2",
            "value": {"expected_value_decimal": 0.04, "edge_probability_points": 0.07},
            "market": {"overround": 0.05},
        }
        high = {
            "decision": "BET",
            "bookmaker": "B",
            "market_name": "1X2",
            "value": {"expected_value_decimal": 0.08, "edge_probability_points": 0.08},
            "market": {"overround": 0.07},
        }
        ranked = sorted([low, high], key=_candidate_rank_key)
        self.assertIs(ranked[0], high)

    def test_quote_window_rejects_any_three_way_quote_before_j1(self) -> None:
        candidate = {
            "latest_quote_fetched_at": "2026-08-24T16:45:10+00:00",
            "quote_span_seconds": 20,
        }
        audit = _candidate_timing_audit(
            candidate,
            quote_not_before=datetime(2026, 8, 24, 16, 45, tzinfo=timezone.utc),
            quote_not_after=datetime(2026, 8, 24, 17, 30, tzinfo=timezone.utc),
        )
        self.assertFalse(audit["eligible"])
        self.assertIn("QUOTE_BEFORE_REQUIRED_WINDOW", audit["reason_codes"])


class RunnerHealthTests(unittest.TestCase):
    def test_idle_when_no_fixture_due(self) -> None:
        self.assertEqual(_run_health([])["status"], "IDLE")

    def test_failed_on_integrity_failure(self) -> None:
        health = _run_health([{"status": "prediction_timing_invalid"}])
        self.assertEqual(health["status"], "FAILED")

    def test_degraded_on_not_ready(self) -> None:
        health = _run_health([{"status": "decision_not_ready"}])
        self.assertEqual(health["status"], "DEGRADED")


if __name__ == "__main__":
    unittest.main()
