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

BASELINE_1X2_VERSION = "baseline_1x2_temporal_v1"
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


def _metrics(y_true: np.ndarray, y_pred: np.ndarray, probabilities: np.ndarray) -> dict:
    cm = confusion_matrix(y_true, y_pred, labels=CLASS_ORDER)
    return {
        "rows": int(len(y_true)),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 6),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), 6),
        "f1_macro": round(float(f1_score(y_true, y_pred, labels=CLASS_ORDER, average="macro", zero_division=0)), 6),
        "log_loss": round(float(log_loss(y_true, probabilities, labels=CLASS_ORDER)), 6),
        "confusion_matrix": {
            "labels": CLASS_ORDER,
            "matrix": cm.tolist(),
        },
    }


def _always_home_metrics(y_true: np.ndarray) -> dict:
    pred = np.asarray(["1"] * len(y_true), dtype=object)
    cm = confusion_matrix(y_true, pred, labels=CLASS_ORDER)
    return {
        "rows": int(len(y_true)),
        "accuracy": round(float(accuracy_score(y_true, pred)), 6),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, pred)), 6),
        "f1_macro": round(float(f1_score(y_true, pred, labels=CLASS_ORDER, average="macro", zero_division=0)), 6),
        "confusion_matrix": {
            "labels": CLASS_ORDER,
            "matrix": cm.tolist(),
        },
    }


def _top_coefficients(model: Pipeline, feature_names: list[str], limit: int = 10) -> dict[str, list[dict]]:
    classifier = model.named_steps["classifier"]
    result: dict[str, list[dict]] = {}
    for class_name, coefs in zip(classifier.classes_, classifier.coef_):
        ranked = sorted(zip(feature_names, coefs), key=lambda item: abs(float(item[1])), reverse=True)[:limit]
        result[str(class_name)] = [
            {"feature": feature, "coefficient": round(float(value), 6)} for feature, value in ranked
        ]
    return result


def _prediction_rows(rows: list[dict], y_pred: np.ndarray, probabilities: np.ndarray) -> list[dict]:
    items: list[dict] = []
    for row, pred, probs in zip(rows, y_pred, probabilities):
        items.append(
            {
                "fixture_id": row.get("fixture_id"),
                "sportmonks_fixture_id": row.get("sportmonks_fixture_id"),
                "starts_at": row.get("starts_at"),
                "league": row.get("league"),
                "home_team": row.get("home_team"),
                "away_team": row.get("away_team"),
                "actual": (row.get("y") or {}).get("outcome_1x2"),
                "predicted": str(pred),
                "probabilities": {
                    "1": round(float(probs[0]), 6),
                    "X": round(float(probs[1]), 6),
                    "2": round(float(probs[2]), 6),
                },
            }
        )
    return items


def build_baseline_1x2_temporal_v1(
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
        raise ValueError("baseline requires non-empty train, validation, and test partitions")

    X_train, y_train = _matrix(train_rows, feature_names)
    X_validation, y_validation = _matrix(validation_rows, feature_names)
    X_test, y_test = _matrix(test_rows, feature_names)

    train_classes = sorted(set(y_train.tolist()))
    if set(train_classes) != set(CLASS_ORDER):
        raise ValueError(f"train partition must contain all 1X2 classes; found {train_classes}")

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

    validation_pred = pipeline.predict(X_validation)
    test_pred = pipeline.predict(X_test)
    validation_proba = _ordered_probabilities(pipeline, X_validation)
    test_proba = _ordered_probabilities(pipeline, X_test)

    validation_metrics = _metrics(y_validation, validation_pred, validation_proba)
    test_metrics = _metrics(y_test, test_pred, test_proba)
    validation_naive = _always_home_metrics(y_validation)
    test_naive = _always_home_metrics(y_test)

    baseline_metadata = {
        "version": BASELINE_1X2_VERSION,
        "model_dataset_sha256": model_dataset["model_dataset_sha256"],
        "family": family,
        "family_sha256": family_payload["family_sha256"],
        "feature_names": feature_names,
        "class_weight_balanced": class_weight_balanced,
        "algorithm": "multinomial_logistic_regression",
        "C": 1.0,
        "penalty": "l2",
        "solver": "lbfgs",
        "random_state": 42,
    }
    baseline_sha = _stable_hash(baseline_metadata)

    response = {
        "status": "ok",
        "version": BASELINE_1X2_VERSION,
        "baseline_id": f"{BASELINE_1X2_VERSION}:{baseline_sha[:16]}",
        "baseline_sha256": baseline_sha,
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
            "pipeline": ["median_imputer", "standard_scaler", "logistic_regression"],
            "feature_count": len(feature_names),
            "train_rows": len(train_rows),
            "validation_rows": len(validation_rows),
            "test_rows": len(test_rows),
            "class_order": CLASS_ORDER,
            "class_weight": "balanced" if class_weight_balanced else None,
            "hyperparameters": {
                "C": 1.0,
                "penalty": "l2",
                "solver": "lbfgs",
                "max_iter": 2000,
                "random_state": 42,
            },
            "fit_scope": "train partition only",
        },
        "validation": {
            "model": validation_metrics,
            "always_home": validation_naive,
            "accuracy_lift_vs_always_home": round(validation_metrics["accuracy"] - validation_naive["accuracy"], 6),
        },
        "test": {
            "model": test_metrics,
            "always_home": test_naive,
            "accuracy_lift_vs_always_home": round(test_metrics["accuracy"] - test_naive["accuracy"], 6),
        },
        "top_coefficients_by_class": _top_coefficients(pipeline, feature_names),
        "policy": {
            "temporal_split_preserved": True,
            "shuffle": False,
            "imputer_fit_on_train_only": True,
            "scaler_fit_on_train_only": True,
            "classifier_fit_on_train_only": True,
            "validation_used_for_fit": False,
            "test_used_for_fit": False,
            "hyperparameter_search": False,
            "target_match_postgame_data_as_features": False,
            "deterministic": True,
        },
    }
    if include_predictions:
        response["predictions"] = {
            "validation": _prediction_rows(validation_rows, validation_pred, validation_proba),
            "test": _prediction_rows(test_rows, test_pred, test_proba),
        }
    return response
