from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.j1_scheduler import run_primary_operations_cycle
from app.models import Fixture
from app.odds_window_clv import (
    _clv_payload,
    _complete_1x2_markets,
    closing_window,
    daily_window,
    j1_window,
)


class OddsWindowCLVTests(unittest.IsolatedAsyncioTestCase):
    def _quote(self, *, id: int, selection: str, odd: float, minute: int, first_minute: int | None = None):
        fetched = datetime(2026, 8, 24, 22, minute, tzinfo=timezone.utc)
        first = datetime(2026, 8, 24, 22, first_minute if first_minute is not None else minute, tzinfo=timezone.utc)
        return SimpleNamespace(
            id=id,
            bookmaker="Book A",
            market="Fulltime Result",
            selection=selection,
            odd=odd,
            fetched_at=fetched,
            first_seen_at=first,
        )

    def test_window_names_use_sao_paulo_match_date(self) -> None:
        fixture = Fixture(
            sportmonks_id=1,
            home_team="Home FC",
            away_team="Away FC",
            starts_at=datetime(2026, 8, 25, 1, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(daily_window(fixture), "daily_20260824")
        self.assertEqual(j1_window(fixture), "j1_45m_20260824")
        self.assertEqual(closing_window(fixture), "closing_20260824")

    def test_complete_market_uses_latest_state_and_no_vig_sums_to_one(self) -> None:
        rows = [
            self._quote(id=1, selection="Home FC", odd=2.10, minute=0),
            self._quote(id=2, selection="Draw", odd=3.20, minute=0),
            self._quote(id=3, selection="Away FC", odd=3.60, minute=0),
            self._quote(id=4, selection="Home FC", odd=2.00, minute=4),
        ]
        markets = _complete_1x2_markets(rows, home_team="Home FC", away_team="Away FC", latest=True)
        self.assertEqual(len(markets), 1)
        market = markets[0]
        self.assertEqual(market["odds"]["1"], 2.00)
        self.assertAlmostEqual(sum(market["no_vig_probabilities"].values()), 1.0, places=9)
        self.assertTrue(market["coherent"])

    def test_opening_uses_earliest_observed_price_state(self) -> None:
        rows = [
            self._quote(id=1, selection="Home FC", odd=2.10, minute=5, first_minute=0),
            self._quote(id=2, selection="Draw", odd=3.20, minute=5, first_minute=0),
            self._quote(id=3, selection="Away FC", odd=3.60, minute=5, first_minute=0),
            self._quote(id=4, selection="Home FC", odd=1.95, minute=10, first_minute=10),
        ]
        markets = _complete_1x2_markets(
            rows,
            home_team="Home FC",
            away_team="Away FC",
            opening=True,
            latest=False,
        )
        self.assertEqual(markets[0]["odds"]["1"], 2.10)

    def test_positive_clv_is_better_decision_price_and_market_move_toward_selection(self) -> None:
        record = SimpleNamespace(
            id=10,
            fixture_id=20,
            sportmonks_fixture_id=30,
            decision="BET",
            selection="1",
            bookmaker="Book A",
            market_name="Fulltime Result",
            snapshot_window="j1_45m_20260824",
            fixture_starts_at=datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc),
            selected_odd=2.10,
            selected_no_vig_probability=0.48,
            calibrated_favorite_confidence=0.56,
        )
        market = {
            "odds": {"1": 2.00, "X": 3.30, "2": 3.80},
            "no_vig_probabilities": {"1": 0.52, "X": 0.28, "2": 0.20},
            "latest_quote_at": "2026-08-24T22:59:00+00:00",
        }
        payload = _clv_payload(record, market)
        self.assertIsNotNone(payload)
        self.assertAlmostEqual(payload["clv_odds_decimal"], 0.05, places=9)
        self.assertAlmostEqual(payload["clv_odds_pct"], 5.0, places=9)
        self.assertAlmostEqual(payload["clv_probability_points"], 0.04, places=9)
        self.assertAlmostEqual(payload["model_edge_vs_closing"], 0.04, places=9)
        self.assertTrue(payload["positive_clv"])

    async def test_closing_failure_does_not_break_valid_j1_cycle(self) -> None:
        j1 = {"status": "ok", "run_health": {"status": "OK"}, "counts": {}}
        with patch("app.j1_scheduler.run_j1_cycle", new=AsyncMock(return_value=j1)), patch(
            "app.odds_window_clv.run_odds_window_clv_cycle",
            new=AsyncMock(side_effect=RuntimeError("temporary")),
        ):
            result = await run_primary_operations_cycle()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["odds_window_clv"]["status"], "failed")
        self.assertEqual(result["run_health"]["status"], "OK")

    async def test_render_command_runs_closing_without_risking_j1(self) -> None:
        j1 = {"status": "ok", "run_health": {"status": "IDLE"}, "counts": {}}
        with patch("app.j1_scheduler.run_j1_cycle", new=AsyncMock(return_value=j1)), patch(
            "app.odds_window_clv.run_odds_window_clv_cycle",
            new=AsyncMock(return_value={"status": "ok", "version": "odds_window_clv_v1"}),
        ):
            result = await run_primary_operations_cycle()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["odds_window_clv"]["status"], "ok")
        self.assertEqual(result["run_health"]["status"], "IDLE")


if __name__ == "__main__":
    unittest.main()
