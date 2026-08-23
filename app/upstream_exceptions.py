from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import select

from app.data_quality import assess_fixture_quality
from app.database import SessionLocal
from app.feature_profiles import classify_feature_profile_from_assessment
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot

UPSTREAM_EXCEPTION_SOURCE = "upstream_exception_v1"
MAX_EXCEPTIONS_PER_RUN = 25


def _requested_league_keys(leagues: list[str] | None) -> set[str]:
    keys: set[str] = set()
    for league in leagues or []:
        canonical = canonical_league(league)
        if canonical.get("target") and canonical.get("key"):
            keys.add(str(canonical["key"]))
    return keys


def _latest_snapshot_for_fixture(session, fixture_id: int) -> FixtureDataSnapshot | None:
    return session.scalar(
        select(FixtureDataSnapshot)
        .where(FixtureDataSnapshot.fixture_id == fixture_id)
        .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
        .limit(1)
    )


def is_upstream_exception_fixture(fixture_id: int) -> bool:
    with SessionLocal() as session:
        snapshot = _latest_snapshot_for_fixture(session, fixture_id)
        return bool(snapshot and snapshot.source == UPSTREAM_EXCEPTION_SOURCE)


def register_upstream_exception_fixture(fixture_id: int) -> int | None:
    """Persist an idempotent upstream-unavailable marker for one DB fixture id.

    The marker copies the latest payload so quality/profile assessment remains
    unchanged while the snapshot source records that a fresh upstream retry was
    exhausted. If the latest snapshot is already a marker, return it unchanged.
    """
    with SessionLocal() as session:
        current = _latest_snapshot_for_fixture(session, fixture_id)
        if current is None:
            return None
        if current.source == UPSTREAM_EXCEPTION_SOURCE:
            return int(current.id)

        marker = FixtureDataSnapshot(
            fixture_id=fixture_id,
            source=UPSTREAM_EXCEPTION_SOURCE,
            lineups=current.lineups,
            statistics=current.statistics,
            xg=current.xg,
        )
        session.add(marker)
        session.commit()
        session.refresh(marker)
        return int(marker.id)


def count_upstream_exceptions(start_date: date, end_date: date, leagues: list[str] | None = None) -> int:
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys = _requested_league_keys(leagues)

    with SessionLocal() as session:
        fixtures = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()
        count = 0
        for fixture in fixtures:
            canonical = canonical_league(fixture.league_name)
            if requested_keys and canonical.get("key") not in requested_keys:
                continue
            snapshot = _latest_snapshot_for_fixture(session, fixture.id)
            if snapshot and snapshot.source == UPSTREAM_EXCEPTION_SOURCE:
                count += 1
        return count


def register_upstream_exceptions(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    limit: int = 10,
) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if limit < 1 or limit > MAX_EXCEPTIONS_PER_RUN:
        raise ValueError(f"limit must be between 1 and {MAX_EXCEPTIONS_PER_RUN}")

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys = _requested_league_keys(leagues)

    with SessionLocal() as session:
        candidates = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

    selected: list[Fixture] = []
    for fixture in candidates:
        canonical = canonical_league(fixture.league_name)
        if requested_keys and canonical.get("key") not in requested_keys:
            continue
        assessment = assess_fixture_quality(int(fixture.sportmonks_id))
        classification = classify_feature_profile_from_assessment(assessment)
        if classification.get("profile") != "INCOMPLETE":
            continue
        if is_upstream_exception_fixture(int(fixture.id)):
            continue
        selected.append(fixture)
        if len(selected) >= limit:
            break

    results: list[dict] = []
    registered = 0
    for fixture in selected:
        assessment = assess_fixture_quality(int(fixture.sportmonks_id))
        marker_id = register_upstream_exception_fixture(int(fixture.id))
        if marker_id is None:
            continue

        registered += 1
        results.append({
            "sportmonks_fixture_id": fixture.sportmonks_id,
            "fixture_id": fixture.id,
            "league": canonical_league(fixture.league_name).get("canonical_name") or fixture.league_name,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "status": "UPSTREAM_UNAVAILABLE",
            "snapshot_id": marker_id,
            "training_eligible": False,
            "profile": "INCOMPLETE",
            "quality_score": assessment.get("quality_score", 0),
            "blockers": assessment.get("blockers") or [],
            "warnings": assessment.get("warnings") or [],
        })

    return {
        "status": "ok",
        "version": "upstream_exception_v1",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "normalized_league_keys": sorted(requested_keys),
        "limit": limit,
        "selected_incomplete": len(selected),
        "registered_upstream_unavailable": registered,
        "results": results,
        "policy": {
            "training_eligible": False,
            "profile_remains": "INCOMPLETE",
            "exception_status": "UPSTREAM_UNAVAILABLE",
            "persistence": "latest snapshot source marker in fixture_data_snapshots",
            "healthy_snapshots_are_never_touched": True,
            "xg_absence_is_zero": False,
        },
    }
