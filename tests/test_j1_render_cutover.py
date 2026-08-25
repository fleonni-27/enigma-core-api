from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from app import j1_scheduler


class J1RenderCutoverTests(unittest.IsolatedAsyncioTestCase):
    def test_execution_mode_defaults_to_batch_and_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                j1_scheduler.configured_j1_execution_mode(),
                j1_scheduler.J1_EXECUTION_MODE_BATCH,
            )

        with patch.dict(os.environ, {"J1_EXECUTION_MODE": "invalid"}, clear=False):
            with self.assertRaises(ValueError):
                j1_scheduler.configured_j1_execution_mode()

    async def test_batch_mode_uses_existing_primary_cycle(self) -> None:
        expected = {"status": "ok", "run_health": {"status": "IDLE"}}
        with (
            patch.dict(os.environ, {"J1_EXECUTION_MODE": "batch"}, clear=False),
            patch.object(
                j1_scheduler,
                "run_primary_operations_cycle",
                new=AsyncMock(return_value=expected),
            ) as batch_run,
        ):
            result = await j1_scheduler.run_render_cron_entrypoint()

        batch_run.assert_awaited_once()
        self.assertEqual(result["execution"]["mode"], "batch")

    async def test_producer_mode_delegates_to_canonical_producer(self) -> None:
        expected = {"status": "ok", "run_health": {"status": "IDLE"}}
        producer = AsyncMock(return_value=expected)
        with (
            patch.dict(os.environ, {"J1_EXECUTION_MODE": "producer"}, clear=False),
            patch("app.j1_work_producer.run_producer_cycle", new=producer),
            patch.object(
                j1_scheduler,
                "run_primary_operations_cycle",
                new=AsyncMock(side_effect=AssertionError("batch must not run")),
            ),
        ):
            result = await j1_scheduler.run_render_cron_entrypoint()

        producer.assert_awaited_once()
        self.assertEqual(result["execution"]["mode"], "producer")
        self.assertEqual(
            result["execution"]["canonical_producer_module"],
            "app.j1_work_producer",
        )

    def test_render_blueprint_has_three_workers_and_internal_secret_wiring(self) -> None:
        config = yaml.safe_load(Path("render.yaml").read_text())
        services = {service["name"]: service for service in config["services"]}

        worker = services["enigma-j1-worker"]
        self.assertEqual(worker["type"], "worker")
        self.assertEqual(worker["region"], "virginia")
        self.assertEqual(worker["numInstances"], 3)
        self.assertEqual(worker["startCommand"], "python -m app.j1_claim_worker")
        worker_env = {
            item["key"]: item
            for item in worker["envVars"]
            if "key" in item
        }
        for key in ("DATABASE_URL", "SPORTMONKS_API_TOKEN"):
            self.assertEqual(
                worker_env[key]["fromService"],
                {
                    "type": "web",
                    "name": "enigma-core-api",
                    "envVarKey": key,
                },
            )

        cron = services["enigma-j1-runner"]
        self.assertEqual(cron["startCommand"], "python -m app.j1_scheduler")
        cron_env = {
            item["key"]: item.get("value")
            for item in cron["envVars"]
            if "key" in item
        }
        self.assertEqual(cron_env["J1_EXECUTION_MODE"], "batch")


if __name__ == "__main__":
    unittest.main()
