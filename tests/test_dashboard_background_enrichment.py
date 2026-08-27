import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class DashboardBackgroundEnrichmentTests(unittest.TestCase):
    def test_dashboard_reads_cache_without_history_or_provider_calls(self):
        source = (ROOT / "app" / "dashboard_match_center_v3_light.py").read_text()
        self.assertIn("load_dashboard_enrichment", source)
        self.assertNotIn("build_bulk_team_enrichment", source)
        self.assertNotIn("SportmonksClient", source)
        self.assertIn('"history_reconstruction_during_dashboard_refresh": False', source)
        self.assertIn('"enrichment_is_background_materialized": True', source)

    def test_runner_is_separate_and_bounded(self):
        source = (ROOT / "app" / "dashboard_enrichment_runner.py").read_text()
        self.assertIn("build_bulk_team_enrichment", source)
        self.assertIn("backfill_missing_xg", source)
        self.assertIn("XG_BACKFILL_LIMIT = 12", source)
        self.assertIn("XG_BACKFILL_CONCURRENCY = 2", source)
        self.assertIn('"separate_from_j1_runner": True', source)
        self.assertIn('"no_prediction_or_decision_writes": True', source)

    def test_render_has_isolated_fifteen_minute_cron(self):
        source = (ROOT / "render.yaml").read_text()
        self.assertIn("name: enigma-dashboard-enrichment", source)
        self.assertIn("startCommand: python -m app.dashboard_enrichment_runner", source)
        self.assertIn('schedule: "*/15 * * * *"', source)

    def test_cache_has_one_row_per_fixture(self):
        source = (ROOT / "migrations" / "versions" / "20260827_0007_dashboard_enrichment_cache.py").read_text()
        self.assertIn('UniqueConstraint("fixture_id"', source)
        self.assertIn('"dashboard_enrichment_snapshots"', source)


if __name__ == "__main__":
    unittest.main()
