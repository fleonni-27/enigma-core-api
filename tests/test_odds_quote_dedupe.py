from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app import odds_ingestion as odds


class _FakeSession:
    def __init__(self, fixture):
        self.fixture = fixture
        self.added = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def scalar(self, _statement):
        return self.fixture

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.committed = True


def _raw(value: str, *, updated: str = "2026-08-24T20:00:00Z") -> dict:
    return {
        "value": value,
        "label": "Home",
        "bookmaker": {"name": "Book A"},
        "market": {"name": "Fulltime Result"},
        "last_update": updated,
    }


class OddsQuoteDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = SimpleNamespace(id=10, sportmonks_id=999)

    def _run(self, payload, latest):
        fake = _FakeSession(self.fixture)
        with (
            patch.object(odds, "SessionLocal", return_value=fake),
            patch.object(odds, "_lock_fixture_window"),
            patch.object(odds, "_latest_stream_state", return_value=latest),
        ):
            result = odds.ingest_prematch_odds_payload(
                sportmonks_fixture_id=999,
                payload={"data": payload},
                snapshot_window="j1_45m_20260824",
            )
        return result, fake

    def test_same_price_refreshes_existing_state_without_new_row(self) -> None:
        first_seen = datetime.now(timezone.utc) - timedelta(minutes=10)
        previous_fetched = datetime.now(timezone.utc) - timedelta(minutes=5)
        previous = SimpleNamespace(
            odd=Decimal("1.8500"),
            first_seen_at=first_seen,
            fetched_at=previous_fetched,
            observation_count=3,
            source_updated_at=previous_fetched,
        )
        key = odds._quote_key("Book A", "Fulltime Result", "Home")

        result, fake = self._run([_raw("1.85001")], {key: previous})

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["deduplicated_unchanged"], 1)
        self.assertEqual(result["storage_rows_avoided"], 1)
        self.assertEqual(fake.added, [])
        self.assertEqual(previous.first_seen_at, first_seen)
        self.assertGreater(previous.fetched_at, previous_fetched)
        self.assertEqual(previous.observation_count, 4)
        self.assertTrue(fake.committed)

    def test_repeated_identical_quotes_inside_same_payload_collapse(self) -> None:
        result, fake = self._run([_raw("1.85"), _raw("1.8500")], {})

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["initial_states_created"], 1)
        self.assertEqual(result["movements_created"], 0)
        self.assertEqual(result["deduplicated_unchanged"], 1)
        self.assertEqual(len(fake.added), 1)
        state = fake.added[0]
        self.assertEqual(state.odd, Decimal("1.8500"))
        self.assertEqual(state.observation_count, 2)
        self.assertEqual(state.first_seen_at, state.fetched_at)

    def test_price_return_after_real_move_is_preserved(self) -> None:
        result, fake = self._run(
            [_raw("1.85"), _raw("1.87"), _raw("1.85")],
            {},
        )

        self.assertEqual(result["created"], 3)
        self.assertEqual(result["initial_states_created"], 1)
        self.assertEqual(result["movements_created"], 2)
        self.assertEqual(result["deduplicated_unchanged"], 0)
        self.assertEqual(
            [row.odd for row in fake.added],
            [Decimal("1.8500"), Decimal("1.8700"), Decimal("1.8500")],
        )

    def test_dedupe_lock_is_stable_and_window_scoped(self) -> None:
        first = odds._dedupe_lock_key(10, "daily_20260824")
        self.assertEqual(first, odds._dedupe_lock_key(10, "daily_20260824"))
        self.assertNotEqual(first, odds._dedupe_lock_key(10, "j1_45m_20260824"))
        self.assertNotEqual(first, odds._dedupe_lock_key(11, "daily_20260824"))


if __name__ == "__main__":
    unittest.main()
