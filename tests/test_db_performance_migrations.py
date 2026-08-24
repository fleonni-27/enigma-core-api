from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db_query_plan_audit as audit
from app import db_release


class DbPerformanceMigrationTests(unittest.TestCase):
    def test_plan_parser_extracts_indexes_recursively(self) -> None:
        plan = {
            "Node Type": "Nested Loop",
            "Plans": [
                {"Node Type": "Index Scan", "Index Name": "ix_a"},
                {
                    "Node Type": "Bitmap Heap Scan",
                    "Plans": [
                        {"Node Type": "Bitmap Index Scan", "Index Name": "ix_b"}
                    ],
                },
            ],
        }
        self.assertEqual(audit._index_names(plan), {"ix_a", "ix_b"})

    def test_explain_json_normalizer_accepts_psycopg_shape(self) -> None:
        payload = [{"Plan": {"Node Type": "Index Scan"}, "Execution Time": 1.25}]
        self.assertEqual(audit._as_json_plan(payload)["Execution Time"], 1.25)
        self.assertEqual(
            audit._as_json_plan(json.dumps(payload))["Plan"]["Node Type"],
            "Index Scan",
        )

    def test_comparison_reports_execution_delta_and_indexes(self) -> None:
        before = {
            "plans": [
                {
                    "query_name": "q",
                    "execution_ms": 10.0,
                    "indexes_used": [],
                }
            ]
        }
        after = {
            "plans": [
                {
                    "query_name": "q",
                    "execution_ms": 4.0,
                    "indexes_used": ["ix_q"],
                }
            ]
        }
        result = audit.summarize_comparison(before, after)
        row = result["comparisons"][0]
        self.assertEqual(row["execution_delta_pct"], -60.0)
        self.assertEqual(row["after_indexes_used"], ["ix_q"])

    def test_hot_path_migration_is_bounded_resumable_and_contains_expected_indexes(self) -> None:
        migration = Path("migrations/versions/20260824_0002_hot_path_indexes.py").read_text()
        expected = {
            "ix_odds_fixture_window_fetched_id",
            "ix_prediction_fixture_window_generated_id",
            "ix_context_fixture_window_fetched_id",
            "ix_decision_fixture_source_window_recorded_id",
            "ix_fixture_data_fixture_fetched_id",
            "ix_fixture_home_starts_id",
            "ix_fixture_away_starts_id",
            "ix_decision_settlement_starts_recorded_id",
            "ix_decision_sportmonks_recorded_id",
        }
        for name in expected:
            self.assertIn(name, migration)
        self.assertIn("CREATE INDEX IF NOT EXISTS", migration)
        self.assertIn("DROP INDEX IF EXISTS", migration)
        self.assertIn("indisvalid", migration)
        self.assertIn("lock_timeout", migration)
        self.assertIn("statement_timeout", migration)
        self.assertNotIn('op.execute(f"CREATE INDEX CONCURRENTLY', migration)

    def test_odds_quote_migration_can_resume_after_partial_column_commit(self) -> None:
        migration = Path("migrations/versions/20260824_0003_odds_quote_state.py").read_text()
        self.assertIn("existing_columns", migration)
        self.assertIn('"first_seen_at" not in existing_columns', migration)
        self.assertIn('"observation_count" not in existing_columns', migration)
        self.assertIn("CREATE INDEX IF NOT EXISTS", migration)
        self.assertIn("indisvalid", migration)
        self.assertNotIn('op.execute(f"CREATE INDEX CONCURRENTLY', migration)

    def test_release_runner_uses_baseline_then_index_revision(self) -> None:
        self.assertEqual(db_release.BASELINE_REVISION, "20260824_0001")
        self.assertEqual(db_release.INDEX_REVISION, "20260824_0002")
        self.assertEqual(db_release.DB_RELEASE_VERSION, "db_release_v1_2")
        render = Path("render.yaml").read_text()
        self.assertIn("preDeployCommand: python -m app.db_release", render)

    def test_startup_fallback_is_noop_when_database_is_at_head(self) -> None:
        with (
            patch.object(db_release, "_current_revision", return_value="20260824_0002"),
            patch.object(db_release, "_head_revision", return_value="20260824_0002"),
            patch.object(db_release, "run_release") as run_release,
        ):
            result = db_release.ensure_database_release_current()
        self.assertEqual(result["status"], "current")
        self.assertFalse(result["migration_executed"])
        run_release.assert_not_called()

    def test_startup_fallback_runs_release_when_revision_lags(self) -> None:
        with (
            patch.object(db_release, "_current_revision", return_value=None),
            patch.object(db_release, "_head_revision", return_value="20260824_0002"),
            patch.object(
                db_release,
                "run_release",
                return_value={"status": "ok", "final_revision": "20260824_0002"},
            ) as run_release,
        ):
            result = db_release.ensure_database_release_current()
        self.assertTrue(result["startup_fallback_executed"])
        run_release.assert_called_once_with()

    def test_web_entrypoint_contains_non_blocking_release_maintenance(self) -> None:
        entrypoint = Path("app/main_v015.py").read_text()
        self.assertIn("ensure_database_release_current", entrypoint)
        self.assertIn("_run_managed_startup_maintenance", entrypoint)
        self.assertIn("schedule_managed_startup_maintenance", entrypoint)
        self.assertIn("asyncio.create_task", entrypoint)


if __name__ == "__main__":
    unittest.main()
