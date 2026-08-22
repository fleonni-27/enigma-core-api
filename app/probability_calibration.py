from __future__ import annotations

import hashlib
import json
from datetime import date
from math import exp
from typing import Any

import numpy as np
from scipy.optimize import minimize_scalar
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.model_dataset import build_model_dataset_v1

PROBABILITY_CALIBRATION_VERSION = "probability_calibration_v1"
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


def _chronological_inner_split(rows: list[dict], calibration_ratio: float) -> tuple[list[dict], list[dict], dict]:
    if not 0.10 <= calibration_ratio <= 0.40:
        raise ValueError("calibration_ratio must be between 0.10 and 0.40")
    if len(rows) < 60:
        raise ValueError("probability calibration requires at least 60 training rows")

    rows = sorted(rows, key=lambda r: (str(r.get("starts_at")), int(r.get("fixture_id") or 0)))
    target_fit = max(1, int(round(len(rows) * (1.0 - calibration_ratio))))
    target_fit = min(target_fit, len(rows) - 1)

    boundary_ts = str(rows[target_fit - 1].get("starts_at"))
    split_idx = target_fit
    while split_idx < len(rows) and str(rows[split_idx].get("starts_at")) == boundary_ts:
        split_idx += 1
    if split_idx >= len(rows):
        split_idx = target_fit - 1
        boundary_ts = str(rows[split_idx].get("starts_at"))
        while split_idx > 0 and str(rows[split_idx - 1].get("starts_at")) == boundary_ts:
            split_idx -= 1

    fit_rows = rows[:split_idx]
    calibration_rows = rows[split_idx:]
    if not fit_rows or not calibration_rows:
        raise ValueError("unable to create non-empty temporal fit/calibration partitions")

    fit_max = str(fit_rows[-1].get("starts_at"))
    calibration_min = str(calibration_rows[0].get("starts_at"))
    if not fit_max < calibration_min:
        raise ValueError("inner temporal calibration split is not strictly ordered")

    return fit_rows, calibration_rows, {
        "requested_calibration_ratio": calibration_ratio,
        "actual_fit_ratio": round(len(fit_rows) / len(rows), 6),
        "actual_calibration_ratio": round(len(calibration_rows) / len(rows), 6),
        "fit_rows": len(fit_rows),
        "calibration_rows": len(calibration_rows),
        "fit_max_starts_at": fit_max,
        "calibration_min_starts_at": calibration_min,
        "strict_temporal_order": True,
        "same_timestamp_never_crosses_inner_split": True,
    }


def _temperature_scale(probabilities: np.ndarray, temperature: float) -> np.ndarray:
    eps = 1e-12
    logits = np.log(np.clip(probabilities, eps, 1.0))
    scaled = logits / float(temperature)
    scaled -= np.max(scaled, axis=1, keepdims=True)
    exps = np.exp(scaled)
    return exps / np.sum(exps, axis=1, keepdims=True)


def _fit_temperature(probabilities: np.ndarray, y_true: np.ndarray) -> tuple[float, float]:
    def objective(log_t: float) -> float:
        temperature = exp(float(log_t))
        calibrated = _temperature_scale(probabilities, temperature)
        return float(log_loss(y_true, calibrated, labels=CLASS_ORDER))

    result = minimize_scalar(objective, bounds=(-2.302585093, 2.302585093), method="bounded", options={"xatol": 1e-5})
    temperature = exp(float(result.x))
    return temperature, float(result.fun)


def _favorite_bin_index(p: float) -> int:
    if p < 0.40:
        return 0
    if p >= 0.80:
        return 9
    return 1 + int((p - 0.40) // 0.05)


def _favorite_calibration_summary(y_true: np.ndarray, probabilities: np.ndarray) -> dict:
    labels = ["<40%", "40-44.99%", "45-49.99%", "50-54.99%", "55-59.99%", "60-64.99%", "65-69.99%", "70-74.99%", "75-79.99%", "80%+"]
    ranges = [
        (0.0, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60),
        (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, None),
    ]
    stats = [{"rows": 0, "wins": 0, "prob_sum": 0.0} for _ in labels]

    overall_rows = len(y_true)
    overall_wins = 0
    overall_prob_sum = 0.0

    for actual, probs in zip(y_true, probabilities):
        p_home, _, p_away = [float(v) for v in probs]
        favorite = "1" if p_home >= p_away else "2"
        favorite_probability = p_home if favorite == "1" else p_away
        win = str(actual) == favorite
        idx = _favorite_bin_index(favorite_probability)
        stats[idx]["rows"] += 1
        stats[idx]["wins"] += int(win)
        stats[idx]["prob_sum"] += favorite_probability
        overall_wins += int(win)
        overall_prob_sum += favorite_probability

    bins = []
    ece = 0.0
    mce = 0.0
    for label, bounds, s in zip(labels, ranges, stats):
        n = s["rows"]
        if n:
            avg_p = s["prob_sum"] / n
            realized = s["wins"] / n
            gap = realized - avg_p
            abs_gap = abs(gap)
            ece += (n / overall_rows) * abs_gap
            mce = max(mce, abs_gap)
        else:
            avg_p = realized = gap = abs_gap = None
        bins.append({
            "bin": label,
            "range": {"lower_inclusive": bounds[0], "upper_exclusive": bounds[1]},
            "rows": n,
            "share_pct": round(n / overall_rows * 100, 2) if overall_rows else 0.0,
            "favorite_wins": s["wins"],
            "avg_predicted_favorite_probability": round(avg_p, 6) if avg_p is not None else None,
            "realized_favorite_win_rate": round(realized, 6) if realized is not None else None,
            "calibration_gap_realized_minus_predicted": round(gap, 6) if gap is not None else None,
            "absolute_calibration_gap": round(abs_gap, 6) if abs_gap is not None else None,
            "sample_warning": "LOW_SAMPLE" if 0 < n < 10 else None,
        })

    avg_overall = overall_prob_sum / overall_rows if overall_rows else None
    realized_overall = overall_wins / overall_rows if overall_rows else None
    return {
        "rows": overall_rows,
        "favorite_wins": overall_wins,
        "avg_predicted_favorite_probability": round(avg_overall, 6) if avg_overall is not None else None,
        "realized_favorite_win_rate": round(realized_overall, 6) if realized_overall is not None else None,
        "overall_calibration_gap_realized_minus_predicted": round(realized_overall - avg_overall, 6) if avg_overall is not None else None,
        "expected_calibration_error": round(ece, 6),
        "maximum_calibration_error": round(mce, 6),
        "bins": bins,
    }


def build_probability_calibration_v1(
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
    calibration_ratio: float = 0.20,
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
    full_train_rows = family_payload["partitions"]["train"].get("rows") or []
    validation_rows = family_payload["partitions"]["validation"].get("rows") or []
    test_rows = family_payload["partitions"]["test"].get("rows") or []
    if not full_train_rows or not validation_rows or not test_rows:
        raise ValueError("probability calibration requires non-empty train, validation, and test partitions")

    fit_rows, calibration_rows, inner_split = _chronological_inner_split(full_train_rows, calibration_ratio)
    X_fit, y_fit = _matrix(fit_rows, feature_names)
    X_calibration, y_calibration = _matrix(calibration_rows, feature_names)
    X_validation, y_validation = _matrix(validation_rows, feature_names)

    if set(y_fit.tolist()) != set(CLASS_ORDER):
        raise ValueError("inner fit partition must contain all 1X2 classes")

    pipeline = Pipeline(steps=[
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("classifier", LogisticRegression(C=1.0, penalty="l2", solver="lbfgs", max_iter=2000, class_weight="balanced" if class_weight_balanced else None, random_state=42)),
    ])
    pipeline.fit(X_fit, y_fit)

    calibration_raw = _ordered_probabilities(pipeline, X_calibration)
    validation_raw = _ordered_probabilities(pipeline, X_validation)
    temperature, calibration_nll = _fit_temperature(calibration_raw, y_calibration)
    calibration_calibrated = _temperature_scale(calibration_raw, temperature)
    validation_calibrated = _temperature_scale(validation_raw, temperature)

    raw_validation_log_loss = float(log_loss(y_validation, validation_raw, labels=CLASS_ORDER))
    calibrated_validation_log_loss = float(log_loss(y_validation, validation_calibrated, labels=CLASS_ORDER))
    raw_summary = _favorite_calibration_summary(y_validation, validation_raw)
    calibrated_summary = _favorite_calibration_summary(y_validation, validation_calibrated)

    metadata = {
        "version": PROBABILITY_CALIBRATION_VERSION,
        "model_dataset_sha256": model_dataset["model_dataset_sha256"],
        "family_sha256": family_payload["family_sha256"],
        "family": family,
        "calibration_method": "temperature_scaling",
        "temperature": round(temperature, 10),
        "calibration_ratio": calibration_ratio,
        "class_weight_balanced": class_weight_balanced,
    }
    calibration_sha = _stable_hash(metadata)

    return {
        "status": "ok",
        "version": PROBABILITY_CALIBRATION_VERSION,
        "probability_calibration_id": f"{PROBABILITY_CALIBRATION_VERSION}:{calibration_sha[:16]}",
        "probability_calibration_sha256": calibration_sha,
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
            "original_train_rows": len(full_train_rows),
            "validation_rows": len(validation_rows),
            "test_rows_withheld": len(test_rows),
            "class_weight": "balanced" if class_weight_balanced else None,
            "base_model_fit_scope": "early chronological segment of train only",
        },
        "inner_temporal_split": inner_split,
        "calibrator": {
            "method": "temperature_scaling",
            "temperature": round(temperature, 6),
            "interpretation": "temperature > 1 softens overconfident probabilities; temperature < 1 sharpens underconfident probabilities",
            "fit_scope": "late chronological segment of original train only",
            "calibration_segment_log_loss_after_fit": round(calibration_nll, 6),
            "probabilities_preserve_sum_to_one": True,
        },
        "validation": {
            "raw": {
                "multiclass_log_loss": round(raw_validation_log_loss, 6),
                "favorite_calibration": raw_summary,
            },
            "calibrated": {
                "multiclass_log_loss": round(calibrated_validation_log_loss, 6),
                "favorite_calibration": calibrated_summary,
            },
            "delta": {
                "log_loss_calibrated_minus_raw": round(calibrated_validation_log_loss - raw_validation_log_loss, 6),
                "ece_calibrated_minus_raw": round(calibrated_summary["expected_calibration_error"] - raw_summary["expected_calibration_error"], 6),
                "mce_calibrated_minus_raw": round(calibrated_summary["maximum_calibration_error"] - raw_summary["maximum_calibration_error"], 6),
                "improved_log_loss": calibrated_validation_log_loss < raw_validation_log_loss,
                "improved_ece": calibrated_summary["expected_calibration_error"] < raw_summary["expected_calibration_error"],
                "improved_mce": calibrated_summary["maximum_calibration_error"] < raw_summary["maximum_calibration_error"],
            },
        },
        "test": {
            "withheld": True,
            "rows": len(test_rows),
            "reason": "test outcomes remain unevaluated while probability calibration and confidence policy are being developed",
        },
        "policy": {
            "outer_temporal_split_preserved": True,
            "inner_temporal_split_preserved": True,
            "shuffle": False,
            "imputer_fit_on_inner_fit_only": True,
            "scaler_fit_on_inner_fit_only": True,
            "classifier_fit_on_inner_fit_only": True,
            "temperature_fit_on_inner_calibration_only": True,
            "validation_used_for_calibrator_fit": False,
            "test_used_for_fit": False,
            "test_used_for_calibrator_fit": False,
            "test_used_for_threshold_selection": False,
            "target_match_postgame_data_as_features": False,
            "deterministic": True,
        },
    }
