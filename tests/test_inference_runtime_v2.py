from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.inference_runtime_v2 import INFERENCE_RUNTIME_VERSION, InferenceRuntimeV2
from app.model_dataset import STANDARD_FEATURES
from app import prematch_inference as inference_v1


class InferenceRuntimeV2Tests(unittest.TestCase):
    @staticmethod
    def _dataset() -> dict:
        return {
            "status": "ok",
            "dataset_id": "dataset:test",
            "dataset_sha256": "abc123",
            "rows": [
                {
                    "fixture_id": 1,
                    "sportmonks_fixture_id": 101,
                    "starts_at": "2026-08-23T15:00:00+00:00",
                    "features": {},
                    "label": {"outcome_1x2": "1"},
                },
                {
                    "fixture_id": 2,
                    "sportmonks_fixture_id": 102,
                    "starts_at": "2026-08-24T16:50:00+00:00",
                    "features": {},
                    "label": {"outcome_1x2": "X"},
                },
            ],
        }

    def test_same_date_targets_build_dataset_once_and_reuse_prepared_rows(self) -> None:
        runtime = InferenceRuntimeV2()
        first_cutoff = datetime(2026, 8, 24, 16, 45, tzinfo=timezone.utc)
        second_cutoff = datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc)

        with patch(
            "app.inference_runtime_v2.build_full_training_dataset",
            return_value=self._dataset(),
        ) as builder, patch(
            "app.inference_runtime_v2._flatten_standard",
            side_effect=lambda row: {"fixture_marker": float(row["fixture_id"])},
        ):
            first_rows, first_audit = runtime._training_rows_before_target(first_cutoff)
            second_rows, second_audit = runtime._training_rows_before_target(second_cutoff)

        self.assertEqual(builder.call_count, 1)
        self.assertEqual(len(first_rows), 1)
        self.assertEqual(len(second_rows), 2)
        self.assertFalse(first_audit["runtime"]["dataset_reused"])
        self.assertTrue(second_audit["runtime"]["dataset_reused"])

        summary = runtime.audit()
        self.assertEqual(summary["version"], INFERENCE_RUNTIME_VERSION)
        self.assertEqual(summary["dataset_builds"], 1)
        self.assertEqual(summary["dataset_reuses"], 1)
        self.assertEqual(summary["training_views"], 2)

    def test_training_hash_preserves_v1_semantics(self) -> None:
        runtime = InferenceRuntimeV2()
        cutoff = datetime(2026, 8, 24, 16, 45, tzinfo=timezone.utc)

        with patch(
            "app.inference_runtime_v2.build_full_training_dataset",
            return_value=self._dataset(),
        ), patch(
            "app.inference_runtime_v2._flatten_standard",
            side_effect=lambda row: {"fixture_marker": float(row["fixture_id"])},
        ):
            rows, audit = runtime._training_rows_before_target(cutoff)

        expected = inference_v1._stable_hash(
            {
                "inference_version": inference_v1.PREMATCH_INFERENCE_VERSION,
                "model_version": inference_v1.MODEL_VERSION,
                "feature_names": STANDARD_FEATURES,
                "lookback_matches": runtime.lookback_matches,
                "min_history_matches": runtime.min_history_matches,
                "target_cutoff": cutoff.isoformat(),
                "rows": rows,
            }
        )
        self.assertEqual(audit["training_sha256"], expected)
        self.assertTrue(audit["strictly_before_target"])

    def test_different_target_dates_use_distinct_exact_v1_windows(self) -> None:
        runtime = InferenceRuntimeV2()
        first = datetime(2026, 8, 24, 23, 55, tzinfo=timezone.utc)
        second = datetime(2026, 8, 25, 0, 5, tzinfo=timezone.utc)

        with patch(
            "app.inference_runtime_v2.build_full_training_dataset",
            return_value={"dataset_id": "d", "dataset_sha256": "h", "rows": []},
        ) as builder:
            runtime._training_rows_before_target(first)
            runtime._training_rows_before_target(second)

        self.assertEqual(builder.call_count, 2)
        self.assertEqual(runtime.audit()["dataset_builds"], 2)
        self.assertEqual(runtime.audit()["dataset_cache_entries"], 2)


if __name__ == "__main__":
    unittest.main()
