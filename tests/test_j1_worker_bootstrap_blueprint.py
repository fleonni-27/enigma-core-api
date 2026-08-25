from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class J1WorkerBootstrapBlueprintTests(unittest.TestCase):
    def test_bootstrap_contains_only_canonical_worker(self) -> None:
        canonical = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
        bootstrap = yaml.safe_load((ROOT / "render.worker.yaml").read_text(encoding="utf-8"))

        canonical_worker = next(
            service for service in canonical["services"]
            if service.get("name") == "enigma-j1-worker"
        )
        self.assertEqual(len(bootstrap["services"]), 1)
        worker = bootstrap["services"][0]
        self.assertEqual(worker["name"], "enigma-j1-worker")

        for key in (
            "type",
            "runtime",
            "region",
            "branch",
            "plan",
            "numInstances",
            "maxShutdownDelaySeconds",
            "buildCommand",
            "startCommand",
            "envVars",
        ):
            self.assertEqual(worker.get(key), canonical_worker.get(key), key)

        self.assertEqual(worker["numInstances"], 3)
        self.assertEqual(worker["startCommand"], "python -m app.j1_claim_worker")


if __name__ == "__main__":
    unittest.main()
