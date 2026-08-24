from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app import prematch_inference as inference_v1
from app.model_dataset import STANDARD_FEATURES

FIT_CACHE_VERSION = "training_fit_cache_v1"
DEFAULT_MAX_FIT_CACHE_ENTRIES = 8


@dataclass
class _FitCacheEntry:
    key: tuple[str, str, str]
    training_sha256: str
    pipeline: Pipeline
    model_metadata: dict[str, Any]
    fit_seconds: float
    training_rows: int


class TrainingFitCache:
    """Cycle-local fitted-model cache keyed by the immutable training SHA.

    A fit is reusable only when the V1 training SHA, model/pipeline signature and
    class-weight policy are identical. Target features are never part of a cache
    entry; every prediction is produced separately against the cached estimator.
    """

    def __init__(
        self,
        *,
        class_weight_balanced: bool = False,
        max_entries: int = DEFAULT_MAX_FIT_CACHE_ENTRIES,
    ) -> None:
        if max_entries < 1 or max_entries > 32:
            raise ValueError("max_entries must be between 1 and 32")
        self.class_weight_balanced = bool(class_weight_balanced)
        self.max_entries = int(max_entries)
        self.pipeline_signature = inference_v1._stable_hash(
            {
                "model_version": inference_v1.MODEL_VERSION,
                "features": list(STANDARD_FEATURES),
                "algorithm": "multinomial_logistic_regression",
                "pipeline": ["median_imputer", "standard_scaler", "logistic_regression"],
                "C": 1.0,
                "penalty": "l2",
                "solver": "lbfgs",
                "max_iter": 2000,
                "class_weight": "balanced" if self.class_weight_balanced else None,
                "random_state": 42,
            }
        )
        self._entries: dict[tuple[str, str, str], _FitCacheEntry] = {}
        self._stats: dict[str, float | int] = {
            "fit_builds": 0,
            "fit_reuses": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_evictions": 0,
            "predict_calls": 0,
            "fit_seconds": 0.0,
        }

    def _key(self, training_sha256: str) -> tuple[str, str, str]:
        value = str(training_sha256 or "").strip()
        if not value:
            raise ValueError("training_sha256 is required for fit cache")
        return (
            value,
            inference_v1.MODEL_VERSION,
            self.pipeline_signature,
        )

    def _build_pipeline(self, training_rows: list[dict[str, Any]]) -> _FitCacheEntry:
        X_train = np.asarray(
            [[row["X"].get(name) for name in STANDARD_FEATURES] for row in training_rows],
            dtype=float,
        )
        y_train = np.asarray([row["y"] for row in training_rows], dtype=object)
        train_classes = sorted(set(y_train.tolist()))
        if set(train_classes) != set(inference_v1.CLASS_ORDER):
            raise ValueError(
                f"training rows must contain all 1X2 classes; found {train_classes}"
            )

        pipeline = Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                (
                    "classifier",
                    LogisticRegression(
                        C=1.0,
                        penalty="l2",
                        solver="lbfgs",
                        max_iter=2000,
                        class_weight="balanced" if self.class_weight_balanced else None,
                        random_state=42,
                    ),
                ),
            ]
        )
        started = perf_counter()
        pipeline.fit(X_train, y_train)
        fit_seconds = perf_counter() - started
        metadata = {
            "algorithm": "multinomial_logistic_regression",
            "pipeline": ["median_imputer", "standard_scaler", "logistic_regression"],
            "family": "STANDARD",
            "feature_count": len(STANDARD_FEATURES),
            "feature_names": list(STANDARD_FEATURES),
            "C": 1.0,
            "penalty": "l2",
            "solver": "lbfgs",
            "max_iter": 2000,
            "class_weight": "balanced" if self.class_weight_balanced else None,
            "random_state": 42,
            "fit_scope": "all eligible historical rows strictly before target kickoff",
            "production_refit": True,
        }
        # key/training SHA are filled by predict() after the exact cache key is known.
        return _FitCacheEntry(
            key=("", "", ""),
            training_sha256="",
            pipeline=pipeline,
            model_metadata=metadata,
            fit_seconds=fit_seconds,
            training_rows=len(training_rows),
        )

    @staticmethod
    def _predict_pipeline(
        pipeline: Pipeline,
        target_features: dict[str, float | None],
    ) -> dict[str, float]:
        target_matrix = np.asarray(
            [[target_features.get(name) for name in STANDARD_FEATURES]],
            dtype=float,
        )
        raw = pipeline.predict_proba(target_matrix)[0]
        classes = list(pipeline.named_steps["classifier"].classes_)
        ordered = {
            label: float(raw[classes.index(label)])
            for label in inference_v1.CLASS_ORDER
        }
        total = sum(ordered.values())
        return {
            label: ordered[label] / total
            for label in inference_v1.CLASS_ORDER
        }

    def predict(
        self,
        *,
        training_rows: list[dict[str, Any]],
        training_sha256: str,
        target_features: dict[str, float | None],
    ) -> tuple[dict[str, float], dict[str, Any], dict[str, Any]]:
        key = self._key(training_sha256)
        entry = self._entries.get(key)
        cache_hit = entry is not None

        if entry is None:
            self._stats["cache_misses"] = int(self._stats["cache_misses"]) + 1
            if len(self._entries) >= self.max_entries:
                oldest_key = next(iter(self._entries))
                self._entries.pop(oldest_key, None)
                self._stats["cache_evictions"] = int(self._stats["cache_evictions"]) + 1

            built = self._build_pipeline(training_rows)
            entry = _FitCacheEntry(
                key=key,
                training_sha256=str(training_sha256),
                pipeline=built.pipeline,
                model_metadata=built.model_metadata,
                fit_seconds=built.fit_seconds,
                training_rows=built.training_rows,
            )
            self._entries[key] = entry
            self._stats["fit_builds"] = int(self._stats["fit_builds"]) + 1
            self._stats["fit_seconds"] = round(
                float(self._stats["fit_seconds"]) + entry.fit_seconds,
                6,
            )
        else:
            self._stats["cache_hits"] = int(self._stats["cache_hits"]) + 1
            self._stats["fit_reuses"] = int(self._stats["fit_reuses"]) + 1

        self._stats["predict_calls"] = int(self._stats["predict_calls"]) + 1
        probabilities = self._predict_pipeline(entry.pipeline, target_features)
        audit = {
            "version": FIT_CACHE_VERSION,
            "cache_hit": cache_hit,
            "training_sha256": entry.training_sha256,
            "pipeline_signature": self.pipeline_signature,
            "training_rows": entry.training_rows,
            "fit_seconds": round(entry.fit_seconds, 6) if not cache_hit else 0.0,
            "original_fit_seconds": round(entry.fit_seconds, 6),
            "target_features_cached": False,
            "reuse_allowed_only_for_identical_training_sha256": True,
        }
        return probabilities, dict(entry.model_metadata), audit

    def audit(self) -> dict[str, Any]:
        return {
            "version": FIT_CACHE_VERSION,
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "pipeline_signature": self.pipeline_signature,
            "fit_builds": int(self._stats["fit_builds"]),
            "fit_reuses": int(self._stats["fit_reuses"]),
            "cache_hits": int(self._stats["cache_hits"]),
            "cache_misses": int(self._stats["cache_misses"]),
            "cache_evictions": int(self._stats["cache_evictions"]),
            "predict_calls": int(self._stats["predict_calls"]),
            "fit_seconds": round(float(self._stats["fit_seconds"]), 6),
            "policy": {
                "scope": "single_j1_cycle",
                "cache_key_contains_training_sha256": True,
                "cache_key_contains_model_version": True,
                "cache_key_contains_pipeline_signature": True,
                "target_features_cached": False,
                "distinct_training_sha256_never_share_fit": True,
            },
        }
