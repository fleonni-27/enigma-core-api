from __future__ import annotations

import json
import urllib.parse
import urllib.request
import unittest

from app.training_dataset import _as_list, _type_name, _xg_value


BASE = "https://enigma-core-api.onrender.com"


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as response:
        return json.load(response)


class XGLiveSnapshotDiagnostic(unittest.TestCase):
    def test_live_snapshot_xg_parser_contract(self) -> None:
        params = urllib.parse.urlencode(
            {
                "start_date": "2026-04-01",
                "end_date": "2026-08-24",
                "leagues": "Serie A",
                "max_rows": 500,
                "include_rows": "true",
            }
        )
        evaluation = _get_json(f"{BASE}/research/enigma-rating-v2/evaluation-v1?{params}")
        rows = [row for row in (evaluation.get("rows") or []) if row.get("partition") == "test"]
        self.assertTrue(rows)

        print(
            "XG_EVALUATION_AUDIT="
            + json.dumps(
                {
                    "audit": evaluation.get("audit"),
                    "test_size": len(rows),
                    "first_test_fixture_ids": [row.get("sportmonks_fixture_id") for row in rows[:5]],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

        for row in rows[:3]:
            fixture_id = int(row["sportmonks_fixture_id"])
            audit_url = (
                f"{BASE}/audit/fixture/{fixture_id}"
                "?include_raw=true&sample_size=25"
            )
            payload = _get_json(audit_url)
            raw = payload.get("raw") or {}
            xg_rows = _as_list(raw.get("xg"))
            statistics = _as_list(raw.get("statistics"))
            summary = {
                "fixture_id": fixture_id,
                "snapshot": payload.get("snapshot"),
                "counts": payload.get("counts"),
                "xg_type_names": [_type_name(item) for item in xg_rows],
                "xg_home_parsed": _xg_value(xg_rows, statistics, "home"),
                "xg_away_parsed": _xg_value(xg_rows, statistics, "away"),
                "xg_rows": xg_rows,
            }
            print("XG_LIVE_DIAGNOSTIC=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))

        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
