from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture
from app.training_dataset import MAX_LOOKBACK_MATCHES
from app.training_dataset_v11 import (
    MAX_SKIP_DETAILS,
    _build_row,
    _requested_league_context,
    _skip_detail,
)

FULL_DATASET_VERSION = "training_dataset_full_v1"
MAX_FULL_DATASET_ROWS = 5000


def _stable_dataset_hash(rows: list[dict], metadata: dict) -> str:
    payload = {
        "metadata": metadata,
        "rows": rows,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_full_training_dataset(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    lookback_matches: int = 5,
    min_history_matches: int = 3,
    include_skipped_details: bool = False,
    skipped_detail_limit: int = 100,
    max_rows: int = MAX_FULL_DATASET_ROWS,
) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if lookback_matches < 1 or lookback_matches > MAX_LOOKBACK_MATCHES:
        raise ValueError(f"lookback_matches must be between 1 and {MAX_LOOKBACK_MATCHES}")
    if min_history_matches < 1 or min_history_matches > lookback_matches:
        raise ValueError("min_history_matches must be between 1 and lookback_matches")
    if skipped_detail_limit < 0 or skipped_detail_limit > MAX_SKIP_DETAILS:
        raise ValueError(f"skipped_detail_limit must be between 0 and {MAX_SKIP_DETAILS}")
    if max_rows < 1 or max_rows > MAX_FULL_DATASET_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_FULL_DATASET_ROWS}")

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys, requested_names = _requested_league_context(leagues)

    with SessionLocal() as session:
        stmt = select(Fixture).where(Fixture.starts_at.between(start_dt, end_dt))
        if requested_names:
            stmt = stmt.where(Fixture.league_name.in_(sorted(requested_names)))
        candidates = session.scalars(stmt.order_by(Fixture.starts_at.asc(), Fixture.id.asc())).all()

        rows: list[dict] = []
        skipped = {
            "not_target_league": 0,
            "not_training_eligible": 0,
            "missing_label": 0,
            "insufficient_history": 0,
            "leakage_violation": 0,
        }
        skipped_details: list[dict] = []
        profile_counts = {"FULL_XG": 0, "STANDARD_NO_XG": 0}
        league_counts: dict[str, int] = {}

        for fixture in candidates:
            canonical = canonical_league(fixture.league_name)
            league_key = canonical.get("key")
            if requested_keys and league_key not in requested_keys:
                skipped["not_target_league"] += 1
                if include_skipped_details and len(skipped_details) < skipped_detail_limit:
                    skipped_details.append(_skip_detail(fixture, "not_target_league"))
                continue

            row, reason, detail = _build_row(session, fixture, lookback_matches, min_history_matches)
            if row is None:
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                if include_skipped_details and detail and len(skipped_details) < skipped_detail_limit:
                    skipped_details.append(detail)
                continue

            rows.append(row)
            if len(rows) > max_rows:
                raise ValueError(
                    f"full dataset has more than max_rows={max_rows}; split the requested date range into smaller windows"
                )

            profile = str(row["source_profile"])
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
            league = str(row["league"])
            league_counts[league] = league_counts.get(league, 0) + 1

    xg_ready_total = sum(
        1
        for row in rows
        if row["features"]["home"].get("xg_for_avg") is not None
        and row["features"]["away"].get("xg_for_avg") is not None
    )

    metadata = {
        "version": FULL_DATASET_VERSION,
        "source_builder_version": "training_dataset_v1.1",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "normalized_league_keys": sorted(requested_keys),
        "lookback_matches": lookback_matches,
        "min_history_matches": min_history_matches,
        "row_count": len(rows),
        "deterministic_order": "starts_at ASC, fixture_id ASC",
    }
    dataset_sha256 = _stable_dataset_hash(rows, metadata)
    dataset_id = f"{FULL_DATASET_VERSION}:{start_date.isoformat()}:{end_date.isoformat()}:{dataset_sha256[:16]}"

    response = {
        "status": "ok",
        "version": FULL_DATASET_VERSION,
        "source_builder_version": "training_dataset_v1.1",
        "dataset_id": dataset_id,
        "dataset_sha256": dataset_sha256,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "normalized_league_keys": sorted(requested_keys),
        "summary": {
            "candidate_fixtures_after_sql_filter": len(candidates),
            "training_rows_total": len(rows),
            "source_profiles_total": profile_counts,
            "rows_by_league": dict(sorted(league_counts.items())),
            "xg_feature_ready_rows_total": xg_ready_total,
            "xg_feature_ready_pct_total": round((xg_ready_total / len(rows)) * 100, 1) if rows else 0,
            "leakage_violations": skipped.get("leakage_violation", 0),
            "skipped": skipped,
        },
        "build_parameters": {
            "lookback_matches": lookback_matches,
            "min_history_matches": min_history_matches,
            "max_rows_safety": max_rows,
        },
        "feature_schema": {
            "history_scope": "same canonical league and strictly earlier fixtures only",
            "rolling_window_matches": lookback_matches,
            "minimum_history_matches": min_history_matches,
            "labels": ["outcome_1x2", "home_goals", "away_goals", "total_goals", "btts", "over_2_5"],
        },
        "rows": rows,
        "policy": {
            "read_only": True,
            "complete_requested_window_or_explicit_failure": True,
            "training_eligible_only": True,
            "excluded_profiles": ["INCOMPLETE", "NO_SNAPSHOT"],
            "upstream_unavailable_excluded": True,
            "target_match_postgame_data_as_features": False,
            "target_match_postgame_data_allowed_for_labels_only": True,
            "history_cutoff_rule": "historical fixture starts_at must be strictly less than target fixture starts_at",
            "xg_absence_is_zero": False,
            "xg_missing_value": None,
            "deterministic_order": "starts_at ASC, fixture_id ASC",
            "dataset_hash_algorithm": "sha256",
        },
    }
    if include_skipped_details:
        response["skipped_details"] = {
            "returned": len(skipped_details),
            "limit": skipped_detail_limit,
            "items": skipped_details,
        }
    return response
