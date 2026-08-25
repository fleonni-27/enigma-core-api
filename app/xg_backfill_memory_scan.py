from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.league_registry import TARGET_LEAGUES, canonical_league
from app.models import Fixture, FixtureDataSnapshot


def _league_aliases(leagues: list[str] | None) -> list[str]:
    keys: set[str] = set()
    for league in leagues or []:
        canonical = canonical_league(league)
        if canonical.get("target") and canonical.get("key"):
            keys.add(str(canonical["key"]))
    if not keys:
        keys = set(TARGET_LEAGUES)

    aliases: set[str] = set()
    for key in keys:
        definition = TARGET_LEAGUES[key]
        aliases.add(str(definition["canonical_name"]))
        aliases.update(str(alias) for alias in definition.get("aliases") or [])
    return sorted(aliases)


def latest_snapshot_rows_memory_bounded(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None,
) -> list[tuple[Fixture, FixtureDataSnapshot]]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    aliases = _league_aliases(leagues)
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)

    latest_snapshot_id = (
        select(FixtureDataSnapshot.id)
        .where(FixtureDataSnapshot.fixture_id == Fixture.id)
        .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
        .limit(1)
        .correlate(Fixture)
        .scalar_subquery()
    )

    with SessionLocal() as session:
        rows = session.execute(
            select(Fixture, FixtureDataSnapshot)
            .join(FixtureDataSnapshot, FixtureDataSnapshot.id == latest_snapshot_id)
            .where(
                Fixture.starts_at.between(start_dt, end_dt),
                Fixture.league_name.in_(aliases),
            )
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

    return [(fixture, snapshot) for fixture, snapshot in rows]
