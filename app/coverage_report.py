from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.league_registry import canonical_league, registry_definition
from app.models import Fixture, FixtureDataSnapshot


def _has_payload(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, tuple, set, str)):
        return len(value) > 0
    return True


def build_league_registry() -> dict:
    with SessionLocal() as session:
        rows = session.execute(select(Fixture.league_name).distinct()).all()

    observed: dict[str, dict] = {}
    unmapped: list[dict] = []
    for (league_name,) in rows:
        canonical = canonical_league(league_name)
        if canonical["target"]:
            key = canonical["key"]
            item = observed.setdefault(key, {"key": key, "canonical_name": canonical["canonical_name"], "priority": canonical["priority"], "observed_names": []})
            if league_name and league_name not in item["observed_names"]:
                item["observed_names"].append(league_name)
        else:
            unmapped.append({"name": league_name or "Unknown"})

    defined = []
    for definition in registry_definition():
        key = definition["key"]
        defined.append({**definition, "observed_names": sorted(observed.get(key, {}).get("observed_names", [])), "observed": key in observed})

    return {"status": "ok", "target_leagues": defined, "unmapped_observed_leagues": sorted(unmapped, key=lambda x: str(x["name"])), "note": "Sportmonks league IDs will be added in the next schema migration; canonical matching already uses aliases safely."}


def build_data_coverage_report(start_date: date, end_date: date) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)

    with SessionLocal() as session:
        fixtures = session.scalars(select(Fixture).where(Fixture.starts_at.between(start_dt, end_dt)).order_by(Fixture.starts_at.asc())).all()
        fixture_ids = [fixture.id for fixture in fixtures]
        snapshots = []
        if fixture_ids:
            snapshots = session.scalars(select(FixtureDataSnapshot).where(FixtureDataSnapshot.fixture_id.in_(fixture_ids)).order_by(FixtureDataSnapshot.fixture_id.asc(), FixtureDataSnapshot.fetched_at.desc())).all()

    latest_by_fixture: dict[int, FixtureDataSnapshot] = {}
    for snapshot in snapshots:
        latest_by_fixture.setdefault(snapshot.fixture_id, snapshot)

    buckets: dict[str, dict] = defaultdict(lambda: {"fixtures": 0, "snapshots": 0, "with_lineups": 0, "with_statistics": 0, "with_xg": 0, "observed_names": set(), "target": False, "priority": None, "canonical_name": None})
    for fixture in fixtures:
        canonical = canonical_league(fixture.league_name)
        key = canonical["key"] or f"unmapped:{fixture.league_name or 'Unknown'}"
        bucket = buckets[key]
        bucket["fixtures"] += 1
        bucket["target"] = bool(canonical["target"])
        bucket["priority"] = canonical["priority"]
        bucket["canonical_name"] = canonical["canonical_name"]
        if fixture.league_name:
            bucket["observed_names"].add(fixture.league_name)
        snapshot = latest_by_fixture.get(fixture.id)
        if snapshot is None:
            continue
        bucket["snapshots"] += 1
        if _has_payload(snapshot.lineups): bucket["with_lineups"] += 1
        if _has_payload(snapshot.statistics): bucket["with_statistics"] += 1
        if _has_payload(snapshot.xg): bucket["with_xg"] += 1

    report = []
    for key, bucket in buckets.items():
        fixtures_count = bucket["fixtures"]
        report.append({"key": None if key.startswith("unmapped:") else key, "league": bucket["canonical_name"], "target": bucket["target"], "priority": bucket["priority"], "observed_names": sorted(bucket["observed_names"]), "fixtures": fixtures_count, "snapshots": bucket["snapshots"], "lineups_fixtures": bucket["with_lineups"], "statistics_fixtures": bucket["with_statistics"], "xg_fixtures": bucket["with_xg"], "lineups_pct": round(bucket["with_lineups"] / fixtures_count * 100, 1) if fixtures_count else 0.0, "statistics_pct": round(bucket["with_statistics"] / fixtures_count * 100, 1) if fixtures_count else 0.0, "xg_pct": round(bucket["with_xg"] / fixtures_count * 100, 1) if fixtures_count else 0.0})
    report.sort(key=lambda row: (not row["target"], row["priority"] or 999, row["league"] or ""))
    return {"status": "ok", "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "total_fixtures": len(fixtures), "leagues": report}
