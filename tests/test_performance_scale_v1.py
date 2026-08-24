from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from app.daily_operations import _fetch_daily_odds_payloads
from app.daily_prediction_runner_v2 import _fetch_j1_upstream


class _FakeSportmonksClient:
    def __init__(self, *, fail_enriched: set[int] | None = None) -> None:
        self.fail_enriched = fail_enriched or set()
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[str, int]] = []

    async def _enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)

    def _exit(self) -> None:
        self.active -= 1

    async def prematch_odds_by_fixture(self, fixture_id: int) -> dict:
        self.calls.append(("odds", fixture_id))
        await self._enter()
        try:
            return {"data": [{"fixture_id": fixture_id}]}
        finally:
            self._exit()

    async def enriched_fixture(self, fixture_id: int) -> dict:
        self.calls.append(("enriched", fixture_id))
        await self._enter()
        try:
            if fixture_id in self.fail_enriched:
                raise RuntimeError("upstream failure")
            return {"data": {"id": fixture_id, "lineups": []}}
        finally:
            self._exit()


class PerformanceScaleV1Tests(unittest.TestCase):
    def test_daily_odds_fetch_respects_concurrency_cap(self) -> None:
        client = _FakeSportmonksClient()
        fixture_ids = [101, 102, 103, 104, 105]

        results, audit = asyncio.run(
            _fetch_daily_odds_payloads(client, fixture_ids, concurrency=2)
        )

        self.assertEqual(set(results), set(fixture_ids))
        self.assertTrue(all(item["status"] == "ok" for item in results.values()))
        self.assertLessEqual(client.max_active, 2)
        self.assertEqual(audit["concurrency"], 2)
        self.assertEqual(audit["requested_fixtures"], 5)

    def test_j1_prefetch_fetches_lineup_and_odds_for_every_fixture(self) -> None:
        client = _FakeSportmonksClient()
        fixtures = [
            SimpleNamespace(sportmonks_id=201),
            SimpleNamespace(sportmonks_id=202),
            SimpleNamespace(sportmonks_id=203),
        ]

        payloads, audit = asyncio.run(
            _fetch_j1_upstream(client, fixtures, concurrency=3)
        )

        self.assertEqual(set(payloads), {201, 202, 203})
        for fixture_id in (201, 202, 203):
            self.assertEqual(payloads[fixture_id]["enriched"]["status"], "ok")
            self.assertEqual(payloads[fixture_id]["odds"]["status"], "ok")
        self.assertEqual(audit["request_count"], 6)
        self.assertLessEqual(client.max_active, 3)

    def test_j1_prefetch_isolates_single_upstream_failure(self) -> None:
        client = _FakeSportmonksClient(fail_enriched={302})
        fixtures = [
            SimpleNamespace(sportmonks_id=301),
            SimpleNamespace(sportmonks_id=302),
        ]

        payloads, _ = asyncio.run(_fetch_j1_upstream(client, fixtures, concurrency=2))

        self.assertEqual(payloads[301]["enriched"]["status"], "ok")
        self.assertEqual(payloads[302]["enriched"]["status"], "upstream_failed")
        self.assertEqual(payloads[302]["odds"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
