from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from app.training_dataset_split import build_temporal_training_split

MODEL_DATASET_VERSION = "model_dataset_v1"

STANDARD_FEATURES = [
    "home_history_matches",
    "home_points_per_match",
    "home_win_rate",
    "home_draw_rate",
    "home_goals_for_avg",
    "home_goals_against_avg",
    "home_shots_total_for_avg",
    "home_shots_on_target_for_avg",
    "home_possession_avg",
    "home_corners_for_avg",
    "home_successful_passes_for_avg",
    "home_rest_days",
    "home_history_completeness_ratio",
    "away_history_matches",
    "away_points_per_match",
    "away_win_rate",
    "away_draw_rate",
    "away_goals_for_avg",
    "away_goals_against_avg",
    "away_shots_total_for_avg",
    "away_shots_on_target_for_avg",
    "away_possession_avg",
    "away_corners_for_avg",
    "away_successful_passes_for_avg",
    "away_rest_days",
    "away_history_completeness_ratio",
    "delta_points_per_match",
    "delta_goals_for_avg",
    "delta_goals_against_avg",
    "delta_shots_total_for_avg",
    "delta_shots_on_target_for_avg",
    "delta_possession_avg",
    "delta_corners_for_avg",
    "delta_successful_passes_for_avg",
    "delta_rest_days",
    "delta_history_completeness_ratio",
]

FULL_XG_EXTRA_FEATURES = [
    "home_xg_for_avg",
    "home_xg_history_matches",
    "away_xg_for_avg",
    "away_xg_history_matches",
    "delta_xg_for_avg",
]

LABELS = [
    "outcome_1x2",
    "home_goals",
    "away_goals",
    "total_goals",
    "btts",
    "over_2_5",
]


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _flatten_standard(row: dict) -> dict:
    f = row["features"]
    h = f["home"]
    a = f["away"]
    d = f["delta"]
    return {
        "home_history_matches": h.get("history_matches"),
        "home_points_per_match": h.get("points_per_match"),
        "home_win_rate": h.get("win_rate"),
        "home_draw_rate": h.get("draw_rate"),
        "home_goals_for_avg": h.get("goals_for_avg"),
        "home_goals_against_avg": h.get("goals_against_avg"),
        "home_shots_total_for_avg": h.get("shots_total_for_avg"),
        "home_shots_on_target_for_avg": h.get("shots_on_target_for_avg"),
        "home_possession_avg": h.get("possession_avg"),
        "home_corners_for_avg": h.get("corners_for_avg"),
        "home_successful_passes_for_avg": h.get("successful_passes_for_avg"),
        "home_rest_days": h.get("rest_days"),
        "home_history_completeness_ratio": h.get("history_completeness_ratio"),
        "away_history_matches": a.get("history_matches"),
        "away_points_per_match": a.get("points_per_match"),
        "away_win_rate": a.get("win_rate"),
        "away_draw_rate": a.get("draw_rate"),
        "away_goals_for_avg": a.get("goals_for_avg"),
        "away_goals_against_avg": a.get("goals_against_avg"),
        "away_shots_total_for_avg": a.get("shots_total_for_avg"),
        "away_shots_on_target_for_avg": a.get("shots_on_target_for_avg"),
        "away_possession_avg": a.get("possession_avg"),
        "away_corners_for_avg": a.get("corners_for_avg"),
        "away_successful_passes_for_avg": a.get("successful_passes_for_avg"),
        "away_rest_days": a.get("rest_days"),
        "away_history_completeness_ratio": a.get("history_completeness_ratio"),
        "delta_points_per_match": d.get("points_per_match"),
        "delta_goals_for_avg": d.get("goals_for_avg"),
        "delta_goals_against_avg": d.get("goals_against_avg"),
        "delta_shots_total_for_avg": d.get("shots_total_for_avg"),
        "delta_shots_on_target_for_avg": d.get("shots_on_target_for_avg"),
        "delta_possession_avg": d.get("possession_avg"),
        "delta_corners_for_avg": d.get("corners_for_avg"),
        "delta_successful_passes_for_avg": d.get("successful_passes_for_avg"),
        "delta_rest_days": d.get("rest_days"),
        "delta_history_completeness_ratio": d.get("history_completeness_ratio"),
    }


def _flatten_full_xg(row: dict) -> dict | None:
    f = row["features"]
    h = f["home"]
    a = f["away"]
    d = f["delta"]
    if h.get("xg_for_avg") is None or a.get("xg_for_avg") is None or d.get("xg_for_avg") is None:
        return None
    values = _flatten_standard(row)
    values.update(
        {
            "home_xg_for_avg": h.get("xg_for_avg"),
            "home_xg_history_matches": h.get("xg_history_matches"),
            "away_xg_for_avg": a.get("xg_for_avg"),
            "away_xg_history_matches": a.get("xg_history_matches"),
            "delta_xg_for_avg": d.get("xg_for_avg"),
        }
    )
    return values


def _label_payload(row: dict) -> dict:
    label = row.get("label") or {}
    return {key: label.get(key) for key in LABELS}


def _model_row(row: dict, features: dict) -> dict:
    return {
        "fixture_id": row.get("fixture_id"),
        "sportmonks_fixture_id": row.get("sportmonks_fixture_id"),
        "starts_at": row.get("starts_at"),
        "league": row.get("league"),
        "home_team": row.get("home_team"),
        "away_team": row.get("away_team"),
        "source_profile": row.get("source_profile"),
        "X": features,
        "y": _label_payload(row),
    }


def _partition_stats(rows: list[dict]) -> dict:
    outcomes = {"1": 0, "X": 0, "2": 0}
    leagues: dict[str, int] = {}
    profiles: dict[str, int] = {}
    for row in rows:
        outcome = str((row.get("y") or {}).get("outcome_1x2"))
        if outcome in outcomes:
            outcomes[outcome] += 1
        league = str(row.get("league"))
        leagues[league] = leagues.get(league, 0) + 1
        profile = str(row.get("source_profile"))
        profiles[profile] = profiles.get(profile, 0) + 1
    return {
        "rows": len(rows),
        "rows_by_league": dict(sorted(leagues.items())),
        "source_profiles": dict(sorted(profiles.items())),
        "outcome_1x2": outcomes,
    }


def _build_family(
    family_name: str,
    split_partitions: dict,
    parent_split_sha256: str,
    include_rows: bool,
) -> dict:
    feature_names = list(STANDARD_FEATURES)
    if family_name == "FULL_XG":
        feature_names.extend(FULL_XG_EXTRA_FEATURES)

    partitions: dict[str, dict] = {}
    total_rows = 0
    dropped_missing_xg = 0

    for partition_name in ("train", "validation", "test"):
        source_rows = split_partitions[partition_name].get("rows") or []
        model_rows: list[dict] = []
        for row in source_rows:
            if family_name == "FULL_XG":
                features = _flatten_full_xg(row)
                if features is None:
                    dropped_missing_xg += 1
                    continue
            else:
                features = _flatten_standard(row)
            model_rows.append(_model_row(row, features))

        total_rows += len(model_rows)
        metadata = {
            "version": MODEL_DATASET_VERSION,
            "family": family_name,
            "partition": partition_name,
            "parent_split_sha256": parent_split_sha256,
            "feature_names": feature_names,
            "label_names": LABELS,
            "row_count": len(model_rows),
        }
        partition_sha = _stable_hash({"metadata": metadata, "rows": model_rows})
        payload = {
            "partition_sha256": partition_sha,
            "summary": _partition_stats(model_rows),
        }
        if include_rows:
            payload["rows"] = model_rows
        partitions[partition_name] = payload

    family_meta = {
        "version": MODEL_DATASET_VERSION,
        "family": family_name,
        "parent_split_sha256": parent_split_sha256,
        "feature_names": feature_names,
        "label_names": LABELS,
        "partition_hashes": {name: value["partition_sha256"] for name, value in partitions.items()},
    }
    family_sha = _stable_hash(family_meta)

    return {
        "family": family_name,
        "family_sha256": family_sha,
        "feature_count": len(feature_names),
        "feature_names": feature_names,
        "label_names": LABELS,
        "summary": {
            "rows_total": total_rows,
            "dropped_missing_xg": dropped_missing_xg if family_name == "FULL_XG" else 0,
        },
        "partitions": partitions,
    }


def build_model_dataset_v1(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    lookback_matches: int = 5,
    min_history_matches: int = 3,
    train_ratio: float = 0.70,
    validation_ratio: float = 0.15,
    max_rows: int = 5000,
    include_rows: bool = False,
) -> dict:
    split = build_temporal_training_split(
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

    split_sha = str(split["split_sha256"])
    families = {
        "STANDARD": _build_family("STANDARD", split["partitions"], split_sha, include_rows),
        "FULL_XG": _build_family("FULL_XG", split["partitions"], split_sha, include_rows),
    }

    model_metadata = {
        "version": MODEL_DATASET_VERSION,
        "parent_split_sha256": split_sha,
        "standard_family_sha256": families["STANDARD"]["family_sha256"],
        "full_xg_family_sha256": families["FULL_XG"]["family_sha256"],
    }
    model_sha = _stable_hash(model_metadata)

    return {
        "status": "ok",
        "version": MODEL_DATASET_VERSION,
        "model_dataset_id": f"{MODEL_DATASET_VERSION}:{model_sha[:16]}",
        "model_dataset_sha256": model_sha,
        "parent": {
            "split_id": split["split_id"],
            "split_sha256": split_sha,
            "dataset_id": split["parent_dataset"]["dataset_id"],
            "dataset_sha256": split["parent_dataset"]["dataset_sha256"],
            "training_rows_total": split["parent_dataset"]["training_rows_total"],
        },
        "temporal_boundaries": split["temporal_boundaries"],
        "families": families,
        "policy": {
            "shuffle": False,
            "temporal_split_preserved": True,
            "metadata_fields_are_not_model_features": [
                "fixture_id",
                "sportmonks_fixture_id",
                "starts_at",
                "league",
                "home_team",
                "away_team",
                "source_profile",
            ],
            "standard_family_xg_features": False,
            "standard_family_accepts_full_xg_and_standard_no_xg": True,
            "full_xg_family_requires_non_null_home_and_away_xg": True,
            "xg_absence_is_zero": False,
            "target_match_postgame_data_as_features": False,
            "labels_are_target_postgame_outputs_only": True,
            "deterministic": True,
        },
    }
