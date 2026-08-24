from __future__ import annotations

import unittest

from app import prematch_inference as inference_v1
from app.inference_fit_cache import TrainingFitCache
from app.model_dataset import STANDARD_FEATURES


class TrainingFitCacheTests(unittest.TestCase):
    @staticmethod
    def _training_rows() -> list[dict]:
        rows: list[dict] = []
        labels = ["1", "X", "2", "1", "X", "2", "1", "X", "2"]
        for index, label in enumerate(labels, start=1):
            features = {
                name: float(index + feature_index / 100.0)
                for feature_index, name in enumerate(STANDARD_FEATURES)
            }
            rows.append({"X": features, "y": label})
        return rows

    @staticmethod
    def _target(offset: float) -> dict[str, float]:
        return {
            name: float(offset + feature_index / 100.0)
            for feature_index, name in enumerate(STANDARD_FEATURES)
        }

    def test_cached_pipeline_matches_v1_fit_and_predict(self) -> None:
        rows = self._training_rows()
        target = self._target(4.5)
        expected, expected_meta = inference_v1._fit_and_predict(rows, target, False)

        cache = TrainingFitCache(class_weight_balanced=False)
        actual, actual_meta, audit = cache.predict(
            training_rows=rows,
            training_sha256="same-training-sha",
            target_features=target,
        )

        for label in inference_v1.CLASS_ORDER:
            self.assertAlmostEqual(actual[label], expected[label], places=12)
        self.assertEqual(actual_meta, expected_meta)
        self.assertFalse(audit["cache_hit"])

    def test_identical_training_sha_reuses_fit_for_new_target(self) -> None:
        rows = self._training_rows()
        cache = TrainingFitCache(class_weight_balanced=False)

        first, _, first_audit = cache.predict(
            training_rows=rows,
            training_sha256="shared-sha",
            target_features=self._target(3.5),
        )
        second, _, second_audit = cache.predict(
            training_rows=rows,
            training_sha256="shared-sha",
            target_features=self._target(7.5),
        )

        self.assertFalse(first_audit["cache_hit"])
        self.assertTrue(second_audit["cache_hit"])
        self.assertNotEqual(first, second)
        summary = cache.audit()
        self.assertEqual(summary["fit_builds"], 1)
        self.assertEqual(summary["fit_reuses"], 1)
        self.assertEqual(summary["predict_calls"], 2)
        self.assertEqual(summary["cache_hits"], 1)
        self.assertEqual(summary["cache_misses"], 1)

    def test_distinct_training_sha_never_reuses_fit(self) -> None:
        rows = self._training_rows()
        cache = TrainingFitCache(class_weight_balanced=False)
        cache.predict(
            training_rows=rows,
            training_sha256="sha-a",
            target_features=self._target(3.5),
        )
        cache.predict(
            training_rows=rows,
            training_sha256="sha-b",
            target_features=self._target(3.5),
        )
        summary = cache.audit()
        self.assertEqual(summary["fit_builds"], 2)
        self.assertEqual(summary["fit_reuses"], 0)
        self.assertEqual(summary["cache_hits"], 0)
        self.assertEqual(summary["cache_misses"], 2)

    def test_fit_cache_is_memory_bounded(self) -> None:
        rows = self._training_rows()
        cache = TrainingFitCache(class_weight_balanced=False, max_entries=1)
        cache.predict(
            training_rows=rows,
            training_sha256="sha-a",
            target_features=self._target(3.5),
        )
        cache.predict(
            training_rows=rows,
            training_sha256="sha-b",
            target_features=self._target(4.5),
        )
        cache.predict(
            training_rows=rows,
            training_sha256="sha-a",
            target_features=self._target(5.5),
        )
        summary = cache.audit()
        self.assertEqual(summary["entries"], 1)
        self.assertEqual(summary["cache_evictions"], 2)
        self.assertEqual(summary["fit_builds"], 3)


if __name__ == "__main__":
    unittest.main()
