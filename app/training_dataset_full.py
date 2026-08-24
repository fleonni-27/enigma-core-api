from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import or_, select

from app.database import SessionLocal
from app.feature_profiles import classify_fixture_feature_profile
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot
from app.training_dataset import (
    MAX_LOOKBACK_MATCHES,
    STAT_NAMES,
    _aggregate_history,
    _as_list,
    _delta,
    _stat_value,
    _xg_value,
)
from app.training_dataset_v11 import (
    MAX_SKIP_DETAILS,
    VALID_PROFILES,
    _requested_league_context,
    _skip_detail,
)

FULL_DATASET_VERSION = "training_dataset_full_v1"
MAX_FULL_DATASET_ROWS = 5000

# Lazy request/task-local memoization. The cache is not shared across
# independent requests, so dataset freshness between batch runs is preserved.
_request_dataset_cache: ContextVar[dict[str, Any] | None] = ContextVar(
    "full_training_dataset_request_cache",
    default=None,
)


def _stable_dataset_hash(rows: list[dict], metadata: dict) -> str:
    payload = {"metadata": metadata, "rows": rows}
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cache_state() -> dict[str, Any]:
    state = _request_dataset_cache.get()
    if state is None:
        state = {"datasets": {}, "hits": 0, "misses": 0}
        _request_dataset_cache.set(state)
    return state


def request_local_dataset_cache_stats() -> dict[str, int]:
    state = _request_dataset_cache.get()
    if state is None:
        return {"entries": 0, "hits": 0, "misses": 0}
    datasets = state.get("datasets") or {}
    return {
        "entries": len(datasets),
        "hits": int(state.get("hits") or 0),
        "misses": int(state.get("misses") or 0),
    }


def _latest_snapshot_cached(
    session,
    fixture_id: int,
    snapshot_cache: dict[int, FixtureDataSnapshot | None],
) -> FixtureDataSnapshot | None:
    if fixture_id in snapshot_cache:
        return snapshot_cache[fixture_id]
    snapshot = session.scalar(
        select(FixtureDataSnapshot)
        .where(FixtureDataSnapshot.fixture_id == fixture_id)
        .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
        .limit(1)
    )
    snapshot_cache[fixture_id] = snapshot
    return snapshot


def _profile_cached(
    fixture: Fixture,
    profile_cache: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    sportmonks_id = int(fixture.sportmonks_id)
    cached = profile_cache.get(sportmonks_id)
    if cached is not None:
        return cached
    profile = classify_fixture_feature_profile(sportmonks_id)
    profile_cache[sportmonks_id] = profile
    return profile


def _fixture_side_observation_cached(
    session,
    fixture: Fixture,
    team_name: str,
    snapshot_cache: dict[int, FixtureDataSnapshot | None],
    observation_cache: dict[tuple[int, str], dict[str, Any] | None],
) -> dict[str, Any] | None:
    key = (int(fixture.id), str(team_name))
    if key in observation_cache:
        return observation_cache[key]

    snapshot = _latest_snapshot_cached(session, int(fixture.id), snapshot_cache)
    if snapshot is None:
        observation_cache[key] = None
        return None

    statistics = _as_list(snapshot.statistics)
    xg_rows = _as_list(snapshot.xg)
    if not statistics:
        observation_cache[key] = None
        return None

    if fixture.home_team == team_name:
        side, opponent_side = "home", "away"
    elif fixture.away_team == team_name:
        side, opponent_side = "away", "home"
    else:
        observation_cache[key] = None
        return None

    goals_for = _stat_value(statistics, STAT_NAMES["goals"], side)
    goals_against = _stat_value(statistics, STAT_NAMES["goals"], opponent_side)
    if goals_for is None or goals_against is None:
        observation_cache[key] = None
        return None

    if goals_for > goals_against:
        points = 3.0
        result = "W"
    elif goals_for == goals_against:
        points = 1.0
        result = "D"
    else:
        points = 0.0
        result = "L"

    observation = {
        "starts_at": fixture.starts_at,
        "points": points,
        "result": result,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "shots_total_for": _stat_value(statistics, STAT_NAMES["shots_total"], side),
        "shots_on_target_for": _stat_value(
            statistics, STAT_NAMES["shots_on_target"], side
        ),
        "possession": _stat_value(statistics, STAT_NAMES["possession"], side),
        "corners_for": _stat_value(statistics, STAT_NAMES["corners"], side),
        "successful_passes_for": _stat_value(
            statistics, STAT_NAMES["successful_passes"], side
        ),
        "xg_for": _xg_value(xg_rows, statistics, side),
    }
    observation_cache[key] = observation
    return observation


def _team_fixture_candidates_cached(
    session,
    *,
    target: Fixture,
    team_name: str,
    lookback_matches: int,
    team_fixture_cache: dict[str, list[Fixture]],
) -> list[Fixture]:
    cached = team_fixture_cache.get(team_name)
    if cached is None:
        cached = session.scalars(
            select(Fixture)
            .where(
                or_(
                    Fixture.home_team == team_name,
                    Fixture.away_team == team_name,
                )
            )
            .order_by(Fixture.starts_at.desc(), Fixture.id.desc())
        ).all()
        team_fixture_cache[team_name] = list(cached)

    limit = max(lookback_matches * 6, 30)
    return [
        fixture
        for fixture in cached
        if fixture.starts_at < target.starts_at
    ][:limit]


def _team_history_cached(
    session,
    target: Fixture,
    team_name: str,
    league_key: str | None,
    lookback_matches: int,
    *,
    team_fixture_cache: dict[str, list[Fixture]],
    profile_cache: dict[int, dict[str, Any]],
    snapshot_cache: dict[int, FixtureDataSnapshot | None],
    observation_cache: dict[tuple[int, str], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    candidates = _team_fixture_candidates_cached(
        session,
        target=target,
        team_name=team_name,
        lookback_matches=lookback_matches,
        team_fixture_cache=team_fixture_cache,
    )

    history: list[dict[str, Any]] = []
    for fixture in candidates:
        fixture_key = canonical_league(fixture.league_name).get("key")
        if league_key and fixture_key != league_key:
            continue
        profile = _profile_cached(fixture, profile_cache)
        if not profile.get("training_eligible"):
            continue
        observation = _fixture_side_observation_cached(
            session,
            fixture,
            team_name,
            snapshot_cache,
            observation_cache,
        )
        if observation is None:
            continue
        history.append(observation)
        if len(history) >= lookback_matches:
            break
    return history


def _target_label_cached(
    session,
    fixture: Fixture,
    snapshot_cache: dict[int, FixtureDataSnapshot | None],
) -> dict[str, Any] | None:
    snapshot = _latest_snapshot_cached(session, int(fixture.id), snapshot_cache)
    if snapshot is None:
        return None
    statistics = _as_list(snapshot.statistics)
    home_goals = _stat_value(statistics, STAT_NAMES["goals"], "home")
    away_goals = _stat_value(statistics, STAT_NAMES["goals"], "away")
    if home_goals is None or away_goals is None:
        return None

    if home_goals > away_goals:
        outcome = "1"
    elif home_goals == away_goals:
        outcome = "X"
    else:
        outcome = "2"
    total_goals = home_goals + away_goals
    return {
        "outcome_1x2": outcome,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "total_goals": total_goals,
        "btts": bool(home_goals > 0 and away_goals > 0),
        "over_2_5": bool(total_goals > 2.5),
    }


def _build_row_cached(
    session,
    fixture: Fixture,
    lookback_matches: int,
    min_history_matches: int,
    *,
    team_fixture_cache: dict[str, list[Fixture]],
    profile_cache: dict[int, dict[str, Any]],
    snapshot_cache: dict[int, FixtureDataSnapshot | None],
    observation_cache: dict[tuple[int, str], dict[str, Any] | None],
) -> tuple[dict | None, str | None, dict | None]:
    canonical = canonical_league(fixture.league_name)
    league_key = canonical.get("key")

    classification = _profile_cached(fixture, profile_cache)
    profile = str(classification.get("profile") or "")
    if (
        not classification.get("training_eligible")
        or profile not in VALID_PROFILES
    ):
        return None, "not_training_eligible", _skip_detail(
            fixture,
            "not_training_eligible",
            profile=profile or None,
            training_eligible=bool(classification.get("training_eligible")),
        )

    label = _target_label_cached(session, fixture, snapshot_cache)
    if label is None:
        return None, "missing_label", _skip_detail(fixture, "missing_label")

    home_history = _team_history_cached(
        session,
        fixture,
        fixture.home_team,
        league_key,
        lookback_matches,
        team_fixture_cache=team_fixture_cache,
        profile_cache=profile_cache,
        snapshot_cache=snapshot_cache,
        observation_cache=observation_cache,
    )
    away_history = _team_history_cached(
        session,
        fixture,
        fixture.away_team,
        league_key,
        lookback_matches,
        team_fixture_cache=team_fixture_cache,
        profile_cache=profile_cache,
        snapshot_cache=snapshot_cache,
        observation_cache=observation_cache,
    )
    if len(home_history) < min_history_matches or len(away_history) < min_history_matches:
        return None, "insufficient_history", _skip_detail(
            fixture,
            "insufficient_history",
            home_history_matches=len(home_history),
            away_history_matches=len(away_history),
            minimum_required=min_history_matches,
            requested_lookback=lookback_matches,
        )

    historical_times = [
        row.get("starts_at")
        for row in home_history + away_history
        if row.get("starts_at") is not None
    ]
    if any(value >= fixture.starts_at for value in historical_times):
        return None, "leakage_violation", _skip_detail(
            fixture,
            "leakage_violation",
        )

    home = _aggregate_history(home_history, fixture.starts_at)
    away = _aggregate_history(away_history, fixture.starts_at)
    home["history_completeness_ratio"] = round(
        len(home_history) / lookback_matches, 4
    )
    away["history_completeness_ratio"] = round(
        len(away_history) / lookback_matches, 4
    )

    features = {
        "home": home,
        "away": away,
        "delta": {
            "points_per_match": _delta(home, away, "points_per_match"),
            "goals_for_avg": _delta(home, away, "goals_for_avg"),
            "goals_against_avg": _delta(home, away, "goals_against_avg"),
            "shots_total_for_avg": _delta(home, away, "shots_total_for_avg"),
            "shots_on_target_for_avg": _delta(
                home, away, "shots_on_target_for_avg"
            ),
            "possession_avg": _delta(home, away, "possession_avg"),
            "corners_for_avg": _delta(home, away, "corners_for_avg"),
            "successful_passes_for_avg": _delta(
                home, away, "successful_passes_for_avg"
            ),
            "xg_for_avg": _delta(home, away, "xg_for_avg"),
            "rest_days": _delta(home, away, "rest_days"),
            "history_completeness_ratio": _delta(
                home, away, "history_completeness_ratio"
            ),
        },
    }

    row = {
        "sportmonks_fixture_id": int(fixture.sportmonks_id),
        "fixture_id": int(fixture.id),
        "league": canonical.get("canonical_name") or fixture.league_name,
        "starts_at": fixture.starts_at.isoformat(),
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "source_profile": profile,
        "features": features,
        "label": label,
        "leakage_audit": {
            "target_statistics_used_as_features": False,
            "history_strictly_before_target": True,
        },
    }
    return row, None, None


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
        raise ValueError(
            f"lookback_matches must be between 1 and {MAX_LOOKBACK_MATCHES}"
        )
    if min_history_matches < 1 or min_history_matches > lookback_matches:
        raise ValueError("min_history_matches must be between 1 and lookback_matches")
    if skipped_detail_limit < 0 or skipped_detail_limit > MAX_SKIP_DETAILS:
        raise ValueError(
            f"skipped_detail_limit must be between 0 and {MAX_SKIP_DETAILS}"
        )
    if max_rows < 1 or max_rows > MAX_FULL_DATASET_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_FULL_DATASET_ROWS}")

    cache_state = _cache_state()
    cache_key = (
        start_date.isoformat(),
        end_date.isoformat(),
        tuple(leagues or []),
        int(lookback_matches),
        int(min_history_matches),
        bool(include_skipped_details),
        int(skipped_detail_limit),
        int(max_rows),
    )
    datasets: dict[tuple[Any, ...], dict] = cache_state["datasets"]
    cached = datasets.get(cache_key)
    if cached is not None:
        cache_state["hits"] = int(cache_state.get("hits") or 0) + 1
        return cached
    cache_state["misses"] = int(cache_state.get("misses") or 0) + 1

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys, requested_names = _requested_league_context(leagues)

    with SessionLocal() as session:
        stmt = select(Fixture).where(Fixture.starts_at.between(start_dt, end_dt))
        if requested_names:
            stmt = stmt.where(Fixture.league_name.in_(sorted(requested_names)))
        candidates = session.scalars(
            stmt.order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

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

        team_fixture_cache: dict[str, list[Fixture]] = {}
        profile_cache: dict[int, dict[str, Any]] = {}
        snapshot_cache: dict[int, FixtureDataSnapshot | None] = {}
        observation_cache: dict[tuple[int, str], dict[str, Any] | None] = {}

        for fixture in candidates:
            canonical = canonical_league(fixture.league_name)
            league_key = canonical.get("key")
            if requested_keys and league_key not in requested_keys:
                skipped["not_target_league"] += 1
                if (
                    include_skipped_details
                    and len(skipped_details) < skipped_detail_limit
                ):
                    skipped_details.append(
                        _skip_detail(fixture, "not_target_league")
                    )
                continue

            row, reason, detail = _build_row_cached(
                session,
                fixture,
                lookback_matches,
                min_history_matches,
                team_fixture_cache=team_fixture_cache,
                profile_cache=profile_cache,
                snapshot_cache=snapshot_cache,
                observation_cache=observation_cache,
            )
            if row is None:
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                if (
                    include_skipped_details
                    and detail
                    and len(skipped_details) < skipped_detail_limit
                ):
                    skipped_details.append(detail)
                continue

            rows.append(row)
            if len(rows) > max_rows:
                raise ValueError(
                    f"full dataset has more than max_rows={max_rows}; "
                    "split the requested date range into smaller windows"
                )

            profile = str(row["source_profile"])
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
            league = str(row["league"])
            league_counts[league] = league_counts.get(league, 0) + 1

        build_performance = {
            "cache_scope": "single_dataset_build",
            "team_fixture_lists_cached": len(team_fixture_cache),
            "fixture_profiles_cached": len(profile_cache),
            "latest_snapshots_cached": len(snapshot_cache),
            "side_observations_cached": len(observation_cache),
            "history_candidate_semantics": (
                "filter starts_at before target, then apply original candidate limit"
            ),
        }

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
    dataset_id = (
        f"{FULL_DATASET_VERSION}:{start_date.isoformat()}:"
        f"{end_date.isoformat()}:{dataset_sha256[:16]}"
    )

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
            "xg_feature_ready_pct_total": (
                round((xg_ready_total / len(rows)) * 100, 1) if rows else 0
            ),
            "leakage_violations": skipped.get("leakage_violation", 0),
            "skipped": skipped,
        },
        "build_parameters": {
            "lookback_matches": lookback_matches,
            "min_history_matches": min_history_matches,
            "max_rows_safety": max_rows,
        },
        "build_performance": build_performance,
        "feature_schema": {
            "history_scope": "same canonical league and strictly earlier fixtures only",
            "rolling_window_matches": lookback_matches,
            "minimum_history_matches": min_history_matches,
            "labels": [
                "outcome_1x2",
                "home_goals",
                "away_goals",
                "total_goals",
                "btts",
                "over_2_5",
            ],
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
            "history_cutoff_rule": (
                "historical fixture starts_at must be strictly less than "
                "target fixture starts_at"
            ),
            "xg_absence_is_zero": False,
            "xg_missing_value": None,
            "deterministic_order": "starts_at ASC, fixture_id ASC",
            "dataset_hash_algorithm": "sha256",
            "build_caches_do_not_change_dataset_hash": True,
        },
    }
    if include_skipped_details:
        response["skipped_details"] = {
            "returned": len(skipped_details),
            "limit": skipped_detail_limit,
            "items": skipped_details,
        }

    datasets[cache_key] = response
    return response
