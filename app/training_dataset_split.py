from __future__ import annotations

import hashlib
import json
from datetime import date

from app.training_dataset_full import build_full_training_dataset

TEMPORAL_SPLIT_VERSION = "training_dataset_temporal_split_v1"


def _stable_hash(payload: dict) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _group_rows_by_starts_at(rows: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current_key: str | None = None
    current: list[dict] = []

    for row in rows:
        key = str(row["starts_at"])
        if current and key != current_key:
            groups.append(current)
            current = []
        current_key = key
        current.append(row)

    if current:
        groups.append(current)
    return groups


def _boundary_after_target(groups: list[list[dict]], target_rows: float, min_group_index: int = 0) -> int:
    cumulative = 0
    best_index = min_group_index
    best_distance: float | None = None

    for index, group in enumerate(groups):
        cumulative += len(group)
        boundary = index + 1
        if boundary < min_group_index:
            continue
        distance = abs(cumulative - target_rows)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = boundary

    return best_index


def _partition_summary(rows: list[dict]) -> dict:
    profiles: dict[str, int] = {}
    leagues: dict[str, int] = {}
    outcomes: dict[str, int] = {"1": 0, "X": 0, "2": 0}

    for row in rows:
        profile = str(row.get("source_profile"))
        profiles[profile] = profiles.get(profile, 0) + 1
        league = str(row.get("league"))
        leagues[league] = leagues.get(league, 0) + 1
        outcome = str((row.get("label") or {}).get("outcome_1x2"))
        if outcome in outcomes:
            outcomes[outcome] += 1

    starts = [str(row["starts_at"]) for row in rows]
    return {
        "rows": len(rows),
        "starts_at_min": min(starts) if starts else None,
        "starts_at_max": max(starts) if starts else None,
        "source_profiles": dict(sorted(profiles.items())),
        "rows_by_league": dict(sorted(leagues.items())),
        "outcome_1x2": outcomes,
    }


def _partition_payload(name: str, rows: list[dict], parent_dataset_sha256: str) -> dict:
    metadata = {
        "version": TEMPORAL_SPLIT_VERSION,
        "partition": name,
        "parent_dataset_sha256": parent_dataset_sha256,
        "row_count": len(rows),
        "ordering": "starts_at ASC, fixture_id ASC",
    }
    partition_sha256 = _stable_hash({"metadata": metadata, "rows": rows})
    return {
        "name": name,
        "partition_sha256": partition_sha256,
        "summary": _partition_summary(rows),
        "rows": rows,
    }


def build_temporal_training_split(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    lookback_matches: int = 5,
    min_history_matches: int = 3,
    train_ratio: float = 0.70,
    validation_ratio: float = 0.15,
    max_rows: int = 5000,
    include_rows: bool = True,
) -> dict:
    if train_ratio <= 0 or train_ratio >= 1:
        raise ValueError("train_ratio must be greater than 0 and less than 1")
    if validation_ratio <= 0 or validation_ratio >= 1:
        raise ValueError("validation_ratio must be greater than 0 and less than 1")
    if train_ratio + validation_ratio >= 1:
        raise ValueError("train_ratio + validation_ratio must be less than 1 so test remains non-empty")

    full = build_full_training_dataset(
        start_date=start_date,
        end_date=end_date,
        leagues=leagues,
        lookback_matches=lookback_matches,
        min_history_matches=min_history_matches,
        include_skipped_details=False,
        skipped_detail_limit=0,
        max_rows=max_rows,
    )
    rows = list(full["rows"])
    if len(rows) < 3:
        raise ValueError("at least 3 training rows are required for a train/validation/test split")

    groups = _group_rows_by_starts_at(rows)
    if len(groups) < 3:
        raise ValueError("at least 3 distinct starts_at timestamps are required for a temporal split")

    total = len(rows)
    train_target = total * train_ratio
    validation_end_target = total * (train_ratio + validation_ratio)

    train_group_end = _boundary_after_target(groups, train_target, min_group_index=1)
    train_group_end = max(1, min(train_group_end, len(groups) - 2))

    cumulative_before_validation = sum(len(group) for group in groups[:train_group_end])
    remaining_groups = groups[train_group_end:]
    validation_target_within_remaining = validation_end_target - cumulative_before_validation
    validation_relative_end = _boundary_after_target(
        remaining_groups,
        validation_target_within_remaining,
        min_group_index=1,
    )
    validation_group_end = train_group_end + validation_relative_end
    validation_group_end = max(train_group_end + 1, min(validation_group_end, len(groups) - 1))

    train_rows = [row for group in groups[:train_group_end] for row in group]
    validation_rows = [row for group in groups[train_group_end:validation_group_end] for row in group]
    test_rows = [row for group in groups[validation_group_end:] for row in group]

    if not train_rows or not validation_rows or not test_rows:
        raise ValueError("temporal split produced an empty partition; widen the requested date range")

    train_max = str(train_rows[-1]["starts_at"])
    validation_min = str(validation_rows[0]["starts_at"])
    validation_max = str(validation_rows[-1]["starts_at"])
    test_min = str(test_rows[0]["starts_at"])

    strict_temporal_order = train_max < validation_min and validation_max < test_min
    if not strict_temporal_order:
        raise ValueError("strict temporal partition ordering could not be established")

    parent_sha = str(full["dataset_sha256"])
    partitions = {
        "train": _partition_payload("train", train_rows, parent_sha),
        "validation": _partition_payload("validation", validation_rows, parent_sha),
        "test": _partition_payload("test", test_rows, parent_sha),
    }

    split_metadata = {
        "version": TEMPORAL_SPLIT_VERSION,
        "parent_dataset_sha256": parent_sha,
        "train_partition_sha256": partitions["train"]["partition_sha256"],
        "validation_partition_sha256": partitions["validation"]["partition_sha256"],
        "test_partition_sha256": partitions["test"]["partition_sha256"],
        "requested_train_ratio": train_ratio,
        "requested_validation_ratio": validation_ratio,
        "requested_test_ratio": 1 - train_ratio - validation_ratio,
    }
    split_sha256 = _stable_hash(split_metadata)

    if not include_rows:
        for partition in partitions.values():
            partition.pop("rows", None)

    return {
        "status": "ok",
        "version": TEMPORAL_SPLIT_VERSION,
        "split_id": f"{TEMPORAL_SPLIT_VERSION}:{split_sha256[:16]}",
        "split_sha256": split_sha256,
        "parent_dataset": {
            "dataset_id": full["dataset_id"],
            "dataset_sha256": parent_sha,
            "training_rows_total": total,
            "source_builder_version": full["source_builder_version"],
        },
        "requested_ratios": {
            "train": train_ratio,
            "validation": validation_ratio,
            "test": round(1 - train_ratio - validation_ratio, 10),
        },
        "actual_ratios": {
            "train": round(len(train_rows) / total, 6),
            "validation": round(len(validation_rows) / total, 6),
            "test": round(len(test_rows) / total, 6),
        },
        "temporal_boundaries": {
            "train_max_starts_at": train_max,
            "validation_min_starts_at": validation_min,
            "validation_max_starts_at": validation_max,
            "test_min_starts_at": test_min,
            "strict_temporal_order": strict_temporal_order,
            "same_timestamp_never_crosses_partitions": True,
        },
        "partitions": partitions,
        "policy": {
            "shuffle": False,
            "split_strategy": "chronological contiguous partitions grouped by identical starts_at",
            "same_timestamp_never_crosses_partitions": True,
            "train_before_validation": True,
            "validation_before_test": True,
            "target_match_postgame_data_as_features": False,
            "parent_dataset_leakage_violations": full["summary"].get("leakage_violations", 0),
            "xg_absence_is_zero": False,
            "deterministic": True,
        },
    }
