from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from sqlalchemy import select

from app.daily_prediction_runner import PrematchContextSnapshot
from app.database import SessionLocal
from app.football_probability_models import DEFAULT_ELO_HOME_ADVANTAGE, elo_update
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot
from app.training_dataset import _as_list, _stat_value, _xg_value, STAT_NAMES

ENIGMA_RATING_V2_CONTEXT_VERSION = "enigma_rating_v2_context_v1"
DEFAULT_FORM_LOOKBACK = 10
DEFAULT_ELO_HISTORY_DAYS = 1460
DEFAULT_ELO_INITIAL = 1500.0
DEFAULT_ELO_K_FACTOR = 20.0


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _avg(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return round(mean(values), 4) if values else None


def _observation_from_snapshot(
    fixture: Fixture,
    snapshot: FixtureDataSnapshot,
    *,
    team_name: str,
) -> dict[str, Any] | None:
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

    points = 3.0 if goals_for > goals_against else (1.0 if goals_for == goals_against else 0.0)
    return {
        "starts_at": _aware_utc(fixture.starts_at),
        "points": points,
        "goals_for": float(goals_for),
        "goals_against": float(goals_against),
        "xg_for": _xg_value(xg_rows, statistics, side),
        "xg_against": _xg_value(xg_rows, statistics, opponent_side),
    }


def _aggregate_team_history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "history_matches": len(rows),
        "points_per_match": _avg(rows, "points"),
        "goals_for_avg": _avg(rows, "goals_for"),
        "goals_against_avg": _avg(rows, "goals_against"),
        "xg_for_avg": _avg(rows, "xg_for"),
        "xg_against_avg": _avg(rows, "xg_against"),
        "xg_for_history_matches": sum(1 for row in rows if row.get("xg_for") is not None),
        "xg_against_history_matches": sum(1 for row in rows if row.get("xg_against") is not None),
        "latest_history_starts_at": rows[0]["starts_at"].isoformat() if rows else None,
    }


def _result_home_score(fixture: Fixture, snapshot: FixtureDataSnapshot) -> float | None:
    statistics = _as_list(snapshot.statistics)
    home_goals = _stat_value(statistics, STAT_NAMES["goals"], "home")
    away_goals = _stat_value(statistics, STAT_NAMES["goals"], "away")
    if home_goals is None or away_goals is None:
        return None
    if home_goals > away_goals:
        return 1.0
    if home_goals < away_goals:
        return 0.0
    return 0.5


def _latest_snapshot_map(session, fixture_ids: list[int]) -> dict[int, FixtureDataSnapshot]:
    if not fixture_ids:
        return {}
    rows = session.scalars(
        select(FixtureDataSnapshot)
        .where(FixtureDataSnapshot.fixture_id.in_(fixture_ids))
        .order_by(
            FixtureDataSnapshot.fixture_id.asc(),
            FixtureDataSnapshot.fetched_at.desc(),
            FixtureDataSnapshot.id.desc(),
        )
    ).all()
    result: dict[int, FixtureDataSnapshot] = {}
    for row in rows:
        fixture_id = int(row.fixture_id)
        result.setdefault(fixture_id, row)
    return result


def _lineup_summary(lineups: Any) -> dict[str, Any]:
    rows = lineups if isinstance(lineups, list) else []
    starter_rows = [
        row for row in rows
        if isinstance(row, dict) and str(row.get("type_id") or "") == "11"
    ]
    starters_by_team_id: dict[str, int] = {}
    starter_player_ids: list[int] = []
    for row in starter_rows:
        team_id = row.get("team_id")
        if team_id is not None:
            key = str(team_id)
            starters_by_team_id[key] = starters_by_team_id.get(key, 0) + 1
        player_id = row.get("player_id")
        try:
            if player_id is not None:
                starter_player_ids.append(int(player_id))
        except (TypeError, ValueError):
            pass
    return {
        "lineup_rows": len(rows),
        "starter_rows": len(starter_rows),
        "starters_by_team_id": dict(sorted(starters_by_team_id.items())),
        "starter_player_ids": sorted(set(starter_player_ids)),
        "impact_scored": False,
        "impact_reason": "PLAYER_ABSENCE_VALUE_MODEL_NOT_AVAILABLE",
    }


def build_enigma_rating_v2_context(
    sportmonks_fixture_id: int,
    *,
    form_lookback: int = DEFAULT_FORM_LOOKBACK,
    elo_history_days: int = DEFAULT_ELO_HISTORY_DAYS,
    elo_initial: float = DEFAULT_ELO_INITIAL,
    elo_k_factor: float = DEFAULT_ELO_K_FACTOR,
    elo_home_advantage: float = DEFAULT_ELO_HOME_ADVANTAGE,
) -> dict[str, Any]:
    if form_lookback < 3 or form_lookback > 10:
        raise ValueError("form_lookback must be between 3 and 10")
    if elo_history_days < 180 or elo_history_days > 3650:
        raise ValueError("elo_history_days must be between 180 and 3650")
    if elo_k_factor <= 0.0 or elo_k_factor > 80.0:
        raise ValueError("elo_k_factor must be > 0 and <= 80")

    with SessionLocal() as session:
        target = session.scalar(
            select(Fixture).where(Fixture.sportmonks_id == int(sportmonks_fixture_id))
        )
        if target is None:
            return {
                "status": "fixture_not_found",
                "version": ENIGMA_RATING_V2_CONTEXT_VERSION,
                "sportmonks_fixture_id": int(sportmonks_fixture_id),
            }

        target_starts_at = _aware_utc(target.starts_at)
        canonical = canonical_league(target.league_name)
        league_key = canonical.get("key")
        if not canonical.get("target") or not league_key:
            return {
                "status": "not_ready",
                "version": ENIGMA_RATING_V2_CONTEXT_VERSION,
                "reason_codes": ["UNSUPPORTED_TARGET_LEAGUE"],
                "sportmonks_fixture_id": int(sportmonks_fixture_id),
            }

        elo_cutoff = target_starts_at - timedelta(days=int(elo_history_days))
        candidates = session.scalars(
            select(Fixture)
            .where(
                Fixture.starts_at < target_starts_at,
                Fixture.starts_at >= elo_cutoff,
            )
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()
        league_fixtures = [
            fixture for fixture in candidates
            if canonical_league(fixture.league_name).get("key") == league_key
        ]
        snapshot_map = _latest_snapshot_map(
            session,
            [int(fixture.id) for fixture in league_fixtures],
        )

        histories: dict[str, list[dict[str, Any]]] = {
            str(target.home_team): [],
            str(target.away_team): [],
        }
        for fixture in reversed(league_fixtures):
            snapshot = snapshot_map.get(int(fixture.id))
            if snapshot is None:
                continue
            for team_name in histories:
                if len(histories[team_name]) >= form_lookback:
                    continue
                if fixture.home_team != team_name and fixture.away_team != team_name:
                    continue
                observation = _observation_from_snapshot(
                    fixture,
                    snapshot,
                    team_name=team_name,
                )
                if observation is not None:
                    histories[team_name].append(observation)

        home_history = _aggregate_team_history(histories[str(target.home_team)])
        away_history = _aggregate_team_history(histories[str(target.away_team)])

        ratings: dict[str, float] = {}
        elo_matches = 0
        for fixture in league_fixtures:
            snapshot = snapshot_map.get(int(fixture.id))
            if snapshot is None:
                continue
            home_score = _result_home_score(fixture, snapshot)
            if home_score is None:
                continue
            home_rating = ratings.get(str(fixture.home_team), float(elo_initial))
            away_rating = ratings.get(str(fixture.away_team), float(elo_initial))
            new_home, new_away = elo_update(
                home_rating,
                away_rating,
                home_score=home_score,
                k_factor=float(elo_k_factor),
                home_advantage_elo=float(elo_home_advantage),
            )
            ratings[str(fixture.home_team)] = new_home
            ratings[str(fixture.away_team)] = new_away
            elo_matches += 1

        lineup_row = session.scalar(
            select(PrematchContextSnapshot)
            .where(PrematchContextSnapshot.fixture_id == int(target.id))
            .order_by(
                PrematchContextSnapshot.fetched_at.desc(),
                PrematchContextSnapshot.id.desc(),
            )
            .limit(1)
        )
        lineup = _lineup_summary(lineup_row.lineups if lineup_row is not None else None)
        if lineup_row is not None:
            lineup.update(
                {
                    "snapshot_id": int(lineup_row.id),
                    "snapshot_window": lineup_row.snapshot_window,
                    "fetched_at": lineup_row.fetched_at.isoformat() if lineup_row.fetched_at else None,
                }
            )

    rating_inputs = {
        "home_goals_for_avg": home_history["goals_for_avg"],
        "away_goals_for_avg": away_history["goals_for_avg"],
        "home_goals_against_avg": home_history["goals_against_avg"],
        "away_goals_against_avg": away_history["goals_against_avg"],
        "home_xg_for_avg": home_history["xg_for_avg"],
        "away_xg_for_avg": away_history["xg_for_avg"],
        "home_xg_against_avg": home_history["xg_against_avg"],
        "away_xg_against_avg": away_history["xg_against_avg"],
        "home_points_per_match_10": home_history["points_per_match"] if form_lookback == 10 else None,
        "away_points_per_match_10": away_history["points_per_match"] if form_lookback == 10 else None,
        "home_elo": round(ratings.get(str(target.home_team), float(elo_initial)), 4),
        "away_elo": round(ratings.get(str(target.away_team), float(elo_initial)), 4),
    }

    return {
        "status": "ok",
        "version": ENIGMA_RATING_V2_CONTEXT_VERSION,
        "fixture": {
            "fixture_id": int(target.id),
            "sportmonks_fixture_id": int(target.sportmonks_id),
            "league": canonical.get("canonical_name") or target.league_name,
            "home_team": target.home_team,
            "away_team": target.away_team,
            "starts_at": target_starts_at.isoformat(),
        },
        "rating_inputs": rating_inputs,
        "history": {
            "lookback_matches": int(form_lookback),
            "home": home_history,
            "away": away_history,
        },
        "elo": {
            "history_days": int(elo_history_days),
            "matches_processed": elo_matches,
            "initial_rating": float(elo_initial),
            "k_factor": float(elo_k_factor),
            "home_advantage_elo": float(elo_home_advantage),
        },
        "lineup_context": lineup,
        "policy": {
            "research_only": True,
            "history_strictly_before_target": True,
            "target_postgame_snapshot_used": False,
            "xga_is_opponent_xg_from_historical_match": True,
            "form_10_requires_exact_10_match_request": True,
            "elo_only_uses_same_league_pre_target_results": True,
            "lineup_presence_is_observed_but_absence_impact_is_not_guessed": True,
        },
    }
