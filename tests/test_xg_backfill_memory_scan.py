from pathlib import Path
import unittest

from app.xg_backfill_memory_scan import _league_aliases


class XGBackfillMemoryScanTests(unittest.TestCase):
    def test_requested_league_is_filtered_to_known_aliases_before_database_load(self):
        aliases = _league_aliases(["Serie A"])
        self.assertIn("Serie A", aliases)
        self.assertIn("Brazil Serie A", aliases)
        self.assertNotIn("Serie B", aliases)
        self.assertNotIn("La Liga", aliases)

    def test_scanner_uses_correlated_latest_snapshot_and_sql_league_filter(self):
        source = Path("app/xg_backfill_memory_scan.py").read_text(encoding="utf-8")
        startup = Path("app/xg_backfill_startup.py").read_text(encoding="utf-8")
        self.assertIn("Fixture.league_name.in_(aliases)", source)
        self.assertIn("scalar_subquery()", source)
        self.assertNotIn("fixture_ids =", source)
        self.assertIn("_latest_snapshot_rows = latest_snapshot_rows_memory_bounded", startup)


if __name__ == "__main__":
    unittest.main()
