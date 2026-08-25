from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import j1_work_queue as queue
from app.j1_claim_worker import _classify_result
from app.models import Fixture


class J1WorkClaimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Fixture.__table__.create(self.engine)
        queue.J1WorkItem.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.session_patch = patch.object(queue, "SessionLocal", self.Session)
        self.session_patch.start()
        self.original_schema_ready = queue._schema_ready
        queue._schema_ready = True

        self.now = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        with self.Session() as session:
            fixture = Fixture(
                id=1,
                sportmonks_id=990001,
                league_name="Serie A",
                home_team="Home",
                away_team="Away",
                starts_at=self.now + timedelta(minutes=45),
                status="NS",
            )
            session.add(fixture)
            session.commit()
            session.expunge(fixture)
            self.fixture = fixture

    def tearDown(self) -> None:
        queue._schema_ready = self.original_schema_ready
        self.session_patch.stop()
        self.engine.dispose()

    def _insert_work(self, *, work_id: int = 1) -> None:
        with self.Session() as session:
            session.add(
                queue.J1WorkItem(
                    id=work_id,
                    fixture_id=1,
                    sportmonks_fixture_id=990001,
                    snapshot_window="j1_45m_20260825",
                    due_at=self.now,
                    kickoff_at=self.now + timedelta(minutes=45),
                    status=queue.STATUS_PENDING,
                    attempt_count=0,
                    available_at=self.now,
                    result_payload={},
                )
            )
            session.commit()

    def test_enqueue_is_idempotent_by_fixture_and_snapshot_window(self) -> None:
        with patch.object(
            queue,
            "select_pending_j1_fixtures",
            return_value=([self.fixture], {"selected_fixture_count": 1}),
        ):
            first = queue.enqueue_due_j1_work(
                now=self.now,
                max_lateness_minutes=20,
                max_fixtures=20,
            )
            second = queue.enqueue_due_j1_work(
                now=self.now,
                max_lateness_minutes=20,
                max_fixtures=20,
            )

        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(first["already_queued"], 0)
        self.assertEqual(second["enqueued"], 0)
        self.assertEqual(second["already_queued"], 1)
        with self.Session() as session:
            rows = list(session.scalars(select(queue.J1WorkItem)).all())
        self.assertEqual(len(rows), 1)

    def test_claim_token_blocks_stale_worker_completion(self) -> None:
        self._insert_work()
        first = queue.claim_next_j1_work(
            worker_id="worker-a",
            now=self.now,
            lease_seconds=30,
        )
        self.assertIsNotNone(first)
        self.assertEqual(first["attempt_count"], 1)

        no_second = queue.claim_next_j1_work(
            worker_id="worker-b",
            now=self.now + timedelta(seconds=10),
            lease_seconds=30,
        )
        self.assertIsNone(no_second)

        second = queue.claim_next_j1_work(
            worker_id="worker-b",
            now=self.now + timedelta(seconds=31),
            lease_seconds=30,
        )
        self.assertIsNotNone(second)
        self.assertEqual(second["attempt_count"], 2)
        self.assertNotEqual(first["claim_token"], second["claim_token"])

        stale_commit = queue.complete_j1_work(
            work_id=1,
            claim_token=str(first["claim_token"]),
            result_status="completed",
            now=self.now + timedelta(seconds=32),
        )
        self.assertFalse(stale_commit)

        live_commit = queue.complete_j1_work(
            work_id=1,
            claim_token=str(second["claim_token"]),
            result_status="completed",
            now=self.now + timedelta(seconds=32),
        )
        self.assertTrue(live_commit)

    def test_retry_waits_until_available_at_and_is_bounded(self) -> None:
        self._insert_work()
        claim = queue.claim_next_j1_work(
            worker_id="worker-a",
            now=self.now,
            lease_seconds=60,
        )
        transition = queue.fail_j1_work(
            work_id=1,
            claim_token=str(claim["claim_token"]),
            error="decision_not_ready",
            result_status="decision_not_ready",
            retryable=True,
            now=self.now,
            retry_delay_seconds=15,
            max_attempts=3,
        )
        self.assertEqual(transition["status"], queue.STATUS_RETRY)

        early = queue.claim_next_j1_work(
            worker_id="worker-b",
            now=self.now + timedelta(seconds=14),
            lease_seconds=60,
        )
        self.assertIsNone(early)

        retry = queue.claim_next_j1_work(
            worker_id="worker-b",
            now=self.now + timedelta(seconds=16),
            lease_seconds=60,
        )
        self.assertIsNotNone(retry)
        self.assertEqual(retry["attempt_count"], 2)

    def test_expire_prevents_post_kickoff_claim(self) -> None:
        self._insert_work()
        expired = queue.expire_past_kickoff_work(
            now=self.now + timedelta(minutes=46)
        )
        self.assertEqual(expired, 1)
        claim = queue.claim_next_j1_work(
            worker_id="worker-a",
            now=self.now + timedelta(minutes=46),
            lease_seconds=60,
        )
        self.assertIsNone(claim)

    def test_worker_result_classification(self) -> None:
        self.assertEqual(
            _classify_result({"status": "ok", "items": [{"status": "completed"}]}),
            (True, False, "completed"),
        )
        self.assertEqual(
            _classify_result(
                {"status": "ok", "items": [{"status": "decision_not_ready"}]}
            ),
            (False, True, "decision_not_ready"),
        )
        self.assertEqual(
            _classify_result(
                {"status": "ok", "items": [{"status": "prediction_timing_invalid"}]}
            ),
            (False, False, "prediction_timing_invalid"),
        )


if __name__ == "__main__":
    unittest.main()
