from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select

from app.dashboard_j1_team_enrichment import (
    LOOKBACK_DAYS,
    LOOKBACK_MATCHES,
    _facts,
    _latest_snapshot_map,
    _observation,
    _relative_strength,
    _team_summary,
)
from app.database import SessionLocal
from app.models import Fixture

STREAM_VERSION = "dashboard_j1_team_enrichment_stream_v1"
HISTORY_SCAN_LIMIT_PER_TEAM = 24


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _team_rows(*, team: str, league: str, target_start: datetime) -> list[dict[str, Any]]:
    earliest = target_start - timedelta(days=LOOKBACK_DAYS)
    with SessionLocal() as session:
        fixtures = session.scalars(
            select(Fixture)
            .where(
                Fixture.starts_at >= earliest,
                Fixture.starts_at < target_start,
                Fixture.league_name == league,
                or_(Fixture.home_team == team, Fixture.away_team == team),
            )
            .order_by(Fixture.starts_at.desc(), Fixture.id.desc())
            .limit(HISTORY_SCAN_LIMIT_PER_TEAM)
        ).all()
        snapshots = _latest_snapshot_map(session, [int(f.id) for f in fixtures])

    rows: list[dict[str, Any]] = []
    for fixture in fixtures:
        snapshot = snapshots.get(int(fixture.id))
        if snapshot is None:
            continue
        observation = _observation(fixture, snapshot, team)
        if observation is not None:
            rows.append(observation)
        if len(rows) >= LOOKBACK_MATCHES:
            break
    return rows


def build_streaming_team_enrichment(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in items:
        target_start = _aware_utc(datetime.fromisoformat(str(item["starts_at"])))
        league = str(item.get("league") or item.get("league_name") or "")
        home_team = str(item["home_team"])
        away_team = str(item["away_team"])

        home = _team_summary(_team_rows(team=home_team, league=league, target_start=target_start))
        away = _team_summary(_team_rows(team=away_team, league=league, target_start=target_start))
        home_attack, away_attack = _relative_strength(home.get("xg"), away.get("xg"))
        home_defense, away_defense = _relative_strength(home.get("xga"), away.get("xga"), inverse=True)
        home["attack_strength"], away["attack_strength"] = home_attack, away_attack
        home["defense_strength"], away["defense_strength"] = home_defense, away_defense

        result[int(item["fixture_id"])] = {
            "team_metrics": {"home": home, "away": away},
            "facts": _facts(home_team, away_team, home, away),
            "data_quality": {
                "version": STREAM_VERSION,
                "history_strictly_before_target": True,
                "lookback_matches": LOOKBACK_MATCHES,
                "history_scan_limit_per_team": HISTORY_SCAN_LIMIT_PER_TEAM,
                "provider_calls_during_dashboard_request": False,
                "low_memory_streaming": True,
                "xg_is_informational_not_prediction_input": True,
            },
        }
    return result
