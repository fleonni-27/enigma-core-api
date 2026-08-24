from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from app.database import SessionLocal, engine
from app.models import Fixture, Prediction
from app.prediction_window_policy import (
    J1_RESERVED_OWNER,
    J1_RESERVED_WINDOW,
    ReservedPredictionWindowError,
    assert_prediction_window_write_allowed,
    authorized_prediction_producer,
    install_prediction_window_policy,
    quarantine_invalid_reserved_j1_predictions,
)


class PredictionWindowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Fixture.__table__.create(bind=engine, checkfirst=True)
        Prediction.__table__.create(bind=engine, checkfirst=True)
        install_prediction_window_policy()

    def setUp(self) -> None:
        with SessionLocal() as session:
            session.query(Prediction).delete()
            session.query(Fixture).delete()
            session.commit()

    def _fixture(self, sportmonks_id: int, starts_at: datetime) -> Fixture:
        with SessionLocal() as session:
            fixture = Fixture(
                id=sportmonks_id,
                sportmonks_id=sportmonks_id,
                league_name="La Liga",
                home_team=f"Home {sportmonks_id}",
                away_team=f"Away {sportmonks_id}",
                starts_at=starts_at,
                status="NS",
            )
            session.add(fixture)
            session.commit()
            session.refresh(fixture)
            session.expunge(fixture)
            return fixture

    def _prediction(
        self,
        fixture_id: int,
        generated_at: datetime,
        *,
        authorized: bool,
    ) -> int:
        with SessionLocal() as session:
            row = Prediction(
                id=fixture_id + 1_000_000,
                fixture_id=fixture_id,
                prediction_window=J1_RESERVED_WINDOW,
                model_version="baseline_1x2_temporal_v1",
                p_home=Decimal("0.500000"),
                p_draw=Decimal("0.250000"),
                p_away=Decimal("0.250000"),
                generated_at=generated_at,
            )
            session.add(row)
            if authorized:
                with authorized_prediction_producer(J1_RESERVED_OWNER):
                    session.commit()
            else:
                session.commit()
            session.refresh(row)
            return int(row.id)

    def test_manual_reserved_window_is_rejected(self) -> None:
        with self.assertRaises(ReservedPredictionWindowError):
            assert_prediction_window_write_allowed(J1_RESERVED_WINDOW)

    def test_authorized_j1_producer_can_write_reserved_window(self) -> None:
        with authorized_prediction_producer(J1_RESERVED_OWNER):
            assert_prediction_window_write_allowed(J1_RESERVED_WINDOW)

    def test_database_mapper_blocks_unauthorized_reserved_insert(self) -> None:
        fixture = self._fixture(910001, datetime(2026, 8, 24, 20, 45, tzinfo=timezone.utc))
        with self.assertRaises(ReservedPredictionWindowError):
            self._prediction(
                int(fixture.id),
                datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc),
                authorized=False,
            )

    def test_legacy_early_j1_is_quarantined_but_valid_j1_is_preserved(self) -> None:
        now = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
        early_fixture = self._fixture(
            910002,
            datetime(2026, 8, 24, 20, 45, tzinfo=timezone.utc),
        )
        valid_fixture = self._fixture(
            910003,
            datetime(2026, 8, 24, 20, 40, tzinfo=timezone.utc),
        )
        early_id = self._prediction(
            int(early_fixture.id),
            datetime(2026, 8, 24, 19, 30, tzinfo=timezone.utc),
            authorized=True,
        )
        valid_id = self._prediction(
            int(valid_fixture.id),
            datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc),
            authorized=True,
        )

        audit = quarantine_invalid_reserved_j1_predictions(
            now=now,
            prediction_window=J1_RESERVED_WINDOW,
            model_version="baseline_1x2_temporal_v1",
            target_lead_minutes=45,
            max_lateness_minutes=20,
        )

        self.assertEqual(audit["quarantined_count"], 1)
        with SessionLocal() as session:
            early = session.get(Prediction, early_id)
            valid = session.get(Prediction, valid_id)
            self.assertIsNotNone(early)
            self.assertIsNotNone(valid)
            self.assertTrue(str(early.prediction_window).startswith("invalid_j1_"))
            self.assertEqual(valid.prediction_window, J1_RESERVED_WINDOW)
            self.assertEqual(float(early.p_home), 0.5)


if __name__ == "__main__":
    unittest.main()
