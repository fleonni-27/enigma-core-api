from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class J1OperationalContractTests(unittest.TestCase):
    def test_operational_contract_validator_passes(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/validate_j1_operational_contract.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("J1 operational contract: OK", completed.stdout)

    def test_retired_scheduler_entrypoint_is_absent(self) -> None:
        self.assertFalse((ROOT / "app/j1_scheduler_v2.py").exists())


if __name__ == "__main__":
    unittest.main()
