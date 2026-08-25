from pathlib import Path
import unittest

from app.xg_backfill_bounded_v2 import CHUNK_SIZE
from app.xg_backfill_memory_scan import _league_aliases


class XGBackfillMemoryScanTests(unittest.TestCase):
    def test_requested_league_is_filtered_to_known_aliases_before_database_load(self):
        aliases = _league_aliases(["Serie A"])
        self.assertIn("Serie A", aliases)
        self.assertIn("Brazil Serie A", aliases)
        self.assertNotIn("Serie B", aliases)
        self.assertNotIn("La Liga", aliases)

    def test_bounded_runner_filters_and_limits_before_materializing_payloads(self):
        source = Path("app/xg_backfill_bounded_v2.py").read_text(encoding="utf-8")
        startup = Path("app/xg_backfill_startup.py").read_text(encoding="utf-8")
        self.assertIn("Fixture.league_name.in_(aliases)", source)
        self.assertIn("scalar_subquery()", source)
        self.assertIn(".limit(limit)", source)
        self.assertIn("select(\n                Fixture.id,", source)
        self.assertNotIn("FixtureDataSnapshot.statistics,", source)
        self.assertNotIn("FixtureDataSnapshot.xg,", source)
        self.assertEqual(CHUNK_SIZE, 12)
        self.assertIn("backfill_missing_xg = backfill_missing_xg_bounded", startup)
        self.assertIn("xg_gap_status = gap_status_sql", startup)


if __name__ == "__main__":
    unittest.main()
