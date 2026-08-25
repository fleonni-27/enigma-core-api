from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from app.forward_test_report_v2 import (
    _aggregate_pairs,
    _confidence_bucket,
    _edge_bucket,
    _probability_score,
)


class ForwardTestReportV2Tests(unittest.TestCase):
    def _record(
        self,
        *,
        record_id: int,
        decision: str = "BET",
        actual_result: str = "1",
        selection: str = "1",
        pnl: float | None = None,
        odd: float = 2.0,
        confidence: float = 0.60,
        edge_pp: float = 6.0,
        raw: dict | None = None,
        settled: bool = True,
        league: str = "Serie A",
        bookmaker: str = "Book A",
    ):
        return SimpleNamespace(
            id=record_id,
            settlement_status="SETTLED" if settled else "UNSETTLED",
            actual_result=actual_result if settled else None,
            decision=decision,
            selection=selection,
            hypothetical_pnl_units=pnl,
            selected_odd=odd,
            calibrated_favorite_confidence=confidence,
            edge_percentage_points=edge_pp,
            raw_probabilities=raw or {"1": 0.60, "X": 0.20, "2": 0.20},
            league=league,
            bookmaker=bookmaker,
        )

    def _clv(self, *, odds_pct: float, probability_pp: float = 2.0):
        return SimpleNamespace(
            clv_odds_pct=odds_pct,
            clv_probability_pp=probability_pp,
            clv_odds_decimal=odds_pct / 100.0,
        )

    def test_multiclass_brier_and_log_loss_use_actual_result(self) -> None:
        record = self._record(
            record_id=1,
            raw={"1": 0.60, "X": 0.20, "2": 0.20},
            actual_result="1",
            pnl=1.0,
        )
        score = _probability_score(record)
        self.assertIsNotNone(score)
        self.assertAlmostEqual(score["brier"], 0.24, places=9)
        self.assertAlmostEqual(score["log_loss"], -math.log(0.60), places=9)
        self.assertEqual(score["correct"], 1.0)

    def test_probability_score_rejects_invalid_probability_sum(self) -> None:
        record = self._record(
            record_id=1,
            raw={"1": 0.90, "X": 0.90, "2": 0.90},
            pnl=1.0,
        )
        self.assertIsNone(_probability_score(record))

    def test_roi_uses_one_unit_per_settled_bet_only(self) -> None:
        win = self._record(record_id=1, pnl=1.5, odd=2.5, actual_result="1", selection="1")
        loss = self._record(record_id=2, pnl=-1.0, odd=2.0, actual_result="2", selection="1")
        no_bet = self._record(record_id=3, decision="NO_BET", pnl=0.0, actual_result="1", selection="1")
        report = _aggregate_pairs([(win, None), (loss, None), (no_bet, None)], include_calibration_curve=True)
        self.assertEqual(report["economics"]["stake_units"], 2.0)
        self.assertEqual(report["economics"]["pnl_units"], 0.5)
        self.assertEqual(report["economics"]["roi_pct"], 25.0)
        self.assertEqual(report["economics"]["wins"], 1)
        self.assertEqual(report["economics"]["losses"], 1)

    def test_clv_is_computed_on_bet_records_and_reports_coverage(self) -> None:
        first = self._record(record_id=1, pnl=1.0)
        second = self._record(record_id=2, pnl=-1.0)
        third = self._record(record_id=3, pnl=-1.0)
        report = _aggregate_pairs(
            [
                (first, self._clv(odds_pct=5.0, probability_pp=4.0)),
                (second, self._clv(odds_pct=-2.0, probability_pp=-1.0)),
                (third, None),
            ],
            include_calibration_curve=False,
        )
        self.assertEqual(report["clv"]["sample_size"], 2)
        self.assertAlmostEqual(report["clv"]["coverage_rate"], 2 / 3, places=6)
        self.assertEqual(report["clv"]["average_clv_odds_pct"], 1.5)
        self.assertEqual(report["clv"]["median_clv_odds_pct"], 1.5)
        self.assertEqual(report["clv"]["positive_clv_rate"], 0.5)
        self.assertEqual(report["clv"]["average_clv_probability_pp"], 1.5)

    def test_calibration_reports_ece_mce_and_binary_brier(self) -> None:
        first = self._record(record_id=1, pnl=1.0, confidence=0.70, actual_result="1", selection="1")
        second = self._record(record_id=2, pnl=-1.0, confidence=0.60, actual_result="2", selection="1")
        report = _aggregate_pairs([(first, None), (second, None)], include_calibration_curve=True)
        calibration = report["calibration"]
        self.assertEqual(calibration["sample_size"], 2)
        self.assertAlmostEqual(calibration["average_confidence"], 0.65, places=6)
        self.assertAlmostEqual(calibration["observed_success_rate"], 0.50, places=6)
        self.assertAlmostEqual(calibration["binary_brier"], 0.225, places=6)
        self.assertAlmostEqual(calibration["ece"], 0.45, places=6)
        self.assertAlmostEqual(calibration["mce"], 0.60, places=6)
        self.assertEqual(len(calibration["curve"]), 2)

    def test_unsettled_record_is_excluded_from_scores_and_roi(self) -> None:
        record = self._record(record_id=1, settled=False, pnl=None)
        report = _aggregate_pairs([(record, self._clv(odds_pct=3.0))], include_calibration_curve=True)
        self.assertEqual(report["sample"]["settled_records"], 0)
        self.assertEqual(report["sample"]["probability_score_eligible"], 0)
        self.assertEqual(report["economics"]["stake_units"], 0.0)
        self.assertIsNone(report["economics"]["roi_pct"])
        self.assertEqual(report["clv"]["sample_size"], 1)

    def test_fixed_bucket_boundaries_are_deterministic(self) -> None:
        self.assertEqual(_edge_bucket(-0.1), "<0pp")
        self.assertEqual(_edge_bucket(2.5), "2.5-<5pp")
        self.assertEqual(_edge_bucket(5.0), "5-<7.5pp")
        self.assertEqual(_edge_bucket(10.0), ">=10pp")
        self.assertEqual(_confidence_bucket(0.449), "<45%")
        self.assertEqual(_confidence_bucket(0.45), "45-<50%")
        self.assertEqual(_confidence_bucket(0.60), "60-<65%")
        self.assertEqual(_confidence_bucket(0.65), ">=65%")


if __name__ == "__main__":
    unittest.main()
