from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.league_registry import TARGET_LEAGUES, canonical_league
from app.model_dataset import STANDARD_FEATURES, _flatten_standard
from app.models import Fixture, Prediction
from app.training_dataset import _aggregate_history, _delta, _team_history
from app.training_dataset_full import build_full_training_dataset

PREMATCH_INFERENCE_VERSION = "prematch_inference_v1"
MODEL_VERSION = "baseline_1x2_temporal_v1"
DEFAULT_PREDICTION_WINDOW = "prematch_v1"
CLASS_ORDER = ["1", "X", "2"]

DEFAULT_HISTORY_DAYS = 730
DEFAULT_LOOKBACK_MATCHES = 5
DEFAULT_MIN_HISTORY_MATCHES = 3
DEFAULT_MIN_TRAINING_ROWS = 120
DEFAULT_MAX_TRAINING_ROWS = 5000


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _aware_utc(parsed)


def _fixture_payload(fixture: Fixture) -> dict[str, Any]:
    return {
        "fixture_id": int(fixture.id),
        "sportmonks_fixture_id": int(fixture.sportmonks_id),
        "league": fixture.league_name,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
        "status": fixture.status,
    }


def _prediction_payload(prediction: Prediction) -> dict[str, Any]:
    return {
        "prediction_id": int(prediction.id),
        "fixture_id": int(prediction.fixture_id),
        "prediction_window": prediction.prediction_window,
        "model_version": prediction.model_version,
        "p_home": round(float(prediction.p_home), 6),
        "p_draw": round(float(prediction.p_draw), 6),
        "p_away": round(float(prediction.p_away), 6),
        "generated_at": prediction.generated_at.isoformat() if prediction.generated_at else None,
    }


def _target_standard_features(
    session,
    fixture: Fixture,
    lookback_matches: int,
    min_history_matches: int,
) -> tuple[dict[str, float | None] | None, dict[str, Any]]:
    canonical = canonical_league(fixture.league_name)
    league_key = canonical.get("key")
    if not canonical.get("target") or not league_key:
        return None, {
            "reason": "UNSUPPORTED_TARGET_LEAGUE",
            "league": fixture.league_name,
        }

    home_history = _team_history(
        session,
        fixture,
        fixture.home_team,
        str(league_key),
        lookback_matches,
    )
    away_history = _team_history(
        session,
        fixture,
        fixture.away_team,
        str(league_key),
        lookback_matches,
    )

    if len(home_history) < min_history_matches or len(away_history) < min_history_matches:
        return None, {
            "reason": "INSUFFICIENT_TARGET_HISTORY",
            "home_history_matches": len(home_history),
            "away_history_matches": len(away_history),
            "minimum_required": min_history_matches,
            "requested_lookback": lookback_matches,
        }

    target_starts_at = _aware_utc(fixture.starts_at)
    historical_times = [
        _aware_utc(row["starts_at"])
        for row in home_history + away_history
        if row.get("starts_at") is not None
    ]
    if any(value >= target_starts_at for value in historical_times):
        return None, {
            "reason": "TARGET_FEATURE_LEAKAGE_VIOLATION",
        }

    home = _aggregate_history(home_history, fixture.starts_at)
    away = _aggregate_history(away_history, fixture.starts_at)
    home["history_completeness_ratio"] = round(len(home_history) / lookback_matches, 4)
    away["history_completeness_ratio"] = round(len(away_history) / lookback_matches, 4)

    nested_row = {
        "features": {
            "home": home,
            "away": away,
            "delta": {
                "points_per_match": _delta(home, away, "points_per_match"),
                "goals_for_avg": _delta(home, away, "goals_for_avg"),
                "goals_against_avg": _delta(home, away, "goals_against_avg"),
                "shots_total_for_avg": _delta(home, away, "shots_total_for_avg"),
                "shots_on_target_for_avg": _delta(home, away, "shots_on_target_for_avg"),
                "possession_avg": _delta(home, away, "possession_avg"),
                "corners_for_avg": _delta(home, away, "corners_for_avg"),
                "successful_passes_for_avg": _delta(home, away, "successful_passes_for_avg"),
                "xg_for_avg": _delta(home, away, "xg_for_avg"),
                "rest_days": _delta(home, away, "rest_days"),
                "history_completeness_ratio": _delta(home, away, "history_completeness_ratio"),
            },
        }
    }
    flat = _flatten_standard(nested_row)
    return flat, {
        "reason": None,
        "canonical_league": canonical.get("canonical_name"),
        "league_key": league_key,
        "home_history_matches": len(home_history),
        "away_history_matches": len(away_history),
        "home_latest_history_starts_at": home.get("latest_history_starts_at"),
        "away_latest_history_starts_at": away.get("latest_history_starts_at"),
        "history_strictly_before_target": True,
        "target_match_snapshot_read": False,
        "target_match_postgame_data_used": False,
    }


def _training_rows_before_target(
    *,
    target_starts_at: datetime,
    history_days: int,
    lookback_matches: int,
    min_history_matches: int,
    max_training_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    target_starts_at = _aware_utc(target_starts_at)
    start_date = (target_starts_at - timedelta(days=history_days)).date()
    end_date = target_starts_at.date()
    target_leagues = [
        str(item["canonical_name"])
        for item in TARGET_LEAGUES.values()
        if item.get("canonical_name")
    ]

    dataset = build_full_training_dataset(
        start_date=start_date,
        end_date=end_date,
        leagues=target_leagues,
        lookback_matches=lookback_matches,
        min_history_matches=min_history_matches,
        include_skipped_details=False,
        max_rows=max_training_rows,
    )

    rows: list[dict[str, Any]] = []
    for row in dataset.get("rows") or []:
        starts_at = _parse_iso_datetime(str(row["starts_at"]))
        if starts_at >= target_starts_at:
            continue
        label = row.get("label") or {}
        outcome = str(label.get("outcome_1x2") or "")
        if outcome not in CLASS_ORDER:
            continue
        rows.append(
            {
                "fixture_id": row.get("fixture_id"),
                "sportmonks_fixture_id": row.get("sportmonks_fixture_id"),
                "starts_at": starts_at.isoformat(),
                "X": _flatten_standard(row),
                "y": outcome,
            }
        )

    training_hash = _stable_hash(
        {
            "inference_version": PREMATCH_INFERENCE_VERSION,
            "model_version": MODEL_VERSION,
            "feature_names": STANDARD_FEATURES,
            "lookback_matches": lookback_matches,
            "min_history_matches": min_history_matches,
            "target_cutoff": target_starts_at.isoformat(),
            "rows": rows,
        }
    )
    return rows, {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "target_cutoff": target_starts_at.isoformat(),
        "training_rows": len(rows),
        "source_dataset_id": dataset.get("dataset_id"),
        "source_dataset_sha256": dataset.get("dataset_sha256"),
        "training_sha256": training_hash,
        "training_leagues": target_leagues,
        "strictly_before_target": True,
    }


def _fit_and_predict(
    training_rows: list[dict[str, Any]],
    target_features: dict[str, float | None],
    class_weight_balanced: bool,
) -> tuple[dict[str, float], dict[str, Any]]:
    X_train = np.asarray(
        [[row["X"].get(name) for name in STANDARD_FEATURES] for row in training_rows],
        dtype=float,
    )
    y_train = np.asarray([row["y"] for row in training_rows], dtype=object)
    train_classes = sorted(set(y_train.tolist()))
    if set(train_classes) != set(CLASS_ORDER):
        raise ValueError(f"training rows must contain all 1X2 classes; found {train_classes}")

    target_matrix = np.asarray(
        [[target_features.get(name) for name in STANDARD_FEATURES]],
        dtype=float,
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
                    class_weight="balanced" if class_weight_balanced else None,
                    random_state=42,
                ),
            ),
        ]
    )
    pipeline.fit(X_train, y_train)
    raw = pipeline.predict_proba(target_matrix)[0]
    classes = list(pipeline.named_steps["classifier"].classes_)
    ordered = {
        label: float(raw[classes.index(label)])
        for label in CLASS_ORDER
    }
    total = sum(ordered.values())
    probabilities = {
        label: ordered[label] / total
        for label in CLASS_ORDER
    }
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
        "class_weight": "balanced" if class_weight_balanced else None,
        "random_state": 42,
        "fit_scope": "all eligible historical rows strictly before target kickoff",
        "production_refit": True,
    }
    return probabilities, metadata


def generate_and_persist_prematch_prediction(
    *,
    sportmonks_fixture_id: int,
    prediction_window: str = DEFAULT_PREDICTION_WINDOW,
    history_days: int = DEFAULT_HISTORY_DAYS,
    lookback_matches: int = DEFAULT_LOOKBACK_MATCHES,
    min_history_matches: int = DEFAULT_MIN_HISTORY_MATCHES,
    min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
    max_training_rows: int = DEFAULT_MAX_TRAINING_ROWS,
    class_weight_balanced: bool = False,
) -> dict[str, Any]:
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
                "version": PREMATCH_INFERENCE_VERSION,
                "sportmonks_fixture_id": sportmonks_fixture_id,
            }

        fixture_data = _fixture_payload(fixture)
        target_starts_at = _aware_utc(fixture.starts_at)
        now = datetime.now(timezone.utc)
        if now >= target_starts_at:
            return {
                "status": "not_ready",
                "version": PREMATCH_INFERENCE_VERSION,
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
                "version": PREMATCH_INFERENCE_VERSION,
                "reason_codes": ["UNSUPPORTED_TARGET_LEAGUE"],
                "fixture": fixture_data,
            }

        existing = session.scalar(
            select(Prediction)
            .where(
                Prediction.fixture_id == fixture.id,
                Prediction.prediction_window == prediction_window,
                Prediction.model_version == MODEL_VERSION,
            )
            .order_by(Prediction.generated_at.desc(), Prediction.id.desc())
            .limit(1)
        )
        if existing is not None:
            return {
                "status": "exists",
                "version": PREMATCH_INFERENCE_VERSION,
                "fixture": fixture_data,
                "prediction": _prediction_payload(existing),
                "policy": {
                    "prediction_immutable_once_persisted": True,
                    "recomputed": False,
                },
            }

        target_features, target_audit = _target_standard_features(
            session,
            fixture,
            lookback_matches,
            min_history_matches,
        )

    if target_features is None:
        return {
            "status": "not_ready",
            "version": PREMATCH_INFERENCE_VERSION,
            "reason_codes": [str(target_audit.get("reason") or "TARGET_FEATURES_NOT_READY")],
            "fixture": fixture_data,
            "target_feature_audit": target_audit,
        }

    training_rows, training_audit = _training_rows_before_target(
        target_starts_at=target_starts_at,
        history_days=history_days,
        lookback_matches=lookback_matches,
        min_history_matches=min_history_matches,
        max_training_rows=max_training_rows,
    )
    if len(training_rows) < min_training_rows:
        return {
            "status": "not_ready",
            "version": PREMATCH_INFERENCE_VERSION,
            "reason_codes": ["INSUFFICIENT_MODEL_TRAINING_ROWS"],
            "fixture": fixture_data,
            "target_feature_audit": target_audit,
            "training_audit": {
                **training_audit,
                "minimum_required": min_training_rows,
            },
        }

    probabilities, model_metadata = _fit_and_predict(
        training_rows,
        target_features,
        class_weight_balanced,
    )

    with SessionLocal() as session:
        fixture = session.scalar(
            select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id)
        )
        if fixture is None:
            return {
                "status": "fixture_not_found",
                "version": PREMATCH_INFERENCE_VERSION,
                "sportmonks_fixture_id": sportmonks_fixture_id,
            }
        if datetime.now(timezone.utc) >= _aware_utc(fixture.starts_at):
            return {
                "status": "not_ready",
                "version": PREMATCH_INFERENCE_VERSION,
                "reason_codes": ["FIXTURE_STARTED_DURING_INFERENCE"],
                "fixture": _fixture_payload(fixture),
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
                Prediction.model_version == MODEL_VERSION,
            )
            .limit(1)
        )
        if existing is not None:
            return {
                "status": "exists",
                "version": PREMATCH_INFERENCE_VERSION,
                "fixture": _fixture_payload(fixture),
                "prediction": _prediction_payload(existing),
                "policy": {
                    "prediction_immutable_once_persisted": True,
                    "recomputed": False,
                },
            }

        prediction = Prediction(
            fixture_id=int(fixture.id),
            prediction_window=prediction_window,
            model_version=MODEL_VERSION,
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
                    Prediction.model_version == MODEL_VERSION,
                )
                .limit(1)
            )
            if existing is None:
                raise
            return {
                "status": "exists",
                "version": PREMATCH_INFERENCE_VERSION,
                "fixture": _fixture_payload(fixture),
                "prediction": _prediction_payload(existing),
                "policy": {
                    "prediction_immutable_once_persisted": True,
                    "concurrent_insert_resolved": True,
                },
            }

        persisted = _prediction_payload(prediction)

    return {
        "status": "ok",
        "version": PREMATCH_INFERENCE_VERSION,
        "fixture": fixture_data,
        "prediction": persisted,
        "model": {
            "model_version": MODEL_VERSION,
            **model_metadata,
        },
        "target_feature_audit": target_audit,
        "training_audit": {
            **training_audit,
            "minimum_required": min_training_rows,
        },
        "probability_audit": {
            "sum": round(sum(probabilities.values()), 12),
            "class_order": CLASS_ORDER,
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
        },
    }


def get_fixture_predictions(
    *,
    sportmonks_fixture_id: int,
    prediction_window: str | None = None,
    model_version: str | None = None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        fixture = session.scalar(
            select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id)
        )
        if fixture is None:
            return {
                "status": "fixture_not_found",
                "sportmonks_fixture_id": sportmonks_fixture_id,
            }

        stmt = select(Prediction).where(Prediction.fixture_id == fixture.id)
        if prediction_window:
            stmt = stmt.where(Prediction.prediction_window == prediction_window)
        if model_version:
            stmt = stmt.where(Prediction.model_version == model_version)
        rows = session.scalars(
            stmt.order_by(Prediction.generated_at.desc(), Prediction.id.desc())
        ).all()

        return {
            "status": "ok",
            "version": PREMATCH_INFERENCE_VERSION,
            "fixture": _fixture_payload(fixture),
            "prediction_count": len(rows),
            "predictions": [_prediction_payload(row) for row in rows],
        }
