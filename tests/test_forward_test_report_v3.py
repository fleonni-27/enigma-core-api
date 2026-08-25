from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.forward_test_report_v3 import (
    _aggregate_v3,
    _clv_quality,
    _diagnostic_readiness,
    _probability_bucket,
    _probability_quality,
    _quantile,
)


class ForwardTestReportV3Tests(unittest.TestCase):
    def _record(
        self,
        *,
        record_id: int,
        actual_result: str = "1",
        raw: dict | None = None,
        decision: str = "BET",
        selection: str = "1",
        pnl: float | None = 1.0,
        odd: float = 2.0,
        confidence: float = 0.60,
        starts_at: datetime | None = None,
        settled: bool = True,
    ):
        starts_at = starts_at or datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
        return SimpleNamespace(
            id=record_id,
            settlement_status="SETTLED" if settled else "UNSETTLED",
            actual_result=actual_result if settled else None,
            raw_probabilities=raw or {"1": 0.60, "X": 0.20, "2": 0.20},
            decision=decision,
            selection=selection,
            hypothetical_pnl_units=pnl if settled else None,
            selected_odd=odd,
            calibrated_favorite_confidence=confidence,
            fixture_starts_at=starts_at,
            edge_percentage_points=6.0,
            league="Serie A",
            bookmaker="Book A",
        )

    def _clv(
        self,
        *,
        odds_pct: float = 4.0,
        probability_pp: float = 2.0,
        closing_quote_fetched_at: datetime,
    ):
        return SimpleNamespace(
            clv_odds_pct=odds_pct,
            clv_probability_pp=probability_pp,
            closing_quote_fetched_at=closing_quote_fetched_at,
        )

    def test_probability_quality_reports_skill_against_uniform_and_climatology(self) -> None:
        records = [
            self._record(record_id=1, actual_result="1", raw={"1": 0.60, "X": 0.20, "2": 0.20}),
            self._record(record_id=2, actual_result="X", selection="X", raw={"1": 0.20, "X": 0.60, "2": 0.20}),
            self._record(record_id=3, actual_result="2", selection="2", raw={"1": 0.20, "X": 0.20, "2": 0.60}),
        ]
        quality, observations = _probability_quality(records)
        self.assertEqual(quality["sample_size"], 3)
        self.assertAlmostEqual(quality["brier_multiclass"], 0.24, places=6)
        self.assertAlmostEqual(quality["log_loss"], -math.log(0.60), places=6)
        self.assertAlmostEqual(quality["accuracy"], 1.0, places=6)
        self.assertGreater(quality["skill_vs_uniform"]["brier_skill"], 0.0)
        self.assertGreater(quality["skill_vs_uniform"]["log_loss_skill"], 0.0)
        self.assertAlmostEqual(quality["empirical_climatology"]["brier_multiclass"], 2 / 3, places=6)
        self.assertEqual(len(observations), 3)

    def test_probability_buckets_are_fixed_10pp_bins(self) -> None:
        self.assertEqual(_probability_bucket(0.0), "0-<10%")
        self.assertEqual(_probability_bucket(0.199), "10-<20%")
        self.assertEqual(_probability_bucket(0.20), "20-<30%")
        self.assertEqual(_probability_bucket(0.899), "80-<90%")
        self.assertEqual(_probability_bucket(0.90), ">=90%")
        self.assertEqual(_probability_bucket(1.0), ">=90%")

    def test_clv_coverage_uses_only_bets_whose_kickoff_is_due(self) -> None:
        as_of = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
        due_start = as_of - timedelta(hours=1)
        future_start = as_of + timedelta(hours=1)
        due = self._record(record_id=1, starts_at=due_start)
        future = self._record(record_id=2, starts_at=future_start, settled=False)
        clv = self._clv(closing_quote_fetched_at=due_start - timedelta(minutes=5))

        quality = _clv_quality([(due, clv), (future, None)], as_of=as_of)
        self.assertEqual(quality["bet_records"], 2)
        self.assertEqual(quality["clv_due_bet_records"], 1)
        self.assertEqual(quality["finalized_clv_records"], 1)
        self.assertEqual(quality["missing_finalized_clv_records"], 0)
        self.assertEqual(quality["finalized_coverage_rate"], 1.0)
        self.assertEqual(quality["odds_clv"]["average"], 4.0)
        self.assertEqual(quality["closing_quote_lead_minutes"]["median"], 5.0)
        self.assertEqual(quality["closing_quote_lead_minutes"]["post_kickoff_count"], 0)

    def test_quantile_uses_linear_interpolation(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(_quantile(values, 0.25), 1.75)
        self.assertAlmostEqual(_quantile(values, 0.50), 2.50)
        self.assertAlmostEqual(_quantile(values, 0.75), 3.25)

    def test_readiness_marks_incomplete_clv_as_partial_data(self) -> None:
        readiness = _diagnostic_readiness(
            settled_records=40,
            settled_bets=40,
            probability_sample_size=40,
            calibration_sample_size=40,
            clv_due_bets=40,
            clv_coverage_rate=0.50,
        )
        self.assertEqual(readiness["status"], "PARTIAL_DATA")
        self.assertIn("CLV_COVERAGE_INCOMPLETE", readiness["reason_codes"])

    def test_aggregate_scorecard_surfaces_core_metrics(self) -> None:
        as_of = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
        starts_at = as_of - timedelta(hours=2)
        record = self._record(
            record_id=1,
            starts_at=starts_at,
            raw={"1": 0.60, "X": 0.20, "2": 0.20},
        )
        clv = self._clv(
            odds_pct=3.5,
            probability_pp=1.2,
            closing_quote_fetched_at=starts_at - timedelta(minutes=4),
        )
        aggregate = _aggregate_v3([(record, clv)], as_of=as_of, include_curves=True)
        scorecard = aggregate["scorecard"]
        self.assertEqual(scorecard["roi_pct"], 100.0)
        self.assertEqual(scorecard["average_clv_odds_pct"], 3.5)
        self.assertEqual(scorecard["clv_coverage_rate"], 1.0)
        self.assertAlmostEqual(scorecard["brier_multiclass"], 0.24, places=6)
        self.assertIsNotNone(scorecard["log_loss"])
        self.assertIsNotNone(scorecard["predicted_class_ece_pp"])


if __name__ == "__main__":
    unittest.main()
