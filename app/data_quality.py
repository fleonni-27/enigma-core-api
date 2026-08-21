from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Fixture, FixtureDataSnapshot


CRITICAL_STAT_TYPES = {
    "Goals",
    "Shots Total",
    "Shots On Target",
    "Ball Possession %",
    "Corners",
    "Passes",
    "Successful Passes",
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


def _row_key(row: Any) -> tuple:
    if not isinstance(row, dict):
        return (str(row),)
    return (
        row.get("id"), row.get("fixture_id"), row.get("participant_id"),
        row.get("team_id"), row.get("player_id"), row.get("type_id"), row.get("location"),
    )


def _duplicate_rows(rows: list[Any]) -> int:
    counts = Counter(_row_key(row) for row in rows)
    return sum(count - 1 for count in counts.values() if count > 1)


def _ids(rows: list[Any], key: str) -> set[int]:
    result: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(key)
        if isinstance(value, int):
            result.add(value)
        elif isinstance(value, str) and value.isdigit():
            result.add(int(value))
    return result


def _locations(rows: list[Any]) -> set[str]:
    return {
        str(row.get("location")).lower()
        for row in rows
        if isinstance(row, dict) and row.get("location") is not None
    }


def _critical_nulls(rows: list[Any], keys: tuple[str, ...]) -> int:
    missing = 0
    for row in rows:
        if not isinstance(row, dict):
            missing += len(keys)
            continue
        missing += sum(1 for key in keys if row.get(key) is None)
    return missing


def _fixture_mismatch_count(rows: list[Any], sportmonks_fixture_id: int) -> int:
    mismatches = 0
    for row in rows:
        if not isinstance(row, dict):
            mismatches += 1
            continue
        value = row.get("fixture_id")
        if value is not None and int(value) != int(sportmonks_fixture_id):
            mismatches += 1
    return mismatches


def _stat_coverage(statistics: list[Any]) -> dict:
    by_type: dict[str, set[int]] = {}
    for row in statistics:
        name = _type_name(row)
        if not name:
            continue
        participant_id = row.get("participant_id") if isinstance(row, dict) else None
        bucket = by_type.setdefault(name, set())
        if participant_id is not None:
            try:
                bucket.add(int(participant_id))
            except (TypeError, ValueError):
                pass
    present = sorted(name for name in CRITICAL_STAT_TYPES if name in by_type)
    missing = sorted(CRITICAL_STAT_TYPES - set(present))
    incomplete_two_sided = sorted(name for name in present if len(by_type.get(name, set())) < 2)
    return {
        "required": sorted(CRITICAL_STAT_TYPES),
        "present": present,
        "missing": missing,
        "incomplete_two_sided": incomplete_two_sided,
    }


def _xg_coverage(xg: list[Any]) -> dict:
    types = {_type_name(row) for row in xg}
    types.discard(None)
    expected_goal_rows = [row for row in xg if _type_name(row) == EXPECTED_XG_TYPE]
    participants = _ids(expected_goal_rows, "participant_id")
    return {
        "available": bool(xg),
        "types": sorted(str(value) for value in types),
        "expected_goals_present": EXPECTED_XG_TYPE in types,
        "expected_goals_two_sided": len(participants) == 2,
    }


def _add_check(checks: list[dict], *, name: str, passed: bool, severity: str, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "severity": severity, "detail": detail})


def assess_fixture_quality(sportmonks_fixture_id: int) -> dict:
    with SessionLocal() as session:
        fixture = session.scalar(select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id))
        if fixture is None:
            return {"status": "fixture_not_found", "sportmonks_fixture_id": sportmonks_fixture_id}
        snapshot = session.scalar(
            select(FixtureDataSnapshot)
            .where(FixtureDataSnapshot.fixture_id == fixture.id)
            .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
            .limit(1)
        )

    fixture_payload = {
        "id": fixture.id,
        "sportmonks_id": fixture.sportmonks_id,
        "league_name": fixture.league_name,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
        "status": fixture.status,
    }

    if snapshot is None:
        return {
            "status": "ok", "fixture": fixture_payload, "quality_score": 0.0,
            "decision": "rejected", "approved_for_training": False,
            "blockers": ["missing_snapshot"], "warnings": [], "checks": [],
            "note": "Fixture exists but no enriched data snapshot is stored.",
        }

    lineups = _as_list(snapshot.lineups)
    statistics = _as_list(snapshot.statistics)
    xg = _as_list(snapshot.xg)
    score = 100.0
    blockers: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []

    if not lineups:
        score -= 25
        blockers.append("missing_lineups")
    if not statistics:
        score -= 35
        blockers.append("missing_statistics")
    if not xg:
        score -= 5
        warnings.append("xg_unavailable")

    _add_check(checks, name="lineups_present", passed=bool(lineups), severity="blocker", detail=f"{len(lineups)} lineup records")
    _add_check(checks, name="statistics_present", passed=bool(statistics), severity="blocker", detail=f"{len(statistics)} statistic records")
    _add_check(checks, name="xg_present", passed=bool(xg), severity="warning", detail=f"{len(xg)} xG records; absence means unavailable data, not xG=0")

    duplicates = {"lineups": _duplicate_rows(lineups), "statistics": _duplicate_rows(statistics), "xg": _duplicate_rows(xg)}
    duplicate_total = sum(duplicates.values())
    if duplicate_total:
        score -= min(15, duplicate_total * 2)
        warnings.append("duplicate_rows_detected")
    _add_check(checks, name="no_duplicate_rows", passed=duplicate_total == 0, severity="warning", detail=str(duplicates))

    mismatches = {
        "lineups": _fixture_mismatch_count(lineups, sportmonks_fixture_id),
        "statistics": _fixture_mismatch_count(statistics, sportmonks_fixture_id),
        "xg": _fixture_mismatch_count(xg, sportmonks_fixture_id),
    }
    mismatch_total = sum(mismatches.values())
    if mismatch_total:
        score -= 25
        blockers.append("fixture_id_mismatch")
    _add_check(checks, name="fixture_id_consistency", passed=mismatch_total == 0, severity="blocker", detail=str(mismatches))

    lineup_critical_nulls = _critical_nulls(lineups, ("fixture_id", "team_id", "player_id"))
    stats_critical_nulls = _critical_nulls(statistics, ("fixture_id", "participant_id", "type_id"))
    xg_critical_nulls = _critical_nulls(xg, ("fixture_id", "participant_id", "type_id")) if xg else 0
    critical_nulls = lineup_critical_nulls + stats_critical_nulls + xg_critical_nulls
    if critical_nulls:
        score -= min(15, critical_nulls)
        warnings.append("critical_null_fields")
    _add_check(
        checks, name="critical_identity_fields", passed=critical_nulls == 0, severity="warning",
        detail=f"lineups={lineup_critical_nulls}, statistics={stats_critical_nulls}, xg={xg_critical_nulls}",
    )

    lineup_team_ids = _ids(lineups, "team_id")
    lineup_player_ids = _ids(lineups, "player_id")
    lineup_player_rows = [row for row in lineups if isinstance(row, dict) and row.get("player_id") is not None]
    player_id_counts = Counter(int(row["player_id"]) for row in lineup_player_rows)
    repeated_players = sorted(pid for pid, count in player_id_counts.items() if count > 1)

    if lineups and len(lineup_team_ids) != 2:
        score -= 20
        blockers.append("unexpected_lineup_team_count")
    if lineups and len(lineup_player_ids) < 22:
        score -= 10
        warnings.append("insufficient_unique_players")
    if repeated_players:
        score -= min(10, len(repeated_players) * 2)
        warnings.append("player_duplicate_across_lineups")

    _add_check(checks, name="two_lineup_teams", passed=(not lineups) or len(lineup_team_ids) == 2, severity="blocker", detail=f"team_ids={sorted(lineup_team_ids)}")
    _add_check(checks, name="minimum_unique_players", passed=(not lineups) or len(lineup_player_ids) >= 22, severity="warning", detail=f"unique_players={len(lineup_player_ids)}")
    _add_check(checks, name="player_uniqueness", passed=not repeated_players, severity="warning", detail=f"repeated_player_ids={repeated_players[:20]}")

    stat_participant_ids = _ids(statistics, "participant_id")
    stat_locations = _locations(statistics)
    if statistics and len(stat_participant_ids) != 2:
        score -= 20
        blockers.append("unexpected_statistics_participant_count")
    if statistics and stat_locations != {"home", "away"}:
        score -= 10
        warnings.append("statistics_location_incomplete")
    _add_check(checks, name="two_statistics_participants", passed=(not statistics) or len(stat_participant_ids) == 2, severity="blocker", detail=f"participant_ids={sorted(stat_participant_ids)}")
    _add_check(checks, name="home_away_statistics_locations", passed=(not statistics) or stat_locations == {"home", "away"}, severity="warning", detail=f"locations={sorted(stat_locations)}")

    cross_ids_match = bool(lineup_team_ids) and bool(stat_participant_ids) and lineup_team_ids == stat_participant_ids
    if lineups and statistics and not cross_ids_match:
        score -= 20
        blockers.append("lineup_statistics_team_mismatch")
    _add_check(
        checks, name="lineup_statistics_team_consistency", passed=(not lineups or not statistics) or cross_ids_match,
        severity="blocker", detail=f"lineup_team_ids={sorted(lineup_team_ids)}, statistics_participant_ids={sorted(stat_participant_ids)}",
    )

    stat_coverage = _stat_coverage(statistics)
    if stat_coverage["missing"]:
        score -= min(14, len(stat_coverage["missing"]) * 2)
        warnings.append("critical_statistics_missing")
    if stat_coverage["incomplete_two_sided"]:
        score -= min(10, len(stat_coverage["incomplete_two_sided"]) * 2)
        warnings.append("critical_statistics_one_sided")
    _add_check(
        checks, name="critical_statistics_coverage",
        passed=not stat_coverage["missing"] and not stat_coverage["incomplete_two_sided"], severity="warning",
        detail=f"missing={stat_coverage['missing']}, incomplete_two_sided={stat_coverage['incomplete_two_sided']}",
    )

    xg_coverage = _xg_coverage(xg)
    xg_participant_ids = _ids(xg, "participant_id")
    if xg:
        if not xg_coverage["expected_goals_present"]:
            score -= 10
            warnings.append("expected_goals_metric_missing")
        if xg_coverage["expected_goals_present"] and not xg_coverage["expected_goals_two_sided"]:
            score -= 10
            warnings.append("expected_goals_one_sided")
        if stat_participant_ids and xg_participant_ids and xg_participant_ids != stat_participant_ids:
            score -= 10
            warnings.append("xg_statistics_participant_mismatch")
    _add_check(
        checks, name="xg_internal_consistency",
        passed=(not xg or (xg_coverage["expected_goals_present"] and xg_coverage["expected_goals_two_sided"] and (not stat_participant_ids or xg_participant_ids == stat_participant_ids))),
        severity="warning",
        detail=f"available={xg_coverage['available']}, expected_goals_present={xg_coverage['expected_goals_present']}, expected_goals_two_sided={xg_coverage['expected_goals_two_sided']}, participant_ids={sorted(xg_participant_ids)}",
    )

    score = max(0.0, min(100.0, round(score, 1)))
    blockers = sorted(set(blockers))
    warnings = sorted(set(warnings))
    if blockers or score < 70:
        decision = "rejected"
    elif score < 85 or warnings:
        decision = "warning"
    else:
        decision = "approved_for_training"
    approved_for_training = not blockers and score >= 85

    return {
        "status": "ok",
        "fixture": fixture_payload,
        "snapshot": {"id": snapshot.id, "source": snapshot.source, "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None},
        "quality_score": score,
        "decision": decision,
        "approved_for_training": approved_for_training,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "coverage": {
            "lineups": {"records": len(lineups), "team_ids": sorted(lineup_team_ids), "unique_players": len(lineup_player_ids)},
            "statistics": {"records": len(statistics), "participant_ids": sorted(stat_participant_ids), "locations": sorted(stat_locations), "critical": stat_coverage},
            "xg": {"records": len(xg), "participant_ids": sorted(xg_participant_ids), **xg_coverage},
        },
        "duplicates": duplicates,
        "critical_nulls": {"lineups": lineup_critical_nulls, "statistics": stats_critical_nulls, "xg": xg_critical_nulls},
        "policy": {
            "approved_threshold": 85,
            "warning_threshold": 70,
            "xg_absence_is_zero": False,
            "note": "The gate evaluates structural integrity and completeness of the stored snapshot. It does not independently verify real-world player transfers or roster truth; that requires an external participant/roster validation layer.",
        },
    }
