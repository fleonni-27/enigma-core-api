from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.xg_backfill_startup import _startup_config, _truthy
from app.xg_historical_backfill import _xg_payload, _xg_type_names


class XGHistoricalBackfillTests(unittest.TestCase):
    def test_xg_payload_accepts_current_and_legacy_casing(self) -> None:
        current = {"data": {"xgfixture": [{"value": 1.25}]}}
        legacy = {"data": {"xGFixture": [{"value": 0.82}]}}
        self.assertEqual(_xg_payload(current), [{"value": 1.25}])
        self.assertEqual(_xg_payload(legacy), [{"value": 0.82}])
        self.assertEqual(_xg_payload({"data": {}}), [])

    def test_xg_type_names_are_auditable(self) -> None:
        rows = [
            {"type": {"name": "Expected Goals (xG)"}, "value": 1.2},
            {"type_id": 5304, "value": 0.9},
        ]
        self.assertEqual(_xg_type_names(rows), ["5304", "Expected Goals (xG)"])

    def test_startup_defaults_are_bounded_to_serie_a_2026(self) -> None:
        keys = [
            "XG_BACKFILL_START_DATE",
            "XG_BACKFILL_END_DATE",
            "XG_BACKFILL_LEAGUES",
            "XG_BACKFILL_LIMIT",
            "XG_BACKFILL_CONCURRENCY",
        ]
        with patch.dict(os.environ, {key: "" for key in keys}, clear=False):
            for key in keys:
                os.environ.pop(key, None)
            config = _startup_config()
        self.assertEqual(config["start_date"].isoformat(), "2026-01-01")
        self.assertEqual(config["end_date"].isoformat(), "2026-08-24")
        self.assertEqual(config["leagues"], ["Serie A"])
        self.assertEqual(config["limit"], 1000)
        self.assertEqual(config["concurrency"], 3)

    def test_truthy_flag_is_explicit(self) -> None:
        self.assertTrue(_truthy("true"))
        self.assertTrue(_truthy("1"))
        self.assertFalse(_truthy("false"))
        self.assertFalse(_truthy(None))

    def test_append_only_and_route_registration_contract(self) -> None:
        module_source = Path("app/xg_historical_backfill.py").read_text()
        main_source = Path("app/main_v017.py").read_text()
        self.assertIn('source="sportmonks_xg_backfill_v1"', module_source)
        self.assertIn("existing_snapshots_never_overwritten", module_source)
        self.assertNotIn("latest.xg =", module_source)
        self.assertIn("xg_historical_backfill_router", main_source)
        self.assertIn('app.version = "0.53.0"', main_source)


if __name__ == "__main__":
    unittest.main()
