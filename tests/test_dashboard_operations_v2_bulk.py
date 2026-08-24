from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import dashboard_operations_v2 as legacy
from app import dashboard_operations_v2_bulk as bulk


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return False


class DashboardOperationsV2BulkTests(unittest.TestCase):
    def _fixture(self, fixture_id: int, sportmonks_id: int, starts_at: datetime):
        return SimpleNamespace(
            id=fixture_id,
            sportmonks_id=sportmonks_id,
            league_name="Serie A",
            home_team=f"Home {fixture_id}",
            away_team=f"Away {fixture_id}",
            starts_at=starts_at,
            status="NS",
        )

    def test_dashboard_uses_fixed_five_data_queries_for_multiple_fixtures(self) -> None:
        starts_at = datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc)
        fixtures = [
            self._fixture(1, 900001, starts_at),
            self._fixture(2, 900002, starts_at),
        ]
        fetched_at = datetime(2026, 8, 24, 22, 15, tzinfo=timezone.utc)
        window = "j1_45m_20260824"

        contexts = [
            SimpleNamespace(
                id=5,
                fixture_id=1,
                snapshot_window=window,
                lineup_count=22,
                fetched_at=fetched_at,
            )
        ]
        predictions = [
            SimpleNamespace(
                id=12,
                fixture_id=1,
                prediction_window="j1_45m_v1",
                p_home=Decimal("0.510000"),
                p_draw=Decimal("0.250000"),
                p_away=Decimal("0.240000"),
                generated_at=fetched_at,
            ),
            SimpleNamespace(
                id=11,
                fixture_id=1,
                prediction_window="j1_45m_v1",
                p_home=Decimal("0.490000"),
                p_draw=Decimal("0.260000"),
                p_away=Decimal("0.250000"),
                generated_at=datetime(2026, 8, 24, 22, 10, tzinfo=timezone.utc),
            ),
        ]
        decisions = [
            SimpleNamespace(
                id=21,
                fixture_id=1,
                decision="NO_BET",
                selection=None,
                reason_codes=["EDGE_BELOW_THRESHOLD"],
                bookmaker="Book A",
                selected_odd=Decimal("1.9000"),
                selected_no_vig_probability=Decimal("0.500000"),
                calibrated_favorite_confidence=Decimal("0.520000"),
                edge_percentage_points=Decimal("1.000"),
                expected_value_pct=Decimal("1.900"),
                raw_probabilities={"1": 0.51, "X": 0.25, "2": 0.24},
                recorded_at=fetched_at,
                settlement_status="UNSETTLED",
            )
        ]
        odds_rows = [
            (1, 120, fetched_at, 30, fetched_at),
            (2, 80, fetched_at, 0, None),
        ]

        session = MagicMock()
        session.scalars.side_effect = [
            _Rows(fixtures),
            _Rows(contexts),
            _Rows(predictions),
            _Rows(decisions),
        ]
        session.execute.return_value = _Rows(odds_rows)

        with (
            patch.object(bulk, "SessionLocal", return_value=_SessionContext(session)),
            patch.object(bulk, "ensure_forward_test_schema"),
            patch.object(bulk, "ensure_prematch_context_schema"),
        ):
            payload = bulk.build_dashboard_operations_v2_bulk(
                target_date=date(2026, 8, 24)
            )

        self.assertEqual(session.scalars.call_count, 4)
        self.assertEqual(session.execute.call_count, 1)
        self.assertEqual(payload["performance"]["data_select_query_count"], 5)
        self.assertEqual(payload["performance"]["per_fixture_query_count"], 0)
        self.assertFalse(
            payload["performance"]["query_count_scales_with_fixture_count"]
        )
        self.assertEqual(len(payload["fixtures"]), 2)
        self.assertEqual(
            payload["fixtures"][0]["steps"]["prediction"]["prediction_id"],
            12,
        )
        self.assertEqual(payload["fixtures"][0]["steps"]["daily_odds"]["rows"], 120)
        self.assertEqual(payload["fixtures"][0]["steps"]["j1_odds"]["rows"], 30)
        self.assertEqual(payload["fixtures"][1]["steps"]["j1_odds"]["rows"], 0)

    def test_empty_day_executes_only_fixture_query(self) -> None:
        session = MagicMock()
        session.scalars.return_value = _Rows([])

        with (
            patch.object(bulk, "SessionLocal", return_value=_SessionContext(session)),
            patch.object(bulk, "ensure_forward_test_schema"),
            patch.object(bulk, "ensure_prematch_context_schema"),
        ):
            payload = bulk.build_dashboard_operations_v2_bulk(
                target_date=date(2026, 8, 24)
            )

        self.assertEqual(session.scalars.call_count, 1)
        self.assertEqual(session.execute.call_count, 0)
        self.assertEqual(payload["performance"]["data_select_query_count"], 1)

    def test_installer_replaces_dashboard_builder_without_replacing_route(self) -> None:
        original = legacy.build_dashboard_operations_v2
        try:
            bulk.install_dashboard_operations_v2_bulk_reads()
            self.assertIs(
                legacy.build_dashboard_operations_v2,
                bulk.build_dashboard_operations_v2_bulk,
            )
        finally:
            legacy.build_dashboard_operations_v2 = original


if __name__ == "__main__":
    unittest.main()
