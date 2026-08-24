from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.performance_observatory import (
    PIPELINE_DAILY_SYNC,
    PIPELINE_J1,
    PipelinePerformanceSample,
    build_performance_summary,
    record_daily_sync_result,
    record_j1_result,
    try_persist_pipeline_sample,
)


class PerformanceObservatoryV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        PipelinePerformanceSample.__table__.create(bind=self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.patcher = patch(
            "app.performance_observatory.SessionLocal",
            self.SessionLocal,
        )
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.engine.dispose()

    def test_p50_p95_p99_match_percentile_cont_semantics(self) -> None:
        now = datetime.now(timezone.utc)
        for value in (1.0, 2.0, 3.0, 4.0, 100.0):
            persisted = try_persist_pipeline_sample(
                pipeline=PIPELINE_J1,
                source="test",
                status="OK",
                cycle_seconds=value,
                selected_fixtures=1,
                observed_at=now,
            )
            self.assertEqual(persisted["status"], "persisted")

        summary = build_performance_summary(
            pipeline=PIPELINE_J1,
            lookback_hours=1,
        )
        cycle = summary["percentiles"]["cycle_seconds"]
        self.assertEqual(summary["samples"]["total"], 5)
        self.assertEqual(cycle["count"], 5)
        self.assertAlmostEqual(cycle["p50"], 3.0, places=6)
        self.assertAlmostEqual(cycle["p95"], 80.8, places=6)
        self.assertAlmostEqual(cycle["p99"], 96.16, places=6)

    def test_idle_j1_is_excluded_by_default_but_available_on_request(self) -> None:
        now = datetime.now(timezone.utc)
        try_persist_pipeline_sample(
            pipeline=PIPELINE_J1,
            source="render_cron",
            status="IDLE",
            cycle_seconds=0.01,
            selected_fixtures=0,
            observed_at=now,
        )
        try_persist_pipeline_sample(
            pipeline=PIPELINE_J1,
            source="render_cron",
            status="OK",
            cycle_seconds=5.0,
            selected_fixtures=1,
            observed_at=now,
        )
        try_persist_pipeline_sample(
            pipeline=PIPELINE_DAILY_SYNC,
            source="daily_operations",
            status="OK",
            cycle_seconds=2.0,
            selected_fixtures=0,
            observed_at=now,
        )

        active = build_performance_summary(lookback_hours=1)
        all_samples = build_performance_summary(lookback_hours=1, include_idle=True)

        self.assertEqual(active["samples"]["total"], 2)
        self.assertEqual(all_samples["samples"]["total"], 3)
        self.assertTrue(
            active["policy"]["idle_j1_excluded_from_percentiles_by_default"]
        )

    def test_daily_sync_result_extracts_transport_and_upstream_metrics(self) -> None:
        result = {
            "status": "ok",
            "target_fixtures": {"count": 6},
            "odds": {"rows_created": 100, "items": [{"large": "payload"}]},
            "performance": {
                "cycle_seconds": 4.5,
                "odds_fetch": {"fetch_seconds": 1.25},
                "sportmonks_transport": {
                    "logical_requests": 7,
                    "requests": 9,
                    "retry": {"retries": 2},
                    "rate_limit": {"responses_429": 1},
                },
            },
        }
        persisted = record_daily_sync_result(result)
        self.assertEqual(persisted["status"], "persisted")

        with self.SessionLocal() as session:
            row = session.scalar(select(PipelinePerformanceSample))
            self.assertIsNotNone(row)
            self.assertEqual(row.pipeline, PIPELINE_DAILY_SYNC)
            self.assertEqual(row.selected_fixtures, 6)
            self.assertEqual(row.logical_requests, 7)
            self.assertEqual(row.http_requests, 9)
            self.assertEqual(row.retries, 2)
            self.assertEqual(row.rate_limited_responses, 1)
            self.assertAlmostEqual(row.upstream_seconds, 1.25)
            self.assertNotIn("items", row.raw_metrics["odds"])

    def test_j1_result_extracts_runtime_fit_and_dataset_metrics(self) -> None:
        result = {
            "selected_fixtures": 3,
            "run_health": {"status": "OK"},
            "counts": {"completed": 3},
            "performance": {
                "cycle_seconds": 12.0,
                "upstream_prefetch": {"prefetch_seconds": 2.0},
                "sportmonks_transport": {
                    "logical_requests": 6,
                    "requests": 8,
                    "retry": {"retries": 2},
                    "rate_limit": {"responses_429": 0},
                },
            },
            "inference_runtime": {
                "dataset_build_seconds": 3.5,
                "fit_seconds": 1.75,
            },
        }
        persisted = record_j1_result(
            result,
            source="render_cron",
            run_id=123,
            scheduler_status="OK",
        )
        self.assertEqual(persisted["status"], "persisted")

        with self.SessionLocal() as session:
            row = session.scalar(select(PipelinePerformanceSample))
            self.assertIsNotNone(row)
            self.assertEqual(row.pipeline, PIPELINE_J1)
            self.assertEqual(row.run_id, 123)
            self.assertAlmostEqual(row.upstream_seconds, 2.0)
            self.assertAlmostEqual(row.dataset_build_seconds, 3.5)
            self.assertAlmostEqual(row.fit_seconds, 1.75)

    def test_unknown_pipeline_fails_closed_without_throwing(self) -> None:
        result = try_persist_pipeline_sample(
            pipeline="unknown",
            source="test",
            status="OK",
            cycle_seconds=1.0,
        )
        self.assertEqual(result["status"], "not_persisted")
        self.assertEqual(result["error"], "UNKNOWN_PIPELINE")


if __name__ == "__main__":
    unittest.main()
