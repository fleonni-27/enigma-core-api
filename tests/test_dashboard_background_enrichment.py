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

    def test_runner_is_streaming_and_bounded(self):
        source = (ROOT / "app" / "dashboard_enrichment_runner.py").read_text()
        stream = (ROOT / "app" / "dashboard_j1_team_enrichment_stream.py").read_text()
        self.assertIn("build_streaming_team_enrichment", source)
        self.assertNotIn("build_bulk_team_enrichment", source)
        self.assertIn("backfill_missing_xg", source)
        self.assertIn("MAX_TARGET_FIXTURES = 20", source)
        self.assertIn("XG_BACKFILL_LIMIT = 8", source)
        self.assertIn("XG_BACKFILL_CONCURRENCY = 1", source)
        self.assertIn("HISTORY_SCAN_LIMIT_PER_TEAM = 24", stream)
        self.assertIn(".limit(HISTORY_SCAN_LIMIT_PER_TEAM)", stream)
        self.assertIn('"no_prediction_or_decision_writes": True', source)

    def test_existing_worker_runs_enrichment_only_when_idle(self):
        worker = (ROOT / "app" / "j1_claim_worker.py").read_text()
        hook = (ROOT / "app" / "dashboard_enrichment_worker_hook.py").read_text()
        render = (ROOT / "render.yaml").read_text()
        self.assertIn("maybe_run_idle_dashboard_enrichment", worker)
        self.assertIn("if outcome is not None", worker)
        self.assertIn("ENRICHMENT_IDLE_INTERVAL_SECONDS = 15 * 60", hook)
        self.assertIn("pg_try_advisory_lock", hook)
        self.assertIn("ENRICHMENT_TIMEOUT_SECONDS = 120", hook)
        self.assertNotIn("name: enigma-dashboard-enrichment", render)

    def test_cache_has_one_row_per_fixture(self):
        source = (ROOT / "migrations" / "versions" / "20260827_0007_dashboard_enrichment_cache.py").read_text()
        self.assertIn('UniqueConstraint("fixture_id"', source)
        self.assertIn('"dashboard_enrichment_snapshots"', source)


if __name__ == "__main__":
    unittest.main()
