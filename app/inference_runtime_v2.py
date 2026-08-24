from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.league_registry import TARGET_LEAGUES, canonical_league
from app.model_dataset import STANDARD_FEATURES, _flatten_standard
from app.models import Fixture, Prediction
from app import prematch_inference as inference_v1
from app.training_dataset_full import build_full_training_dataset

INFERENCE_RUNTIME_VERSION = "inference_runtime_v2"
MAX_RUNTIME_DATASETS = 2


@dataclass
class _PreparedDataset:
    key: tuple[str, str]
    start_date: date
    end_date: date
    source_dataset_id: str | None
    source_dataset_sha256: str | None
    training_leagues: list[str]
    rows: list[dict[str, Any]]
    build_seconds: float


class InferenceRuntimeV2:
    """Cycle-local inference runtime that reuses prepared historical rows.

    The runtime preserves the V1 model, target features, chronological cutoff,
    training hash and persistence semantics. It only removes repeated work:
    within one J1 cycle, fixtures that resolve to the same historical date
    window share one full dataset build and one flatten/parse pass.
    """

    def __init__(
        self,
        *,
        history_days: int = inference_v1.DEFAULT_HISTORY_DAYS,
        lookback_matches: int = inference_v1.DEFAULT_LOOKBACK_MATCHES,
        min_history_matches: int = inference_v1.DEFAULT_MIN_HISTORY_MATCHES,
        min_training_rows: int = inference_v1.DEFAULT_MIN_TRAINING_ROWS,
        max_training_rows: int = inference_v1.DEFAULT_MAX_TRAINING_ROWS,
        class_weight_balanced: bool = False,
    ) -> None:
        if history_days < 90 or history_days > 3650:
            raise ValueError("history_days must be between 90 and 3650")
        if lookback_matches < 1 or lookback_matches > 10:
            raise ValueError("lookback_matches must be between 1 and 10")
        if min_history_matches < 1 or min_history_matches > lookback_matches:
            raise ValueError("min_history_matches must be between 1 and lookback_matches")
        if min_training_rows < 60 or min_training_rows > max_training_rows:
            raise ValueError("min_training_rows must be between 60 and max_training_rows")
        if max_training_rows < 100 or max_training_rows > 5000:
            raise ValueError("max_training_rows must be between 100 and 5000")

        self.history_days = int(history_days)
        self.lookback_matches = int(lookback_matches)
        self.min_history_matches = int(min_history_matches)
        self.min_training_rows = int(min_training_rows)
        self.max_training_rows = int(max_training_rows)
        self.class_weight_balanced = bool(class_weight_balanced)
        self.training_leagues = [
            str(item["canonical_name"])
            for item in TARGET_LEAGUES.values()
            if item.get("canonical_name")
        ]
        self._datasets: dict[tuple[str, str], _PreparedDataset] = {}
        self._stats: dict[str, float | int] = {
            "dataset_builds": 0,
            "dataset_reuses": 0,
            "training_views": 0,
            "fit_calls": 0,
            "predictions_persisted": 0,
            "predictions_reused": 0,
            "prepared_rows_total": 0,
            "dataset_build_seconds": 0.0,
            "fit_seconds": 0.0,
        }

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _dataset_key(self, target_starts_at: datetime) -> tuple[str, str]:
        target_starts_at = self._aware_utc(target_starts_at)
        start_date = (target_starts_at - timedelta(days=self.history_days)).date()
        end_date = target_starts_at.date()
        return start_date.isoformat(), end_date.isoformat()

    def _prepare_dataset(self, target_starts_at: datetime) -> tuple[_PreparedDataset, bool]:
        key = self._dataset_key(target_starts_at)
        cached = self._datasets.get(key)
        if cached is not None:
            self._stats["dataset_reuses"] = int(self._stats["dataset_reuses"]) + 1
            return cached, True

        if len(self._datasets) >= MAX_RUNTIME_DATASETS:
            oldest_key = next(iter(self._datasets))
            self._datasets.pop(oldest_key, None)

        start_date = date.fromisoformat(key[0])
        end_date = date.fromisoformat(key[1])
        started = perf_counter()
        dataset = build_full_training_dataset(
            start_date=start_date,
            end_date=end_date,
            leagues=self.training_leagues,
            lookback_matches=self.lookback_matches,
            min_history_matches=self.min_history_matches,
            include_skipped_details=False,
            max_rows=self.max_training_rows,
        )
        build_seconds = perf_counter() - started

        rows: list[dict[str, Any]] = []
        for row in dataset.get("rows") or []:
            starts_at = inference_v1._parse_iso_datetime(str(row["starts_at"]))
            label = row.get("label") or {}
            outcome = str(label.get("outcome_1x2") or "")
            if outcome not in inference_v1.CLASS_ORDER:
                continue
            rows.append(
                {
                    "fixture_id": row.get("fixture_id"),
                    "sportmonks_fixture_id": row.get("sportmonks_fixture_id"),
                    "starts_at": starts_at,
                    "X": _flatten_standard(row),
                    "y": outcome,
                }
            )

        prepared = _PreparedDataset(
            key=key,
            start_date=start_date,
            end_date=end_date,
            source_dataset_id=dataset.get("dataset_id"),
            source_dataset_sha256=dataset.get("dataset_sha256"),
            training_leagues=list(self.training_leagues),
            rows=rows,
            build_seconds=build_seconds,
        )
        self._datasets[key] = prepared
        self._stats["dataset_builds"] = int(self._stats["dataset_builds"]) + 1
        self._stats["prepared_rows_total"] = int(self._stats["prepared_rows_total"]) + len(rows)
        self._stats["dataset_build_seconds"] = round(
            float(self._stats["dataset_build_seconds"]) + build_seconds,
            6,
        )
        return prepared, False

    def _training_rows_before_target(
        self,
        target_starts_at: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        target_starts_at = self._aware_utc(target_starts_at)
        prepared, reused = self._prepare_dataset(target_starts_at)

        rows = [
            {
                "fixture_id": row.get("fixture_id"),
                "sportmonks_fixture_id": row.get("sportmonks_fixture_id"),
                "starts_at": row["starts_at"].isoformat(),
                "X": row["X"],
                "y": row["y"],
            }
            for row in prepared.rows
            if row["starts_at"] < target_starts_at
        ]
        self._stats["training_views"] = int(self._stats["training_views"]) + 1

        training_hash = inference_v1._stable_hash(
            {
                "inference_version": inference_v1.PREMATCH_INFERENCE_VERSION,
                "model_version": inference_v1.MODEL_VERSION,
                "feature_names": STANDARD_FEATURES,
                "lookback_matches": self.lookback_matches,
                "min_history_matches": self.min_history_matches,
                "target_cutoff": target_starts_at.isoformat(),
                "rows": rows,
            }
        )
        return rows, {
            "start_date": prepared.start_date.isoformat(),
            "end_date": prepared.end_date.isoformat(),
            "target_cutoff": target_starts_at.isoformat(),
            "training_rows": len(rows),
            "source_dataset_id": prepared.source_dataset_id,
            "source_dataset_sha256": prepared.source_dataset_sha256,
            "training_sha256": training_hash,
            "training_leagues": list(prepared.training_leagues),
            "strictly_before_target": True,
            "runtime": {
                "version": INFERENCE_RUNTIME_VERSION,
                "dataset_key": list(prepared.key),
                "dataset_reused": reused,
                "prepared_rows": len(prepared.rows),
                "dataset_build_seconds": round(prepared.build_seconds, 6),
                "training_view_rows": len(rows),
            },
        }

    def generate_and_persist_prediction(
        self,
        *,
        sportmonks_fixture_id: int,
        prediction_window: str = inference_v1.DEFAULT_PREDICTION_WINDOW,
    ) -> dict[str, Any]:
        prediction_window = str(prediction_window or "").strip()
        if not prediction_window or len(prediction_window) > 30:
            raise ValueError("prediction_window must contain 1 to 30 characters")

        with SessionLocal() as session:
            fixture = session.scalar(
                select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id)
            )
            if fixture is None:
                return {
                    "status": "fixture_not_found",
                    "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                    "runtime_version": INFERENCE_RUNTIME_VERSION,
                    "sportmonks_fixture_id": sportmonks_fixture_id,
                }

            fixture_data = inference_v1._fixture_payload(fixture)
            target_starts_at = self._aware_utc(fixture.starts_at)
            now = datetime.now(timezone.utc)
            if now >= target_starts_at:
                return {
                    "status": "not_ready",
                    "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                    "runtime_version": INFERENCE_RUNTIME_VERSION,
                    "reason_codes": ["FIXTURE_ALREADY_STARTED"],
                    "fixture": fixture_data,
                    "policy": {
                        "retroactive_prediction_persistence_allowed": False,
                        "audit_integrity_protected": True,
                    },
                }

            canonical = canonical_league(fixture.league_name)
            if not canonical.get("target"):
                return {
                    "status": "not_ready",
                    "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                    "runtime_version": INFERENCE_RUNTIME_VERSION,
                    "reason_codes": ["UNSUPPORTED_TARGET_LEAGUE"],
                    "fixture": fixture_data,
                }

            existing = session.scalar(
                select(Prediction)
                .where(
                    Prediction.fixture_id == fixture.id,
                    Prediction.prediction_window == prediction_window,
                    Prediction.model_version == inference_v1.MODEL_VERSION,
                )
                .order_by(Prediction.generated_at.desc(), Prediction.id.desc())
                .limit(1)
            )
            if existing is not None:
                self._stats["predictions_reused"] = int(self._stats["predictions_reused"]) + 1
                return {
                    "status": "exists",
                    "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                    "runtime_version": INFERENCE_RUNTIME_VERSION,
                    "fixture": fixture_data,
                    "prediction": inference_v1._prediction_payload(existing),
                    "policy": {
                        "prediction_immutable_once_persisted": True,
                        "recomputed": False,
                    },
                }

            target_features, target_audit = inference_v1._target_standard_features(
                session,
                fixture,
                self.lookback_matches,
                self.min_history_matches,
            )

        if target_features is None:
            return {
                "status": "not_ready",
                "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                "runtime_version": INFERENCE_RUNTIME_VERSION,
                "reason_codes": [str(target_audit.get("reason") or "TARGET_FEATURES_NOT_READY")],
                "fixture": fixture_data,
                "target_feature_audit": target_audit,
            }

        training_rows, training_audit = self._training_rows_before_target(target_starts_at)
        if len(training_rows) < self.min_training_rows:
            return {
                "status": "not_ready",
                "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                "runtime_version": INFERENCE_RUNTIME_VERSION,
                "reason_codes": ["INSUFFICIENT_MODEL_TRAINING_ROWS"],
                "fixture": fixture_data,
                "target_feature_audit": target_audit,
                "training_audit": {
                    **training_audit,
                    "minimum_required": self.min_training_rows,
                },
            }

        fit_started = perf_counter()
        probabilities, model_metadata = inference_v1._fit_and_predict(
            training_rows,
            target_features,
            self.class_weight_balanced,
        )
        fit_seconds = perf_counter() - fit_started
        self._stats["fit_calls"] = int(self._stats["fit_calls"]) + 1
        self._stats["fit_seconds"] = round(float(self._stats["fit_seconds"]) + fit_seconds, 6)

        with SessionLocal() as session:
            fixture = session.scalar(
                select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id)
            )
            if fixture is None:
                return {
                    "status": "fixture_not_found",
                    "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                    "runtime_version": INFERENCE_RUNTIME_VERSION,
                    "sportmonks_fixture_id": sportmonks_fixture_id,
                }
            if datetime.now(timezone.utc) >= self._aware_utc(fixture.starts_at):
                return {
                    "status": "not_ready",
                    "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                    "runtime_version": INFERENCE_RUNTIME_VERSION,
                    "reason_codes": ["FIXTURE_STARTED_DURING_INFERENCE"],
                    "fixture": inference_v1._fixture_payload(fixture),
                    "policy": {
                        "prediction_persisted": False,
                        "audit_integrity_protected": True,
                    },
                }

            existing = session.scalar(
                select(Prediction)
                .where(
                    Prediction.fixture_id == fixture.id,
                    Prediction.prediction_window == prediction_window,
                    Prediction.model_version == inference_v1.MODEL_VERSION,
                )
                .limit(1)
            )
            if existing is not None:
                self._stats["predictions_reused"] = int(self._stats["predictions_reused"]) + 1
                return {
                    "status": "exists",
                    "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                    "runtime_version": INFERENCE_RUNTIME_VERSION,
                    "fixture": inference_v1._fixture_payload(fixture),
                    "prediction": inference_v1._prediction_payload(existing),
                    "training_audit": training_audit,
                    "policy": {
                        "prediction_immutable_once_persisted": True,
                        "recomputed": False,
                    },
                }

            prediction = Prediction(
                fixture_id=int(fixture.id),
                prediction_window=prediction_window,
                model_version=inference_v1.MODEL_VERSION,
                p_home=Decimal(f"{probabilities['1']:.6f}"),
                p_draw=Decimal(f"{probabilities['X']:.6f}"),
                p_away=Decimal(f"{probabilities['2']:.6f}"),
            )
            session.add(prediction)
            try:
                session.commit()
                session.refresh(prediction)
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(Prediction)
                    .where(
                        Prediction.fixture_id == fixture.id,
                        Prediction.prediction_window == prediction_window,
                        Prediction.model_version == inference_v1.MODEL_VERSION,
                    )
                    .limit(1)
                )
                if existing is None:
                    raise
                self._stats["predictions_reused"] = int(self._stats["predictions_reused"]) + 1
                return {
                    "status": "exists",
                    "version": inference_v1.PREMATCH_INFERENCE_VERSION,
                    "runtime_version": INFERENCE_RUNTIME_VERSION,
                    "fixture": inference_v1._fixture_payload(fixture),
                    "prediction": inference_v1._prediction_payload(existing),
                    "training_audit": training_audit,
                    "policy": {
                        "prediction_immutable_once_persisted": True,
                        "concurrent_insert_resolved": True,
                    },
                }

            persisted = inference_v1._prediction_payload(prediction)
            self._stats["predictions_persisted"] = int(self._stats["predictions_persisted"]) + 1

        return {
            "status": "ok",
            "version": inference_v1.PREMATCH_INFERENCE_VERSION,
            "runtime_version": INFERENCE_RUNTIME_VERSION,
            "fixture": fixture_data,
            "prediction": persisted,
            "model": {
                "model_version": inference_v1.MODEL_VERSION,
                **model_metadata,
            },
            "target_feature_audit": target_audit,
            "training_audit": {
                **training_audit,
                "minimum_required": self.min_training_rows,
                "fit_seconds": round(fit_seconds, 6),
            },
            "probability_audit": {
                "sum": round(sum(probabilities.values()), 12),
                "class_order": inference_v1.CLASS_ORDER,
                "raw_probabilities": {
                    "1": round(probabilities["1"], 6),
                    "X": round(probabilities["X"], 6),
                    "2": round(probabilities["2"], 6),
                },
            },
            "policy": {
                "pre_match_only": True,
                "retroactive_prediction_persistence_allowed": False,
                "target_match_snapshot_read": False,
                "target_match_postgame_data_used": False,
                "history_strictly_before_target": True,
                "prediction_immutable_once_persisted": True,
                "hyperparameter_search_at_inference_time": False,
                "test_set_optimization_at_inference_time": False,
                "family": "STANDARD",
                "decision_engine_compatible": True,
                "historical_dataset_reused_within_j1_cycle": True,
                "model_fit_reused_across_distinct_cutoffs": False,
            },
        }

    def audit(self) -> dict[str, Any]:
        return {
            "version": INFERENCE_RUNTIME_VERSION,
            "dataset_cache_entries": len(self._datasets),
            "dataset_cache_max_entries": MAX_RUNTIME_DATASETS,
            "dataset_builds": int(self._stats["dataset_builds"]),
            "dataset_reuses": int(self._stats["dataset_reuses"]),
            "training_views": int(self._stats["training_views"]),
            "fit_calls": int(self._stats["fit_calls"]),
            "predictions_persisted": int(self._stats["predictions_persisted"]),
            "predictions_reused": int(self._stats["predictions_reused"]),
            "prepared_rows_total": int(self._stats["prepared_rows_total"]),
            "dataset_build_seconds": round(float(self._stats["dataset_build_seconds"]), 6),
            "fit_seconds": round(float(self._stats["fit_seconds"]), 6),
            "policy": {
                "scope": "single_j1_cycle",
                "reuse_unit": "historical_dataset_and_flattened_rows",
                "per_fixture_target_features_preserved": True,
                "per_fixture_strict_temporal_cutoff_preserved": True,
                "training_hash_semantics_preserved": True,
                "standard_36_features_unchanged": True,
                "model_version_unchanged": True,
                "probability_method_unchanged": True,
            },
        }
