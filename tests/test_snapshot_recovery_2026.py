from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import snapshot_recovery_2026 as recovery


class _FakeSportmonksClient:
    def __init__(self, payloads):
        self.payloads = payloads

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def enriched_fixture(self, fixture_id: int):
        value = self.payloads[fixture_id]
        if isinstance(value, Exception):
            raise value
        return value

    def transport_audit(self):
        return {"version": "test", "logical_requests": len(self.payloads)}


class SnapshotRecoveryProfileTests(unittest.TestCase):
    def test_profile_requires_lineups_and_statistics_for_training_core(self):
        profile = recovery._payload_profile(
            {
                "data": {
                    "lineups": [{"player_id": 1}],
                    "statistics": [{"type_id": 1}],
                    "xgfixture": [],
                }
            }
        )
        self.assertTrue(profile["has_any_enriched_data"])
        self.assertTrue(profile["training_core_present"])
        self.assertEqual(profile["lineups_count"], 1)
        self.assertEqual(profile["statistics_count"], 1)

    def test_empty_payload_is_not_recoverable(self):
        profile = recovery._payload_profile({"data": {}})
        self.assertFalse(profile["has_any_enriched_data"])
        self.assertFalse(profile["training_core_present"])


class SnapshotRecoveryRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_preserves_partial_evidence_without_calling_it_training_ready(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        candidates = [
            recovery.RecoveryCandidate(1, 101, "Serie A", now),
            recovery.RecoveryCandidate(2, 102, "Serie B", now),
            recovery.RecoveryCandidate(3, 103, "La Liga", now),
        ]
        remaining = [candidates[2]]
        payloads = {
            101: {"data": {"lineups": [{"id": 1}], "statistics": [{"id": 2}], "xgfixture": []}},
            102: {"data": {"lineups": [], "statistics": [{"id": 3}], "xgfixture": []}},
            103: {"data": {}},
        }
        fake_client = _FakeSportmonksClient(payloads)

        with (
            patch.object(recovery, "_missing_candidates", side_effect=[candidates, remaining]),
            patch.object(recovery, "SportmonksClient", return_value=fake_client),
            patch.object(
                recovery,
                "_persist_recovered_snapshot",
                side_effect=[
                    {"status": "created", "snapshot_id": 501},
                    {"status": "created", "snapshot_id": 502},
                ],
            ),
        ):
            result = await recovery.recover_missing_2026_snapshots(concurrency=2)

        self.assertEqual(result["selected_missing"], 3)
        self.assertEqual(result["recovered"], 2)
        self.assertEqual(result["recovered_training_core"], 1)
        self.assertEqual(result["recovered_incomplete"], 1)
        self.assertEqual(result["unrecoverable_empty_payload"], 1)
        self.assertEqual(result["remaining_missing"], 1)
        self.assertEqual(result["remaining_missing_by_league"], {"La Liga": 1})


if __name__ == "__main__":
    unittest.main()
