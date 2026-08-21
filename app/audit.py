from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Fixture, FixtureDataSnapshot


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _canonical_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _duplicate_count(rows: list[Any]) -> int:
    if not rows:
        return 0
    counts = Counter(_canonical_key(row) for row in rows)
    return sum(count - 1 for count in counts.values() if count > 1)


def _null_leaf_count(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, dict):
        return sum(_null_leaf_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_null_leaf_count(item) for item in value)
    return 0


def _type_name(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    raw_type = row.get("type")
    if isinstance(raw_type, dict):
        return str(raw_type.get("name") or raw_type.get("developer_name") or raw_type.get("code") or raw_type.get("id") or "") or None
    if raw_type is not None:
        return str(raw_type)
    return None


def _player_identity(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    player = row.get("player")
    if isinstance(player, dict):
        value = player.get("id") or player.get("display_name") or player.get("name")
        return str(value) if value is not None else None
    player_id = row.get("player_id")
    return str(player_id) if player_id is not None else None


def _section_summary(rows: list[Any], *, section: str) -> dict:
    summary = {"records": len(rows), "duplicate_rows": _duplicate_count(rows), "null_leaf_fields": _null_leaf_count(rows)}
    if section in {"statistics", "xg"}:
        types = sorted({name for row in rows if (name := _type_name(row))})
        summary["types"] = types
        summary["types_count"] = len(types)
    if section == "lineups":
        players = {identity for row in rows if (identity := _player_identity(row))}
        summary["unique_players"] = len(players)
    return summary


def audit_fixture(sportmonks_fixture_id: int, *, include_raw: bool = False, sample_size: int = 5) -> dict:
    if sample_size < 0 or sample_size > 25:
        raise ValueError("sample_size must be between 0 and 25")

    with SessionLocal() as session:
        fixture = session.scalar(select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id))
        if fixture is None:
            return {"status": "fixture_not_found", "sportmonks_fixture_id": sportmonks_fixture_id}
        latest_snapshot = session.scalar(select(FixtureDataSnapshot).where(FixtureDataSnapshot.fixture_id == fixture.id).order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc()).limit(1))
        snapshot_count = session.scalar(select(func.count(FixtureDataSnapshot.id)).where(FixtureDataSnapshot.fixture_id == fixture.id)) or 0

    fixture_payload = {"id": fixture.id, "sportmonks_id": fixture.sportmonks_id, "league_name": fixture.league_name, "home_team": fixture.home_team, "away_team": fixture.away_team, "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None, "status": fixture.status, "created_at": fixture.created_at.isoformat() if fixture.created_at else None}

    if latest_snapshot is None:
        return {"status": "ok", "fixture": fixture_payload, "snapshot": None, "snapshot_count": int(snapshot_count), "quality": {"complete": False, "missing_sections": ["lineups", "statistics", "xg"], "note": "Fixture exists but has no data snapshot yet."}}

    lineups = _as_list(latest_snapshot.lineups)
    statistics = _as_list(latest_snapshot.statistics)
    xg = _as_list(latest_snapshot.xg)
    missing_sections = [name for name, rows in (("lineups", lineups), ("statistics", statistics), ("xg", xg)) if not rows]

    response = {
        "status": "ok",
        "fixture": fixture_payload,
        "snapshot": {"id": latest_snapshot.id, "source": latest_snapshot.source, "fetched_at": latest_snapshot.fetched_at.isoformat() if latest_snapshot.fetched_at else None, "snapshot_count": int(snapshot_count)},
        "counts": {"lineups": len(lineups), "statistics": len(statistics), "xg": len(xg)},
        "summaries": {"lineups": _section_summary(lineups, section="lineups"), "statistics": _section_summary(statistics, section="statistics"), "xg": _section_summary(xg, section="xg")},
        "samples": {"lineups": lineups[:sample_size], "statistics": statistics[:sample_size], "xg": xg[:sample_size]},
        "quality": {"complete": len(missing_sections) == 0, "missing_sections": missing_sections, "duplicate_rows": {"lineups": _duplicate_count(lineups), "statistics": _duplicate_count(statistics), "xg": _duplicate_count(xg)}, "interpretation": "Empty xg means xG data was not present in the stored Sportmonks snapshot; it must not be interpreted as xG = 0."},
    }
    if include_raw:
        response["raw"] = {"lineups": latest_snapshot.lineups, "statistics": latest_snapshot.statistics, "xg": latest_snapshot.xg}
    return response
