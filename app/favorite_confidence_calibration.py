from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.model_dataset import build_model_dataset_v1

FAVORITE_CONFIDENCE_CALIBRATION_VERSION = "favorite_confidence_calibration_v1"
CLASS_ORDER = ["1", "X", "2"]
BIN_DEFINITIONS = [
    (0.00, 0.40, "<40%"),
    (0.40, 0.45, "40-44.99%"),
    (0.45, 0.50, "45-49.99%"),
    (0.50, 0.55, "50-54.99%"),
    (0.55, 0.60, "55-59.99%"),
    (0.60, 0.65, "60-64.99%"),
    (0.65, 0.70, "65-69.99%"),
    (0.70, 0.75, "70-74.99%"),
    (0.75, 0.80, "75-79.99%"),
    (0.80, 1.0000001, "80%+"),
]


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _matrix(rows: list[dict], feature_names: list[str]) -> tuple[np.ndarray, np.ndarray]:
    X = np.asarray([[row["X"].get(name) for name in feature_names] for row in rows], dtype=float)
    y = np.asarray([str((row.get("y") or {}).get("outcome_1x2")) for row in rows], dtype=object)
    return X, y


def _build_base_model(class_weight_balanced: bool) -> Pipeline:
    return Pipeline(
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


def _ordered_probabilities(model: Pipeline, X: np.ndarray) -> np.ndarray:
    raw = model.predict_proba(X)
    classes = list(model.named_steps["classifier"].classes_)
    return np.column_stack([raw[:, classes.index(label)] for label in CLASS_ORDER])


def _favorite_observations(y_true: np.ndarray, probabilities: np.ndarray) -> list[dict]:
    observations: list[dict] = []
    for actual, probs in zip(y_true, probabilities):
        p_home, _, p_away = [float(v) for v in probs]
        favorite = "1" if p_home >= p_away else "2"
        favorite_probability = p_home if favorite == "1" else p_away
        observations.append(
            {
                "favorite": favorite,
                "favorite_probability": favorite_probability,
                "favorite_won": int(str(actual) == favorite),
            }
        )
    return observations


def _logit(probability: float) -> float:
    eps = 1e-6
    p = min(max(float(probability), eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def _fit_platt(observations: list[dict]) -> LogisticRegression:
    if len(observations) < 30:
        raise ValueError("favorite confidence calibration requires at least 30 OOF favorite observations")
    y = np.asarray([int(item["favorite_won"]) for item in observations], dtype=int)
    if len(set(y.tolist())) < 2:
        raise ValueError("OOF favorite observations must contain both wins and non-wins")
    X = np.asarray([[_logit(float(item["favorite_probability"]))] for item in observations], dtype=float)
    calibrator = LogisticRegression(C=1e6, penalty="l2", solver="lbfgs", max_iter=2000, random_state=42)
    calibrator.fit(X, y)
    return calibrator


def _apply_platt(calibrator: LogisticRegression, probabilities: list[float]) -> list[float]:
    X = np.asarray([[_logit(p)] for p in probabilities], dtype=float)
    return [float(v) for v in calibrator.predict_proba(X)[:, 1]]


def _group_rows_by_timestamp(rows: list[dict]) -> list[list[dict]]:
    ordered = sorted(rows, key=lambda r: (str(r.get("starts_at")), int(r.get("fixture_id") or 0)))
    groups: list[list[dict]] = []
    current_ts: str | None = None
    current: list[dict] = []
    for row in ordered:
        ts = str(row.get("starts_at"))
        if current_ts is None or ts == current_ts:
            current.append(row)
            current_ts = ts
        else:
            groups.append(current)
            current = [row]
            current_ts = ts
    if current:
        groups.append(current)
    return groups


def _rolling_origin_oof(
    rows: list[dict],
    feature_names: list[str],
    class_weight_balanced: bool,
    oof_folds: int,
    min_initial_train_rows: int,
) -> tuple[list[dict], list[dict]]:
    if not 3 <= oof_folds <= 10:
        raise ValueError("oof_folds must be between 3 and 10")
    if min_initial_train_rows < 60:
        raise ValueError("min_initial_train_rows must be at least 60")
    if len(rows) <= min_initial_train_rows:
        raise ValueError("train partition is too small for rolling-origin OOF calibration")

    groups = _group_rows_by_timestamp(rows)
    cumulative = 0
    initial_group_idx: int | None = None
    for idx, group in enumerate(groups):
        cumulative += len(group)
        if cumulative >= min_initial_train_rows:
            initial_group_idx = idx + 1
            break
    if initial_group_idx is None or initial_group_idx >= len(groups):
        raise ValueError("unable to create an initial rolling-origin training window")

    future_groups = groups[initial_group_idx:]
    fold_group_count = max(1, math.ceil(len(future_groups) / oof_folds))
    observations: list[dict] = []
    folds: list[dict] = []
    train_groups = groups[:initial_group_idx]
    fold_number = 0

    for start in range(0, len(future_groups), fold_group_count):
        prediction_groups = future_groups[start : start + fold_group_count]
        if not prediction_groups:
            continue
        fit_rows = [row for group in train_groups for row in group]
        prediction_rows = [row for group in prediction_groups for row in group]
        if not fit_rows or not prediction_rows:
            continue

        X_fit, y_fit = _matrix(fit_rows, feature_names)
        if set(y_fit.tolist()) != set(CLASS_ORDER):
            train_groups.extend(prediction_groups)
            continue

        X_pred, y_pred = _matrix(prediction_rows, feature_names)
        model = _build_base_model(class_weight_balanced)
        model.fit(X_fit, y_fit)
        probabilities = _ordered_probabilities(model, X_pred)
        observations.extend(_favorite_observations(y_pred, probabilities))

        fold_number += 1
        folds.append(
            {
                "fold": fold_number,
                "fit_rows": len(fit_rows),
                "prediction_rows": len(prediction_rows),
                "fit_max_starts_at": str(fit_rows[-1].get("starts_at")),
                "prediction_min_starts_at": str(prediction_rows[0].get("starts_at")),
                "prediction_max_starts_at": str(prediction_rows[-1].get("starts_at")),
                "strict_temporal_order": str(fit_rows[-1].get("starts_at")) < str(prediction_rows[0].get("starts_at")),
            }
        )
        train_groups.extend(prediction_groups)

    if len(observations) < 30:
        raise ValueError("rolling-origin OOF produced fewer than 30 calibration observations")
    if len(folds) < 2:
        raise ValueError("rolling-origin OOF requires at least two successful prediction folds")
    return observations, folds


def _bin_for_probability(probability: float) -> tuple[float, float, str]:
    for lower, upper, label in BIN_DEFINITIONS:
        if lower <= probability < upper:
            return lower, upper, label
    return BIN_DEFINITIONS[-1]


def _calibration_summary(outcomes: list[int], probabilities: list[float]) -> dict:
    if len(outcomes) != len(probabilities):
        raise ValueError("outcomes and probabilities must have equal length")
    total = len(outcomes)
    if total == 0:
        raise ValueError("calibration summary requires at least one observation")

    buckets = {label: {"rows": 0, "wins": 0, "probability_sum": 0.0} for _, _, label in BIN_DEFINITIONS}
    for outcome, probability in zip(outcomes, probabilities):
        _, _, label = _bin_for_probability(float(probability))
        bucket = buckets[label]
        bucket["rows"] += 1
        bucket["wins"] += int(outcome)
        bucket["probability_sum"] += float(probability)

    bins: list[dict] = []
    ece = 0.0
    mce = 0.0
    previous_realized: float | None = None
    monotonic_violations = 0

    for lower, upper, label in BIN_DEFINITIONS:
        bucket = buckets[label]
        n = bucket["rows"]
        if n:
            avg_pred = bucket["probability_sum"] / n
            realized = bucket["wins"] / n
            gap = realized - avg_pred
            abs_gap = abs(gap)
            ece += (n / total) * abs_gap
            mce = max(mce, abs_gap)
            if previous_realized is not None and realized + 1e-12 < previous_realized:
                monotonic_violations += 1
            previous_realized = realized
        else:
            avg_pred = realized = gap = abs_gap = None

        bins.append(
            {
                "bin": label,
                "range": {
                    "lower_inclusive": round(lower, 2),
                    "upper_exclusive": None if upper > 1 else round(upper, 2),
                },
                "rows": n,
                "share_pct": round(n / total * 100, 2),
                "favorite_wins": bucket["wins"],
                "avg_predicted_favorite_probability": round(avg_pred, 6) if avg_pred is not None else None,
                "realized_favorite_win_rate": round(realized, 6) if realized is not None else None,
                "calibration_gap_realized_minus_predicted": round(gap, 6) if gap is not None else None,
                "absolute_calibration_gap": round(abs_gap, 6) if abs_gap is not None else None,
                "sample_warning": "LOW_SAMPLE" if 0 < n < 10 else None,
            }
        )

    avg_probability = float(np.mean(probabilities))
    realized_rate = float(np.mean(outcomes))
    return {
        "rows": total,
        "favorite_wins": int(sum(outcomes)),
        "avg_predicted_favorite_probability": round(avg_probability, 6),
        "realized_favorite_win_rate": round(realized_rate, 6),
        "overall_calibration_gap_realized_minus_predicted": round(realized_rate - avg_probability, 6),
        "brier_score": round(float(brier_score_loss(outcomes, probabilities)), 6),
        "expected_calibration_error": round(ece, 6),
        "maximum_calibration_error": round(mce, 6),
        "monotonicity_violations_across_non_empty_bins": monotonic_violations,
        "bins": bins,
    }


def _delta(raw_summary: dict, calibrated_summary: dict) -> dict:
    return {
        "brier_calibrated_minus_raw": round(calibrated_summary["brier_score"] - raw_summary["brier_score"], 6),
        "ece_calibrated_minus_raw": round(calibrated_summary["expected_calibration_error"] - raw_summary["expected_calibration_error"], 6),
        "mce_calibrated_minus_raw": round(calibrated_summary["maximum_calibration_error"] - raw_summary["maximum_calibration_error"], 6),
        "monotonicity_violations_calibrated_minus_raw": calibrated_summary["monotonicity_violations_across_non_empty_bins"] - raw_summary["monotonicity_violations_across_non_empty_bins"],
        "improved_brier": calibrated_summary["brier_score"] < raw_summary["brier_score"],
        "improved_ece": calibrated_summary["expected_calibration_error"] < raw_summary["expected_calibration_error"],
        "improved_mce": calibrated_summary["maximum_calibration_error"] < raw_summary["maximum_calibration_error"],
    }


def _evaluate_partition(model: Pipeline, calibrator: LogisticRegression, rows: list[dict], feature_names: list[str]) -> dict:
    X, y = _matrix(rows, feature_names)
    probabilities = _ordered_probabilities(model, X)
    observations = _favorite_observations(y, probabilities)
    raw_probabilities = [float(item["favorite_probability"]) for item in observations]
    outcomes = [int(item["favorite_won"]) for item in observations]
    calibrated_probabilities = _apply_platt(calibrator, raw_probabilities)
    raw_summary = _calibration_summary(outcomes, raw_probabilities)
    calibrated_summary = _calibration_summary(outcomes, calibrated_probabilities)
    return {
        "raw": raw_summary,
        "calibrated": calibrated_summary,
        "delta": _delta(raw_summary, calibrated_summary),
    }


def build_favorite_confidence_calibration_v1(
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
    oof_folds: int = 5,
    min_initial_train_rows: int = 120,
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
        raise ValueError("favorite confidence calibration requires non-empty train, validation, and test partitions")

    oof_observations, folds = _rolling_origin_oof(
        rows=train_rows,
        feature_names=feature_names,
        class_weight_balanced=class_weight_balanced,
        oof_folds=oof_folds,
        min_initial_train_rows=min_initial_train_rows,
    )
    calibrator = _fit_platt(oof_observations)

    X_train, y_train = _matrix(train_rows, feature_names)
    if set(y_train.tolist()) != set(CLASS_ORDER):
        raise ValueError("train partition must contain all 1X2 classes")

    final_model = _build_base_model(class_weight_balanced)
    final_model.fit(X_train, y_train)

    validation_evaluation = _evaluate_partition(final_model, calibrator, validation_rows, feature_names)
    test_evaluation = _evaluate_partition(final_model, calibrator, test_rows, feature_names)

    coef = float(calibrator.coef_[0][0])
    intercept = float(calibrator.intercept_[0])
    metadata = {
        "version": FAVORITE_CONFIDENCE_CALIBRATION_VERSION,
        "model_dataset_sha256": model_dataset["model_dataset_sha256"],
        "family_sha256": family_payload["family_sha256"],
        "family": family,
        "calibration_method": "platt_scaling_binary_on_logit_raw_favorite_probability",
        "class_weight_balanced": class_weight_balanced,
        "oof_folds_requested": oof_folds,
        "min_initial_train_rows": min_initial_train_rows,
        "platt_coef": round(coef, 10),
        "platt_intercept": round(intercept, 10),
    }
    calibration_sha = _stable_hash(metadata)

    validation_pass = bool(
        validation_evaluation["delta"]["improved_brier"]
        and validation_evaluation["delta"]["improved_ece"]
    )
    test_pass = bool(
        test_evaluation["delta"]["improved_brier"]
        and test_evaluation["delta"]["improved_ece"]
    )

    return {
        "status": "ok",
        "version": FAVORITE_CONFIDENCE_CALIBRATION_VERSION,
        "favorite_confidence_calibration_id": f"{FAVORITE_CONFIDENCE_CALIBRATION_VERSION}:{calibration_sha[:16]}",
        "favorite_confidence_calibration_sha256": calibration_sha,
        "family": family,
        "parent": {
            "model_dataset_id": model_dataset["model_dataset_id"],
            "model_dataset_sha256": model_dataset["model_dataset_sha256"],
            "family_sha256": family_payload["family_sha256"],
            "split_sha256": model_dataset["parent"]["split_sha256"],
            "dataset_sha256": model_dataset["parent"]["dataset_sha256"],
        },
        "training": {
            "base_algorithm": "multinomial_logistic_regression",
            "feature_count": len(feature_names),
            "train_rows": len(train_rows),
            "validation_rows": len(validation_rows),
            "test_rows": len(test_rows),
            "class_weight": "balanced" if class_weight_balanced else None,
            "final_base_model_fit_scope": "full outer train partition only",
        },
        "rolling_origin_oof": {
            "requested_folds": oof_folds,
            "successful_folds": len(folds),
            "min_initial_train_rows": min_initial_train_rows,
            "oof_observations": len(oof_observations),
            "folds": folds,
            "shuffle": False,
            "same_timestamp_never_crosses_a_fold_boundary": True,
        },
        "calibrator": {
            "method": "platt_scaling_binary",
            "input": "logit(raw favorite probability)",
            "target": "1 if selected favorite (home or away) won; 0 otherwise",
            "draw_is_never_defined_as_favorite": True,
            "coefficient": round(coef, 6),
            "intercept": round(intercept, 6),
            "fit_scope": "rolling-origin OOF predictions from outer train only",
            "frozen_before_test_evaluation": True,
        },
        "validation": validation_evaluation,
        "test": {
            "withheld": False,
            "opened_for_final_gate": True,
            **test_evaluation,
        },
        "final_gate": {
            "primary_rule": "Brier and ECE must both improve out-of-sample; MCE is diagnostic because sparse bins can dominate it",
            "validation_passed_primary_rule": validation_pass,
            "test_passed_primary_rule": test_pass,
            "promote_candidate": bool(validation_pass and test_pass),
            "no_recalibration_after_test": True,
        },
        "policy": {
            "outer_temporal_split_preserved": True,
            "rolling_origin_temporal_order_preserved": True,
            "shuffle": False,
            "calibrator_fit_uses_validation": False,
            "test_used_for_fit": False,
            "test_used_for_calibrator_fit": False,
            "test_used_for_threshold_selection": False,
            "test_used_only_for_final_out_of_sample_evaluation": True,
            "target_match_postgame_data_as_features": False,
            "raw_1x2_probabilities_are_not_rewritten": True,
            "favorite_confidence_is_a_separate_binary_calibration_layer": True,
            "deterministic": True,
        },
    }
