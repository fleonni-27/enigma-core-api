from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from sqlalchemy import or_, select

from app.database import SessionLocal
from app.models import Fixture, FixtureDataSnapshot
from app.training_dataset import STAT_NAMES, _as_list, _stat_value, _xg_value

ENRICHMENT_VERSION = "dashboard_j1_team_enrichment_v1"
LOOKBACK_MATCHES = 10
LOOKBACK_DAYS = 730
MAX_HISTORY_FIXTURES = 800


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 3) if values else None


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
        result.setdefault(int(row.fixture_id), row)
    return result


def _observation(fixture: Fixture, snapshot: FixtureDataSnapshot, team: str) -> dict[str, Any] | None:
    statistics = _as_list(snapshot.statistics)
    xg_rows = _as_list(snapshot.xg)
    if fixture.home_team == team:
        side, opponent = "home", "away"
    elif fixture.away_team == team:
        side, opponent = "away", "home"
    else:
        return None

    goals_for = _stat_value(statistics, STAT_NAMES["goals"], side) if statistics else None
    goals_against = _stat_value(statistics, STAT_NAMES["goals"], opponent) if statistics else None
    xg_for = _xg_value(xg_rows, statistics, side)
    xg_against = _xg_value(xg_rows, statistics, opponent)
    if goals_for is None and goals_against is None and xg_for is None and xg_against is None:
        return None

    result = None
    points = None
    if goals_for is not None and goals_against is not None:
        result = "V" if float(goals_for) > float(goals_against) else "E" if float(goals_for) == float(goals_against) else "D"
        points = 3.0 if result == "V" else 1.0 if result == "E" else 0.0
    return {
        "starts_at": _aware_utc(fixture.starts_at),
        "result": result,
        "points": points,
        "goals_for": float(goals_for) if goals_for is not None else None,
        "goals_against": float(goals_against) if goals_against is not None else None,
        "xg_for": float(xg_for) if xg_for is not None else None,
        "xg_against": float(xg_against) if xg_against is not None else None,
    }


def _team_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    xg_for = [float(r["xg_for"]) for r in rows if r.get("xg_for") is not None]
    xg_against = [float(r["xg_against"]) for r in rows if r.get("xg_against") is not None]
    goals_for = [float(r["goals_for"]) for r in rows if r.get("goals_for") is not None]
    goals_against = [float(r["goals_against"]) for r in rows if r.get("goals_against") is not None]
    points = [float(r["points"]) for r in rows if r.get("points") is not None]
    form = [str(r["result"]) for r in rows if r.get("result") is not None]
    return {
        "xg": _avg(xg_for),
        "xga": _avg(xg_against),
        "goals_for_avg": _avg(goals_for),
        "goals_against_avg": _avg(goals_against),
        "form_5": form[:5],
        "form_10_ppm": _avg(points[:10]) if len(points) >= 10 else None,
        "history_matches": len(rows),
        "xg_matches": len(xg_for),
        "xga_matches": len(xg_against),
        "result_matches": len(points),
        "elo": None,
    }


def _relative_strength(a: float | None, b: float | None, *, inverse: bool = False) -> tuple[float | None, float | None]:
    if a is None or b is None:
        return None, None
    if inverse:
        a = 1.0 / max(a, 0.05)
        b = 1.0 / max(b, 0.05)
    peak = max(a, b, 0.05)
    return round(a / peak * 100.0, 1), round(b / peak * 100.0, 1)


def _facts(home_team: str, away_team: str, home: dict[str, Any], away: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    hxg, axg = home.get("xg"), away.get("xg")
    hxga, axga = home.get("xga"), away.get("xga")
    if hxg is not None and axg is not None and abs(float(hxg) - float(axg)) >= 0.25:
        leader = home_team if float(hxg) > float(axg) else away_team
        facts.append(f"{leader} chega com maior produção média de xG no histórico pré-jogo disponível.")
    if hxga is not None and axga is not None and abs(float(hxga) - float(axga)) >= 0.25:
        leader = home_team if float(hxga) < float(axga) else away_team
        facts.append(f"{leader} apresenta melhor contenção de xG cedido no histórico pré-jogo disponível.")
    for team, row in ((home_team, home), (away_team, away)):
        form = list(row.get("form_5") or [])
        if len(form) >= 5:
            wins = form.count("V")
            losses = form.count("D")
            if wins >= 4:
                facts.append(f"{team} venceu pelo menos 4 dos últimos 5 jogos com resultados persistidos.")
            elif losses >= 4:
                facts.append(f"{team} perdeu pelo menos 4 dos últimos 5 jogos com resultados persistidos.")
        xg = row.get("xg")
        goals = row.get("goals_for_avg")
        if xg is not None and goals is not None and abs(float(goals) - float(xg)) >= 0.45:
            direction = "acima" if float(goals) > float(xg) else "abaixo"
            facts.append(f"{team} vem marcando {direction} do xG médio, sinal de diferença entre produção e conversão.")
    return facts[:5]


def build_bulk_team_enrichment(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    if not items:
        return {}
    teams = sorted({str(i["home_team"]) for i in items} | {str(i["away_team"]) for i in items})
    starts = [_aware_utc(datetime.fromisoformat(str(i["starts_at"]))) for i in items]
    earliest = min(starts) - timedelta(days=LOOKBACK_DAYS)
    latest = max(starts)

    with SessionLocal() as session:
        history = session.scalars(
            select(Fixture)
            .where(
                Fixture.starts_at >= earliest,
                Fixture.starts_at < latest,
                or_(Fixture.home_team.in_(teams), Fixture.away_team.in_(teams)),
            )
            .order_by(Fixture.starts_at.desc(), Fixture.id.desc())
            .limit(MAX_HISTORY_FIXTURES)
        ).all()
        snapshot_map = _latest_snapshot_map(session, [int(f.id) for f in history])

    result: dict[int, dict[str, Any]] = {}
    for item in items:
        target_start = _aware_utc(datetime.fromisoformat(str(item["starts_at"])))
        league = str(item.get("league") or item.get("league_name") or "")
        team_rows: dict[str, list[dict[str, Any]]] = {
            str(item["home_team"]): [],
            str(item["away_team"]): [],
        }
        for fixture in history:
            if _aware_utc(fixture.starts_at) >= target_start:
                continue
            if league and str(fixture.league_name or "") != league:
                continue
            snapshot = snapshot_map.get(int(fixture.id))
            if snapshot is None:
                continue
            for team in team_rows:
                if len(team_rows[team]) >= LOOKBACK_MATCHES:
                    continue
                if fixture.home_team != team and fixture.away_team != team:
                    continue
                row = _observation(fixture, snapshot, team)
                if row is not None:
                    team_rows[team].append(row)
            if all(len(rows) >= LOOKBACK_MATCHES for rows in team_rows.values()):
                break

        home = _team_summary(team_rows[str(item["home_team"])])
        away = _team_summary(team_rows[str(item["away_team"])])
        home_attack, away_attack = _relative_strength(home["xg"], away["xg"])
        home_defense, away_defense = _relative_strength(home["xga"], away["xga"], inverse=True)
        home["attack_strength"], away["attack_strength"] = home_attack, away_attack
        home["defense_strength"], away["defense_strength"] = home_defense, away_defense
        facts = _facts(str(item["home_team"]), str(item["away_team"]), home, away)
        result[int(item["fixture_id"])] = {
            "team_metrics": {"home": home, "away": away},
            "facts": facts,
            "data_quality": {
                "version": ENRICHMENT_VERSION,
                "history_strictly_before_target": True,
                "lookback_matches": LOOKBACK_MATCHES,
                "provider_calls_during_dashboard_request": False,
                "xg_is_informational_not_prediction_input": True,
            },
        }
    return result
