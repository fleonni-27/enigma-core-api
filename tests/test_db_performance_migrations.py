from __future__ import annotations

import json
import unittest
from pathlib import Path

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

    def test_hot_path_migration_is_concurrent_and_contains_expected_indexes(self) -> None:
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
        self.assertIn("CREATE INDEX CONCURRENTLY IF NOT EXISTS", migration)
        self.assertIn("DROP INDEX CONCURRENTLY IF EXISTS", migration)

    def test_release_runner_uses_baseline_then_index_revision(self) -> None:
        self.assertEqual(db_release.BASELINE_REVISION, "20260824_0001")
        self.assertEqual(db_release.INDEX_REVISION, "20260824_0002")
        render = Path("render.yaml").read_text()
        self.assertIn("preDeployCommand: python -m app.db_release", render)


if __name__ == "__main__":
    unittest.main()
