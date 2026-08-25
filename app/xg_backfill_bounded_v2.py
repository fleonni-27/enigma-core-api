from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import String, and_, case, cast, func, or_, select

from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot
from app.sportmonks import SportmonksClient
from app.xg_backfill_memory_scan import _league_aliases
from app.xg_historical_backfill import (
    XG_BACKFILL_VERSION,
    XGBackfillCandidate,
    _persist_append_only_xg,
    _xg_payload,
    _xg_type_names,
)

CHUNK_SIZE = 12


def _json_text(column):
    return cast(column, String)


def _json_present(column):
    return and_(
        column.is_not(None),
        ~_json_text(column).in_(("null", "[]", "{}")),
    )


def _json_missing(column):
    return or_(
        column.is_(None),
        _json_text(column).in_(("null", "[]", "{}")),
    )


def _latest_snapshot_id_subquery():
    return (
        select(FixtureDataSnapshot.id)
        .where(FixtureDataSnapshot.fixture_id == Fixture.id)
        .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
        .limit(1)
        .correlate(Fixture)
        .scalar_subquery()
    )


def candidate_rows_sql(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None,
    limit: int,
) -> list[XGBackfillCandidate]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    aliases = _league_aliases(leagues)
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    latest_snapshot_id = _latest_snapshot_id_subquery()

    with SessionLocal() as session:
        rows = session.execute(
            select(
                Fixture.id,
                Fixture.sportmonks_id,
                Fixture.league_name,
                Fixture.starts_at,
                FixtureDataSnapshot.id.label("snapshot_id"),
            )
            .join(FixtureDataSnapshot, FixtureDataSnapshot.id == latest_snapshot_id)
            .where(
                Fixture.starts_at.between(start_dt, end_dt),
                Fixture.league_name.in_(aliases),
                _json_present(FixtureDataSnapshot.statistics),
                _json_missing(FixtureDataSnapshot.xg),
            )
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
            .limit(limit)
        ).all()

    return [
        XGBackfillCandidate(
            fixture_id=int(row.id),
            sportmonks_fixture_id=int(row.sportmonks_id),
            league=str(canonical_league(row.league_name).get("canonical_name") or row.league_name or "Unknown"),
            starts_at=row.starts_at,
            previous_snapshot_id=int(row.snapshot_id),
        )
        for row in rows
    ]


def gap_status_sql(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    aliases = _league_aliases(leagues)
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    latest_snapshot_id = _latest_snapshot_id_subquery()
    base_conditions = (
        Fixture.starts_at.between(start_dt, end_dt),
        Fixture.league_name.in_(aliases),
    )
    has_xg = _json_present(FixtureDataSnapshot.xg)
    eligible = and_(
        _json_present(FixtureDataSnapshot.statistics),
        _json_missing(FixtureDataSnapshot.xg),
    )

    with SessionLocal() as session:
        total = session.scalar(
            select(func.count(Fixture.id))
            .join(FixtureDataSnapshot, FixtureDataSnapshot.id == latest_snapshot_id)
            .where(*base_conditions)
        ) or 0
        with_xg = session.scalar(
            select(func.count(Fixture.id))
            .join(FixtureDataSnapshot, FixtureDataSnapshot.id == latest_snapshot_id)
            .where(*base_conditions, has_xg)
        ) or 0
        eligible_count = session.scalar(
            select(func.count(Fixture.id))
            .join(FixtureDataSnapshot, FixtureDataSnapshot.id == latest_snapshot_id)
            .where(*base_conditions, eligible)
        ) or 0
        league_rows = session.execute(
            select(
                Fixture.league_name,
                func.count(Fixture.id).label("snapshots"),
                func.sum(case((has_xg, 1), else_=0)).label("with_xg"),
                func.sum(case((eligible, 1), else_=0)).label("eligible_missing_xg"),
            )
            .join(FixtureDataSnapshot, FixtureDataSnapshot.id == latest_snapshot_id)
            .where(*base_conditions)
            .group_by(Fixture.league_name)
        ).all()
        sample_rows = session.execute(
            select(
                Fixture.id,
                Fixture.sportmonks_id,
                Fixture.league_name,
                Fixture.starts_at,
                FixtureDataSnapshot.id.label("snapshot_id"),
            )
            .join(FixtureDataSnapshot, FixtureDataSnapshot.id == latest_snapshot_id)
            .where(*base_conditions, eligible)
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
            .limit(25)
        ).all()

    aggregated: dict[str, Counter[str]] = defaultdict(Counter)
    for row in league_rows:
        canonical = str(canonical_league(row.league_name).get("canonical_name") or row.league_name or "Unknown")
        aggregated[canonical]["snapshots"] += int(row.snapshots or 0)
        aggregated[canonical]["with_xg"] += int(row.with_xg or 0)
        aggregated[canonical]["eligible_missing_xg"] += int(row.eligible_missing_xg or 0)

    return {
        "status": "ok",
        "version": XG_BACKFILL_VERSION,
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "leagues": leagues or [],
        "totals": {
            "fixtures_with_snapshot": int(total),
            "with_xg": int(with_xg),
            "eligible_missing_xg": int(eligible_count),
        },
        "by_league": {league: dict(sorted(counter.items())) for league, counter in sorted(aggregated.items())},
        "candidate_sample": [
            {
                "fixture_id": int(row.id),
                "sportmonks_fixture_id": int(row.sportmonks_id),
                "league": str(canonical_league(row.league_name).get("canonical_name") or row.league_name or "Unknown"),
                "starts_at": row.starts_at.isoformat(),
                "previous_snapshot_id": int(row.snapshot_id),
            }
            for row in sample_rows
        ],
        "policy": {
            "latest_snapshot_only_for_gap_detection": True,
            "statistics_required_for_evaluation_recovery": True,
            "empty_xg_is_missing_not_zero": True,
            "backfill_is_append_only": True,
            "existing_snapshots_are_never_overwritten": True,
            "json_payloads_not_materialized_during_gap_scan": True,
            "league_filter_applied_in_sql": True,
        },
    }


async def backfill_missing_xg_bounded(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    limit: int = 250,
    concurrency: int = 3,
) -> dict[str, Any]:
    if concurrency < 1 or concurrency > 6:
        raise ValueError("concurrency must be between 1 and 6")

    started_at = datetime.now(timezone.utc)
    candidates = candidate_rows_sql(
        start_date=start_date,
        end_date=end_date,
        leagues=leagues,
        limit=limit,
    )
    semaphore = asyncio.Semaphore(concurrency)
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    async with SportmonksClient() as client:
        async def recover_one(candidate: XGBackfillCandidate) -> dict[str, Any]:
            async with semaphore:
                try:
                    payload = await client.enriched_fixture(candidate.sportmonks_fixture_id)
                except Exception as exc:
                    return {"candidate": candidate, "status": "upstream_failed", "error": exc.__class__.__name__}

            xg_rows = _xg_payload(payload)
            if not xg_rows:
                return {"candidate": candidate, "status": "upstream_xg_unavailable", "xg_count": 0}

            try:
                persisted = _persist_append_only_xg(candidate, xg_rows)
            except Exception as exc:
                return {
                    "candidate": candidate,
                    "status": "persistence_failed",
                    "error": exc.__class__.__name__,
                    "xg_count": len(xg_rows),
                    "xg_type_names": _xg_type_names(xg_rows),
                }
            return {
                "candidate": candidate,
                "status": str(persisted.get("status") or "unknown"),
                "previous_snapshot_id": persisted.get("previous_snapshot_id"),
                "snapshot_id": persisted.get("snapshot_id"),
                "xg_count": len(xg_rows),
                "xg_type_names": _xg_type_names(xg_rows),
            }

        for offset in range(0, len(candidates), CHUNK_SIZE):
            chunk = candidates[offset : offset + CHUNK_SIZE]
            results = await asyncio.gather(*(recover_one(candidate) for candidate in chunk))
            for result in results:
                candidate: XGBackfillCandidate = result["candidate"]
                status = str(result.get("status") or "unknown")
                counts[status] += 1
                items.append(
                    {
                        "fixture_id": candidate.fixture_id,
                        "sportmonks_fixture_id": candidate.sportmonks_fixture_id,
                        "league": candidate.league,
                        "starts_at": candidate.starts_at.isoformat(),
                        "status": status,
                        "previous_snapshot_id": result.get("previous_snapshot_id") or candidate.previous_snapshot_id,
                        "snapshot_id": result.get("snapshot_id"),
                        "xg_count": int(result.get("xg_count") or 0),
                        "xg_type_names": result.get("xg_type_names") or [],
                        "error": result.get("error"),
                    }
                )
            del results
        transport_audit = client.transport_audit()

    after = gap_status_sql(start_date=start_date, end_date=end_date, leagues=leagues)
    finished_at = datetime.now(timezone.utc)
    return {
        "status": "partial" if counts["persistence_failed"] else "ok",
        "version": XG_BACKFILL_VERSION,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "leagues": leagues or [],
        "selected": len(candidates),
        "created": counts["created"],
        "upstream_xg_unavailable": counts["upstream_xg_unavailable"],
        "upstream_failed": counts["upstream_failed"],
        "persistence_failed": counts["persistence_failed"],
        "already_has_xg": counts["already_has_xg"],
        "remaining_eligible_missing_xg": int((after.get("totals") or {}).get("eligible_missing_xg") or 0),
        "sportmonks_transport": transport_audit,
        "items": items,
        "policy": {
            "append_only": True,
            "existing_snapshots_never_overwritten": True,
            "empty_upstream_xg_not_persisted": True,
            "xg_missing_never_imputed_as_zero": True,
            "bounded_concurrency": concurrency,
            "bounded_task_chunk_size": CHUNK_SIZE,
            "candidate_json_not_materialized": True,
            "idempotent_against_latest_snapshot": True,
        },
    }
