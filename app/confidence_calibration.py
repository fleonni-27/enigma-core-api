from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.model_dataset import build_model_dataset_v1

CONFIDENCE_CALIBRATION_VERSION = "confidence_calibration_v1"
CLASS_ORDER = ["1", "X", "2"]

# Favorite means the more probable team outcome between home (1) and away (2).
# Draw is never treated as the favorite for this diagnostic.
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


def _ordered_probabilities(model: Pipeline, X: np.ndarray) -> np.ndarray:
    raw = model.predict_proba(X)
    classes = list(model.named_steps["classifier"].classes_)
    return np.column_stack([raw[:, classes.index(label)] for label in CLASS_ORDER])


def _wilson_interval(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * n * n))) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


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
                "favorite_won": str(actual) == favorite,
            }
        )
    return observations


def _bin_for_probability(probability: float) -> tuple[float, float, str]:
    for lower, upper, label in BIN_DEFINITIONS:
        if lower <= probability < upper:
            return lower, upper, label
    return BIN_DEFINITIONS[-1]


def _calibration_report(y_true: np.ndarray, probabilities: np.ndarray) -> dict:
    observations = _favorite_observations(y_true, probabilities)
    buckets: dict[str, dict] = {
        label: {
            "lower": lower,
            "upper": upper,
            "rows": 0,
            "wins": 0,
            "probability_sum": 0.0,
            "home_favorites": 0,
            "away_favorites": 0,
        }
        for lower, upper, label in BIN_DEFINITIONS
    }

    for item in observations:
        _, _, label = _bin_for_probability(item["favorite_probability"])
        bucket = buckets[label]
        bucket["rows"] += 1
        bucket["wins"] += int(item["favorite_won"])
        bucket["probability_sum"] += item["favorite_probability"]
        if item["favorite"] == "1":
            bucket["home_favorites"] += 1
        else:
            bucket["away_favorites"] += 1

    bins: list[dict] = []
    weighted_abs_gap = 0.0
    max_abs_gap = 0.0
    non_empty_bins = 0

    for lower, upper, label in BIN_DEFINITIONS:
        bucket = buckets[label]
        n = bucket["rows"]
        if n:
            non_empty_bins += 1
            avg_pred = bucket["probability_sum"] / n
            realized = bucket["wins"] / n
            gap = realized - avg_pred
            abs_gap = abs(gap)
            weighted_abs_gap += abs_gap * n
            max_abs_gap = max(max_abs_gap, abs_gap)
            ci_low, ci_high = _wilson_interval(bucket["wins"], n)
        else:
            avg_pred = realized = gap = abs_gap = ci_low = ci_high = None

        bins.append(
            {
                "bin": label,
                "range": {
                    "lower_inclusive": round(lower, 2),
                    "upper_exclusive": None if upper > 1 else round(upper, 2),
                },
                "rows": n,
                "share_pct": round(n / len(observations) * 100, 2) if observations else 0.0,
                "favorite_wins": bucket["wins"],
                "avg_predicted_favorite_probability": round(avg_pred, 6) if avg_pred is not None else None,
                "realized_favorite_win_rate": round(realized, 6) if realized is not None else None,
                "calibration_gap_realized_minus_predicted": round(gap, 6) if gap is not None else None,
                "absolute_calibration_gap": round(abs_gap, 6) if abs_gap is not None else None,
                "realized_win_rate_ci95_wilson": {
                    "low": round(ci_low, 6) if ci_low is not None else None,
                    "high": round(ci_high, 6) if ci_high is not None else None,
                },
                "favorite_side": {
                    "home": bucket["home_favorites"],
                    "away": bucket["away_favorites"],
                },
                "sample_warning": "LOW_SAMPLE" if 0 < n < 10 else None,
            }
        )

    total = len(observations)
    favorite_wins = sum(int(item["favorite_won"]) for item in observations)
    avg_probability = sum(item["favorite_probability"] for item in observations) / total if total else None
    realized_rate = favorite_wins / total if total else None
    overall_gap = realized_rate - avg_probability if total else None
    ci_low, ci_high = _wilson_interval(favorite_wins, total)

    return {
        "rows": total,
        "favorite_wins": favorite_wins,
        "avg_predicted_favorite_probability": round(avg_probability, 6) if avg_probability is not None else None,
        "realized_favorite_win_rate": round(realized_rate, 6) if realized_rate is not None else None,
        "overall_calibration_gap_realized_minus_predicted": round(overall_gap, 6) if overall_gap is not None else None,
        "realized_win_rate_ci95_wilson": {
            "low": round(ci_low, 6) if ci_low is not None else None,
            "high": round(ci_high, 6) if ci_high is not None else None,
        },
        "expected_calibration_error": round(weighted_abs_gap / total, 6) if total else None,
        "maximum_calibration_error": round(max_abs_gap, 6) if non_empty_bins else None,
        "non_empty_bins": non_empty_bins,
        "bins": bins,
    }


def build_confidence_calibration_v1(
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
        raise ValueError("confidence calibration requires non-empty train, validation, and test partitions")

    X_train, y_train = _matrix(train_rows, feature_names)
    X_validation, y_validation = _matrix(validation_rows, feature_names)
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
    validation_probabilities = _ordered_probabilities(pipeline, X_validation)
    validation_report = _calibration_report(y_validation, validation_probabilities)

    metadata = {
        "version": CONFIDENCE_CALIBRATION_VERSION,
        "model_dataset_sha256": model_dataset["model_dataset_sha256"],
        "family_sha256": family_payload["family_sha256"],
        "family": family,
        "class_weight_balanced": class_weight_balanced,
        "bin_edges": [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80],
        "fit_scope": "train_only",
        "calibration_scope": "validation_only",
        "test_scope": "withheld",
    }
    calibration_sha = _stable_hash(metadata)

    return {
        "status": "ok",
        "version": CONFIDENCE_CALIBRATION_VERSION,
        "calibration_id": f"{CONFIDENCE_CALIBRATION_VERSION}:{calibration_sha[:16]}",
        "calibration_sha256": calibration_sha,
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
            "test_rows_withheld": len(test_rows),
            "class_weight": "balanced" if class_weight_balanced else None,
            "fit_scope": "train partition only",
        },
        "calibration_definition": {
            "event": "the model's more probable team outcome (1 or 2) wins the match",
            "draw_is_never_defined_as_favorite": True,
            "bin_width_pct": 5,
            "special_bins": ["<40%", "80%+"],
            "gap_definition": "realized_favorite_win_rate - avg_predicted_favorite_probability",
            "negative_gap_means": "model overconfidence",
            "positive_gap_means": "model underconfidence",
            "confidence_interval": "95% Wilson score interval on realized favorite win rate",
        },
        "validation": validation_report,
        "test": {
            "withheld": True,
            "rows": len(test_rows),
            "reason": "test outcomes are intentionally not evaluated while confidence thresholds are being developed",
        },
        "policy": {
            "temporal_split_preserved": True,
            "shuffle": False,
            "model_fit_on_train_only": True,
            "calibration_diagnostics_use_validation_only": True,
            "test_used_for_fit": False,
            "test_used_for_threshold_selection": False,
            "probabilities_are_not_clamped_or_rewritten": True,
            "calibration_is_diagnostic_only": True,
            "target_match_postgame_data_as_features": False,
            "deterministic": True,
        },
    }
