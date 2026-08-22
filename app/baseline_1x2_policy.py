from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, f1_score, log_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.model_dataset import build_model_dataset_v1

BASELINE_POLICY_VERSION = "baseline_1x2_confidence_policy_v1"
CLASS_ORDER = ["1", "X", "2"]


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _matrix(rows: list[dict], feature_names: list[str]) -> tuple[np.ndarray, np.ndarray]:
    X = np.asarray([[row["X"].get(name) for name in feature_names] for row in rows], dtype=float)
    y = np.asarray([str((row.get("y") or {}).get("outcome_1x2")) for row in rows], dtype=object)
    return X, y


def _ordered_probabilities(model: Pipeline, X: np.ndarray) -> np.ndarray:
    raw = model.predict_proba(X)
    classes = list(model.named_steps["classifier"].classes_)
    return np.column_stack([raw[:, classes.index(label)] for label in CLASS_ORDER])


def _strict_metrics(y_true: np.ndarray, probabilities: np.ndarray) -> dict:
    idx = np.argmax(probabilities, axis=1)
    pred = np.asarray([CLASS_ORDER[int(i)] for i in idx], dtype=object)
    cm = confusion_matrix(y_true, pred, labels=CLASS_ORDER)
    return {
        "rows": int(len(y_true)),
        "accuracy": round(float(accuracy_score(y_true, pred)), 6),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, pred)), 6),
        "f1_macro": round(float(f1_score(y_true, pred, labels=CLASS_ORDER, average="macro", zero_division=0)), 6),
        "log_loss": round(float(log_loss(y_true, probabilities, labels=CLASS_ORDER)), 6),
        "confusion_matrix": {"labels": CLASS_ORDER, "matrix": cm.tolist()},
    }


def _tier(favorite_probability: float) -> str:
    if favorite_probability >= 0.65:
        return "STRONG_FAVORITE"
    if favorite_probability >= 0.55:
        return "EFFECTIVE_FAVORITE"
    if favorite_probability >= 0.45:
        return "MODERATE_FAVORITE"
    return "OPEN_GAME"


def _decision_for_probabilities(probs: np.ndarray) -> dict:
    p_home, p_draw, p_away = [float(v) for v in probs]
    favorite = "1" if p_home >= p_away else "2"
    favorite_probability = p_home if favorite == "1" else p_away
    tier = _tier(favorite_probability)

    if tier in {"STRONG_FAVORITE", "EFFECTIVE_FAVORITE"}:
        accepted = [favorite]
        recommendation = favorite
        combined_probability = favorite_probability
        decision_type = "single"
    else:
        accepted = [favorite, "X"]
        recommendation = "1X" if favorite == "1" else "X2"
        combined_probability = favorite_probability + p_draw
        decision_type = "double_chance"

    return {
        "tier": tier,
        "favorite": favorite,
        "favorite_probability": favorite_probability,
        "decision_type": decision_type,
        "recommendation": recommendation,
        "accepted_outcomes": accepted,
        "recommendation_probability": combined_probability,
        "probabilities": {"1": p_home, "X": p_draw, "2": p_away},
    }


def _policy_evaluation(rows: list[dict], y_true: np.ndarray, probabilities: np.ndarray, include_predictions: bool) -> dict:
    tier_names = ["STRONG_FAVORITE", "EFFECTIVE_FAVORITE", "MODERATE_FAVORITE", "OPEN_GAME"]
    tier_stats = {
        name: {
            "rows": 0,
            "hits": 0,
            "single_rows": 0,
            "double_chance_rows": 0,
            "probability_sum": 0.0,
        }
        for name in tier_names
    }

    total_hits = 0
    single_rows = 0
    single_hits = 0
    dc_rows = 0
    dc_hits = 0
    recommendation_probability_sum = 0.0
    prediction_rows: list[dict] = []

    for row, actual, probs in zip(rows, y_true, probabilities):
        decision = _decision_for_probabilities(probs)
        hit = str(actual) in decision["accepted_outcomes"]
        total_hits += int(hit)
        recommendation_probability_sum += decision["recommendation_probability"]

        stats = tier_stats[decision["tier"]]
        stats["rows"] += 1
        stats["hits"] += int(hit)
        stats["probability_sum"] += decision["recommendation_probability"]

        if decision["decision_type"] == "single":
            single_rows += 1
            single_hits += int(hit)
            stats["single_rows"] += 1
        else:
            dc_rows += 1
            dc_hits += int(hit)
            stats["double_chance_rows"] += 1

        if include_predictions:
            prediction_rows.append(
                {
                    "fixture_id": row.get("fixture_id"),
                    "sportmonks_fixture_id": row.get("sportmonks_fixture_id"),
                    "starts_at": row.get("starts_at"),
                    "league": row.get("league"),
                    "home_team": row.get("home_team"),
                    "away_team": row.get("away_team"),
                    "actual": str(actual),
                    "tier": decision["tier"],
                    "favorite": decision["favorite"],
                    "favorite_probability": round(decision["favorite_probability"], 6),
                    "decision_type": decision["decision_type"],
                    "recommendation": decision["recommendation"],
                    "recommendation_probability": round(decision["recommendation_probability"], 6),
                    "hit": bool(hit),
                    "probabilities": {k: round(v, 6) for k, v in decision["probabilities"].items()},
                }
            )

    tiers = {}
    for name in tier_names:
        stats = tier_stats[name]
        rows_count = stats["rows"]
        tiers[name] = {
            "rows": rows_count,
            "share_pct": round(rows_count / len(y_true) * 100, 2) if len(y_true) else 0.0,
            "hits": stats["hits"],
            "hit_rate": round(stats["hits"] / rows_count, 6) if rows_count else None,
            "single_rows": stats["single_rows"],
            "double_chance_rows": stats["double_chance_rows"],
            "avg_recommendation_probability": round(stats["probability_sum"] / rows_count, 6) if rows_count else None,
        }

    result = {
        "rows": int(len(y_true)),
        "hits": int(total_hits),
        "policy_hit_rate": round(total_hits / len(y_true), 6),
        "avg_recommendation_probability": round(recommendation_probability_sum / len(y_true), 6),
        "single": {
            "rows": single_rows,
            "share_pct": round(single_rows / len(y_true) * 100, 2),
            "hits": single_hits,
            "hit_rate": round(single_hits / single_rows, 6) if single_rows else None,
        },
        "double_chance": {
            "rows": dc_rows,
            "share_pct": round(dc_rows / len(y_true) * 100, 2),
            "hits": dc_hits,
            "hit_rate": round(dc_hits / dc_rows, 6) if dc_rows else None,
        },
        "tiers": tiers,
    }
    if include_predictions:
        result["predictions"] = prediction_rows
    return result


def build_baseline_1x2_confidence_policy_v1(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    family: str = "STANDARD",
    lookback_matches: int = 5,
    min_history_matches: int = 3,
    train_ratio: float = 0.70,
    validation_ratio: float = 0.15,
    max_rows: int = 5000,
    class_weight_balanced: bool = False,
    include_predictions: bool = False,
) -> dict:
    family = family.strip().upper()
    if family not in {"STANDARD", "FULL_XG"}:
        raise ValueError("family must be STANDARD or FULL_XG")

    model_dataset = build_model_dataset_v1(
        start_date=start_date,
        end_date=end_date,
        leagues=leagues,
        lookback_matches=lookback_matches,
        min_history_matches=min_history_matches,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
        max_rows=max_rows,
        include_rows=True,
    )

    family_payload = model_dataset["families"][family]
    feature_names = list(family_payload["feature_names"])
    train_rows = family_payload["partitions"]["train"].get("rows") or []
    validation_rows = family_payload["partitions"]["validation"].get("rows") or []
    test_rows = family_payload["partitions"]["test"].get("rows") or []
    if not train_rows or not validation_rows or not test_rows:
        raise ValueError("policy baseline requires non-empty train, validation, and test partitions")

    X_train, y_train = _matrix(train_rows, feature_names)
    X_validation, y_validation = _matrix(validation_rows, feature_names)
    X_test, y_test = _matrix(test_rows, feature_names)
    if set(y_train.tolist()) != set(CLASS_ORDER):
        raise ValueError("train partition must contain all 1X2 classes")

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

    validation_proba = _ordered_probabilities(pipeline, X_validation)
    test_proba = _ordered_probabilities(pipeline, X_test)
    validation_strict = _strict_metrics(y_validation, validation_proba)
    test_strict = _strict_metrics(y_test, test_proba)
    validation_policy = _policy_evaluation(validation_rows, y_validation, validation_proba, include_predictions)
    test_policy = _policy_evaluation(test_rows, y_test, test_proba, include_predictions)

    metadata = {
        "version": BASELINE_POLICY_VERSION,
        "model_dataset_sha256": model_dataset["model_dataset_sha256"],
        "family_sha256": family_payload["family_sha256"],
        "family": family,
        "class_weight_balanced": class_weight_balanced,
        "thresholds": {"strong": 0.65, "effective": 0.55, "moderate": 0.45},
        "moderate_action": "favorite_plus_draw",
        "open_action": "favorite_plus_draw",
    }
    policy_sha = _stable_hash(metadata)

    return {
        "status": "ok",
        "version": BASELINE_POLICY_VERSION,
        "policy_id": f"{BASELINE_POLICY_VERSION}:{policy_sha[:16]}",
        "policy_sha256": policy_sha,
        "family": family,
        "parent": {
            "model_dataset_id": model_dataset["model_dataset_id"],
            "model_dataset_sha256": model_dataset["model_dataset_sha256"],
            "family_sha256": family_payload["family_sha256"],
            "split_sha256": model_dataset["parent"]["split_sha256"],
            "dataset_sha256": model_dataset["parent"]["dataset_sha256"],
        },
        "training": {
            "algorithm": "multinomial_logistic_regression",
            "feature_count": len(feature_names),
            "train_rows": len(train_rows),
            "validation_rows": len(validation_rows),
            "test_rows": len(test_rows),
            "class_weight": "balanced" if class_weight_balanced else None,
            "fit_scope": "train partition only",
        },
        "decision_policy": {
            "STRONG_FAVORITE": {"favorite_probability": ">=0.65", "action": "single favorite"},
            "EFFECTIVE_FAVORITE": {"favorite_probability": "0.55-0.649999", "action": "single favorite"},
            "MODERATE_FAVORITE": {"favorite_probability": "0.45-0.549999", "action": "double chance favorite+draw (1X/X2)"},
            "OPEN_GAME": {"favorite_probability": "<0.45", "action": "double chance favorite+draw (1X/X2)"},
            "probabilities_are_not_clamped_or_rewritten": True,
        },
        "validation": {"strict_1x2": validation_strict, "policy": validation_policy},
        "test": {"strict_1x2": test_strict, "policy": test_policy},
        "policy": {
            "temporal_split_preserved": True,
            "shuffle": False,
            "train_only_fit": True,
            "validation_used_for_fit": False,
            "test_used_for_fit": False,
            "thresholds_are_fixed_before_evaluation": True,
            "double_chance_is_evaluation_of_accepted_outcomes_not_probability_recalibration": True,
            "target_match_postgame_data_as_features": False,
            "deterministic": True,
        },
    }
