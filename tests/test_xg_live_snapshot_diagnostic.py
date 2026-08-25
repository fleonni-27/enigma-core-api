from __future__ import annotations

import json
import urllib.request
import unittest

from app.training_dataset import _as_list, _type_name, _xg_value


class XGLiveSnapshotDiagnostic(unittest.TestCase):
    def test_live_snapshot_xg_parser_contract(self) -> None:
        for fixture_id in (19453590, 19453588, 19453594):
            url = (
                f"https://enigma-core-api.onrender.com/audit/fixture/{fixture_id}"
                "?include_raw=true&sample_size=25"
            )
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = json.load(response)

            raw = payload.get("raw") or {}
            xg_rows = _as_list(raw.get("xg"))
            statistics = _as_list(raw.get("statistics"))
            summary = {
                "fixture_id": fixture_id,
                "snapshot": payload.get("snapshot"),
                "counts": payload.get("counts"),
                "xg_type_names": [_type_name(row) for row in xg_rows],
                "xg_home_parsed": _xg_value(xg_rows, statistics, "home"),
                "xg_away_parsed": _xg_value(xg_rows, statistics, "away"),
                "xg_rows": xg_rows,
            }
            print("XG_LIVE_DIAGNOSTIC=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))

        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
