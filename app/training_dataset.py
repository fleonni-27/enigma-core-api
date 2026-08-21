from __future__ import annotations

from datetime import date, datetime, time, timezone
from statistics import mean
from typing import Any

from sqlalchemy import or_, select

from app.database import SessionLocal
from app.feature_profiles import classify_fixture_feature_profile
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot

TRAINING_DATASET_VERSION = "training_dataset_v1"
MAX_TRAINING_ROWS = 200
MAX_LOOKBACK_MATCHES = 10

STAT_NAMES = {
    "goals": "Goals",
    "shots_total": "Shots Total",
    "shots_on_target": "Shots On Target",
    "possession": "Ball Possession %",
    "corners": "Corners",
    "successful_passes": "Successful Passes",
}
EXPECTED_XG_TYPE = "Expected Goals (xG)"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _type_name(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    raw_type = row.get("type")
    if isinstance(raw_type, dict):
        value = raw_type.get("name") or raw_type.get("developer_name") or raw_type.get("code") or raw_type.get("id")
        return str(value) if value is not None else None
    if raw_type is not None:
        return str(raw_type)
    return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("%", "").replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _numeric_value(row: Any) -> float | None:
    if not isinstance(row, dict):
        return _to_float(row)
    direct = _to_float(row.get("value"))
    if direct is not None:
        return direct
    data = row.get("data")
    direct_data = _to_float(data)
    if direct_data is not None:
        return direct_data
    if isinstance(data, dict):
        for key in ("value", "total", "amount", "score", "percentage"):
            parsed = _to_float(data.get(key))
            if parsed is not None:
                return parsed
    return None


def _latest_snapshot(session, fixture_id: int) -> FixtureDataSnapshot | None:
    return session.scalar(
        select(FixtureDataSnapshot)
        .where(FixtureDataSnapshot.fixture_id == fixture_id)
        .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
        .limit(1)
    )


def _stat_value(statistics: list[Any], type_name: str, location: str) -> float | None:
    values: list[float] = []
    for row in statistics:
        if not isinstance(row, dict):
            continue
        if _type_name(row) != type_name:
            continue
        if str(row.get("location") or "").lower() != location:
            continue
        value = _numeric_value(row)
        if value is not None:
            values.append(value)
    return values[-1] if values else None


def _participant_by_location(statistics: list[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in statistics:
        if not isinstance(row, dict):
            continue
        location = str(row.get("location") or "").lower()
        participant_id = row.get("participant_id")
        if location not in {"home", "away"} or participant_id is None:
            continue
        try:
            result[location] = int(participant_id)
        except (TypeError, ValueError):
            pass
    return result


def _xg_value(xg_rows: list[Any], statistics: list[Any], location: str) -> float | None:
    participant_by_location = _participant_by_location(statistics)
    expected_participant = participant_by_location.get(location)
    values: list[float] = []
    for row in xg_rows:
        if not isinstance(row, dict) or _type_name(row) != EXPECTED_XG_TYPE:
            continue
        row_location = str(row.get("location") or "").lower()
        matches_location = row_location == location
        if not matches_location and expected_participant is not None:
            try:
                matches_location = int(row.get("participant_id")) == expected_participant
            except (TypeError, ValueError):
                matches_location = False
        if not matches_location:
            continue
        value = _numeric_value(row)
        if value is not None:
            values.append(value)
    return values[-1] if values else None


def _fixture_side_observation(session, fixture: Fixture, team_name: str) -> dict | None:
    snapshot = _latest_snapshot(session, int(fixture.id))
    if snapshot is None:
        return None
    statistics = _as_list(snapshot.statistics)
    xg_rows = _as_list(snapshot.xg)
    if not statistics:
        return None

    if fixture.home_team == team_name:
        side, opponent_side = "home", "away"
    elif fixture.away_team == team_name:
        side, opponent_side = "away", "home"
    else:
        return None

    goals_for = _stat_value(statistics, STAT_NAMES["goals"], side)
    goals_against = _stat_value(statistics, STAT_NAMES["goals"], opponent_side)
    if goals_for is None or goals_against is None:
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

    return {
        "starts_at": fixture.starts_at,
        "points": points,
        "result": result,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "shots_total_for": _stat_value(statistics, STAT_NAMES["shots_total"], side),
        "shots_on_target_for": _stat_value(statistics, STAT_NAMES["shots_on_target"], side),
        "possession": _stat_value(statistics, STAT_NAMES["possession"], side),
        "corners_for": _stat_value(statistics, STAT_NAMES["corners"], side),
        "successful_passes_for": _stat_value(statistics, STAT_NAMES["successful_passes"], side),
        "xg_for": _xg_value(xg_rows, statistics, side),
    }


def _avg(rows: list[dict], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return round(mean(values), 4) if values else None


def _team_history(session, target: Fixture, team_name: str, league_key: str | None, lookback_matches: int) -> list[dict]:
    candidates = session.scalars(
        select(Fixture)
        .where(
            Fixture.starts_at < target.starts_at,
            or_(Fixture.home_team == team_name, Fixture.away_team == team_name),
        )
        .order_by(Fixture.starts_at.desc(), Fixture.id.desc())
        .limit(max(lookback_matches * 6, 30))
    ).all()

    history: list[dict] = []
    for fixture in candidates:
        fixture_key = canonical_league(fixture.league_name).get("key")
        if league_key and fixture_key != league_key:
            continue
        profile = classify_fixture_feature_profile(int(fixture.sportmonks_id))
        if not profile.get("training_eligible"):
            continue
        observation = _fixture_side_observation(session, fixture, team_name)
        if observation is None:
            continue
        history.append(observation)
        if len(history) >= lookback_matches:
            break
    return history


def _aggregate_history(history: list[dict], target_starts_at: datetime) -> dict:
    matches = len(history)
    wins = sum(1 for row in history if row.get("result") == "W")
    draws = sum(1 for row in history if row.get("result") == "D")
    latest_at = history[0].get("starts_at") if history else None
    rest_days = None
    if latest_at is not None:
        rest_days = round((target_starts_at - latest_at).total_seconds() / 86400.0, 2)

    xg_matches = sum(1 for row in history if row.get("xg_for") is not None)
    return {
        "history_matches": matches,
        "points_per_match": _avg(history, "points"),
        "win_rate": round(wins / matches, 4) if matches else None,
        "draw_rate": round(draws / matches, 4) if matches else None,
        "goals_for_avg": _avg(history, "goals_for"),
        "goals_against_avg": _avg(history, "goals_against"),
        "shots_total_for_avg": _avg(history, "shots_total_for"),
        "shots_on_target_for_avg": _avg(history, "shots_on_target_for"),
        "possession_avg": _avg(history, "possession"),
        "corners_for_avg": _avg(history, "corners_for"),
        "successful_passes_for_avg": _avg(history, "successful_passes_for"),
        "xg_for_avg": _avg(history, "xg_for"),
        "xg_history_matches": xg_matches,
        "rest_days": rest_days,
        "latest_history_starts_at": latest_at.isoformat() if latest_at else None,
    }


def _delta(home: dict, away: dict, key: str) -> float | None:
    h, a = home.get(key), away.get(key)
    if h is None or a is None:
        return None
    return round(float(h) - float(a), 4)


def _target_label(session, fixture: Fixture) -> dict | None:
    snapshot = _latest_snapshot(session, int(fixture.id))
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


def build_training_dataset(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    limit: int = 100,
    lookback_matches: int = 5,
    min_history_matches: int = 3,
) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if limit < 1 or limit > MAX_TRAINING_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_TRAINING_ROWS}")
    if lookback_matches < 1 or lookback_matches > MAX_LOOKBACK_MATCHES:
        raise ValueError(f"lookback_matches must be between 1 and {MAX_LOOKBACK_MATCHES}")
    if min_history_matches < 1 or min_history_matches > lookback_matches:
        raise ValueError("min_history_matches must be between 1 and lookback_matches")

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys = {
        str(canonical_league(name).get("key"))
        for name in leagues or []
        if canonical_league(name).get("target") and canonical_league(name).get("key")
    }

    with SessionLocal() as session:
        candidates = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

        rows: list[dict] = []
        skipped = {"not_target_league": 0, "not_training_eligible": 0, "missing_label": 0, "insufficient_history": 0}
        profile_counts = {"FULL_XG": 0, "STANDARD_NO_XG": 0}
        leakage_violations = 0

        for fixture in candidates:
            canonical = canonical_league(fixture.league_name)
            league_key = canonical.get("key")
            if requested_keys and league_key not in requested_keys:
                skipped["not_target_league"] += 1
                continue

            classification = classify_fixture_feature_profile(int(fixture.sportmonks_id))
            profile = str(classification.get("profile") or "")
            if not classification.get("training_eligible") or profile not in {"FULL_XG", "STANDARD_NO_XG"}:
                skipped["not_training_eligible"] += 1
                continue

            label = _target_label(session, fixture)
            if label is None:
                skipped["missing_label"] += 1
                continue

            home_history = _team_history(session, fixture, fixture.home_team, league_key, lookback_matches)
            away_history = _team_history(session, fixture, fixture.away_team, league_key, lookback_matches)
            if len(home_history) < min_history_matches or len(away_history) < min_history_matches:
                skipped["insufficient_history"] += 1
                continue

            home = _aggregate_history(home_history, fixture.starts_at)
            away = _aggregate_history(away_history, fixture.starts_at)
            latest_history_times = [row.get("starts_at") for row in home_history + away_history if row.get("starts_at") is not None]
            if any(value >= fixture.starts_at for value in latest_history_times):
                leakage_violations += 1
                continue

            features = {
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
                },
            }

            rows.append(
                {
                    "sportmonks_fixture_id": fixture.sportmonks_id,
                    "fixture_id": fixture.id,
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
            )
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
            if len(rows) >= limit:
                break

    xg_feature_ready = sum(
        1 for row in rows
        if row["features"]["home"].get("xg_for_avg") is not None and row["features"]["away"].get("xg_for_avg") is not None
    )
    return {
        "status": "ok",
        "version": TRAINING_DATASET_VERSION,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "normalized_league_keys": sorted(requested_keys),
        "limit": limit,
        "lookback_matches": lookback_matches,
        "min_history_matches": min_history_matches,
        "summary": {
            "training_rows": len(rows),
            "source_profiles": profile_counts,
            "xg_feature_ready_rows": xg_feature_ready,
            "xg_feature_ready_pct": round(xg_feature_ready / len(rows) * 100.0, 1) if rows else 0.0,
            "leakage_violations": leakage_violations,
            "skipped": skipped,
        },
        "feature_schema": {
            "history_scope": "same canonical league and strictly earlier fixtures only",
            "rolling_window_matches": lookback_matches,
            "team_features": [
                "history_matches", "points_per_match", "win_rate", "draw_rate", "goals_for_avg", "goals_against_avg",
                "shots_total_for_avg", "shots_on_target_for_avg", "possession_avg", "corners_for_avg",
                "successful_passes_for_avg", "xg_for_avg", "xg_history_matches", "rest_days",
            ],
            "labels": ["outcome_1x2", "home_goals", "away_goals", "total_goals", "btts", "over_2_5"],
        },
        "rows": rows,
        "policy": {
            "read_only": True,
            "training_eligible_only": True,
            "excluded_profiles": ["INCOMPLETE", "NO_SNAPSHOT"],
            "upstream_unavailable_excluded": True,
            "target_match_postgame_data_as_features": False,
            "target_match_postgame_data_allowed_for_labels_only": True,
            "history_cutoff_rule": "historical fixture starts_at must be strictly less than target fixture starts_at",
            "xg_absence_is_zero": False,
            "xg_missing_value": None,
            "source_profile_is_metadata_not_model_feature": True,
        },
    }
