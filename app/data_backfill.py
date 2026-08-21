from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import exists, select

from app.database import SessionLocal
from app.fixture_data_ingestion import ingest_fixture_data_payload
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot
from app.sportmonks import SportmonksClient

MAX_FIXTURES_PER_RUN = 25


def _requested_league_keys(leagues: list[str] | None) -> set[str]:
    keys: set[str] = set()
    for league in leagues or []:
        canonical = canonical_league(league)
        if canonical.get("target") and canonical.get("key"):
            keys.add(str(canonical["key"]))
    return keys


async def backfill_fixture_data(start_date: date, end_date: date, leagues: list[str] | None = None, limit: int = 10, skip_existing: bool = True) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if limit < 1 or limit > MAX_FIXTURES_PER_RUN:
        raise ValueError(f"limit must be between 1 and {MAX_FIXTURES_PER_RUN}")

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys = _requested_league_keys(leagues)

    with SessionLocal() as session:
        query = select(Fixture).where(Fixture.starts_at.between(start_dt, end_dt))
        if skip_existing:
            query = query.where(~exists().where(FixtureDataSnapshot.fixture_id == Fixture.id))
        candidates = session.scalars(query.order_by(Fixture.starts_at.asc())).all()

    fixtures: list[Fixture] = []
    for fixture in candidates:
        if requested_keys:
            fixture_key = canonical_league(fixture.league_name).get("key")
            if fixture_key not in requested_keys:
                continue
        fixtures.append(fixture)
        if len(fixtures) >= limit:
            break

    client = SportmonksClient()
    completed = failed = lineups_total = statistics_total = xg_total = 0
    results: list[dict] = []

    for fixture in fixtures:
        try:
            payload = await client.enriched_fixture(fixture.sportmonks_id)
            result = ingest_fixture_data_payload(fixture.sportmonks_id, payload)
            completed += 1
            lineups_total += int(result.get("lineups_count", 0))
            statistics_total += int(result.get("statistics_count", 0))
            xg_total += int(result.get("xg_count", 0))
            results.append({"sportmonks_fixture_id": fixture.sportmonks_id, "league": fixture.league_name, "canonical_league": canonical_league(fixture.league_name).get("canonical_name"), "status": result.get("status"), "lineups_count": result.get("lineups_count", 0), "statistics_count": result.get("statistics_count", 0), "xg_count": result.get("xg_count", 0)})
        except Exception as exc:
            failed += 1
            results.append({"sportmonks_fixture_id": fixture.sportmonks_id, "league": fixture.league_name, "canonical_league": canonical_league(fixture.league_name).get("canonical_name"), "status": "failed", "error": exc.__class__.__name__})

    return {"status": "ok" if failed == 0 else "partial", "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "leagues": leagues or [], "normalized_league_keys": sorted(requested_keys), "skip_existing": skip_existing, "limit": limit, "selected_fixtures": len(fixtures), "completed": completed, "failed": failed, "lineups_total": lineups_total, "statistics_total": statistics_total, "xg_total": xg_total, "results": results}
