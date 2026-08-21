from __future__ import annotations

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Fixture, FixtureDataSnapshot


def ingest_fixture_data_payload(sportmonks_fixture_id: int, payload: dict) -> dict:
    raw = payload.get("data") or {}

    with SessionLocal() as session:
        fixture = session.scalar(
            select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id)
        )
        if fixture is None:
            return {
                "status": "fixture_not_found",
                "sportmonks_fixture_id": sportmonks_fixture_id,
                "created": 0,
            }

        lineups = raw.get("lineups")
        statistics = raw.get("statistics")
        xg = raw.get("xgfixture") or raw.get("xGFixture")

        snapshot = FixtureDataSnapshot(
            fixture_id=fixture.id,
            source="sportmonks",
            lineups=lineups,
            statistics=statistics,
            xg=xg,
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)

        return {
            "status": "ok",
            "sportmonks_fixture_id": sportmonks_fixture_id,
            "fixture_id": fixture.id,
            "snapshot_id": snapshot.id,
            "lineups_count": len(lineups or []),
            "statistics_count": len(statistics or []),
            "xg_count": len(xg or []),
        }
