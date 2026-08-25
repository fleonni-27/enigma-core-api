from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.daily_operations import _fetch_daily_odds_payloads
from app.daily_prediction_runner_v2 import _fetch_j1_upstream
from app.j1_capacity import (
    HARD_MAX_J1_FIXTURES,
    J1_FIXTURE_STAGES,
    J1_MAX_FIXTURES_ENV,
    activate_j1_runner_capacity,
    configured_j1_max_fixtures,
)


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

    def test_j1_load_matrix_5_10_20_respects_global_concurrency(self) -> None:
        for fixture_count in J1_FIXTURE_STAGES:
            with self.subTest(fixture_count=fixture_count):
                client = _FakeSportmonksClient()
                fixtures = [
                    SimpleNamespace(sportmonks_id=1000 + index)
                    for index in range(fixture_count)
                ]

                payloads, audit = asyncio.run(
                    _fetch_j1_upstream(client, fixtures, concurrency=4)
                )

                self.assertEqual(len(payloads), fixture_count)
                self.assertEqual(audit["fixture_count"], fixture_count)
                self.assertEqual(audit["request_count"], fixture_count * 2)
                self.assertLessEqual(client.max_active, 4)
                self.assertTrue(
                    all(
                        result[kind]["status"] == "ok"
                        for result in payloads.values()
                        for kind in ("enriched", "odds")
                    )
                )

                # The fake upstream sleeps 10 ms per request. A serial 20-fixture
                # cycle would take about 400 ms for 40 requests; concurrency=4
                # should keep the synthetic prefetch safely below 300 ms while
                # leaving generous CI scheduling headroom.
                if fixture_count == HARD_MAX_J1_FIXTURES:
                    self.assertLess(audit["prefetch_seconds"], 0.30)

    def test_j1_capacity_accepts_only_rollout_stages(self) -> None:
        for stage in J1_FIXTURE_STAGES:
            with self.subTest(stage=stage), patch.dict(
                os.environ,
                {J1_MAX_FIXTURES_ENV: str(stage)},
            ):
                self.assertEqual(configured_j1_max_fixtures(), stage)

        for invalid in (0, 7, 21, 100):
            with self.subTest(invalid=invalid), patch.dict(
                os.environ,
                {J1_MAX_FIXTURES_ENV: str(invalid)},
            ):
                with self.assertRaises(ValueError):
                    configured_j1_max_fixtures()

    def test_j1_capacity_activation_raises_only_hard_ceiling(self) -> None:
        from app import daily_prediction_runner_v2 as runner_module

        original_hard_max = runner_module.MAX_FIXTURES_PER_RUN
        try:
            with patch.dict(os.environ, {J1_MAX_FIXTURES_ENV: "10"}):
                audit = activate_j1_runner_capacity()
                self.assertEqual(audit["configured_max_fixtures"], 10)
                self.assertEqual(audit["hard_max_fixtures"], 20)
                self.assertEqual(runner_module.MAX_FIXTURES_PER_RUN, 20)
        finally:
            runner_module.MAX_FIXTURES_PER_RUN = original_hard_max


if __name__ == "__main__":
    unittest.main()