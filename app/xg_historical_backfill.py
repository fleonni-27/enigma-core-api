from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot
from app.sportmonks import SportmonksClient

XG_BACKFILL_VERSION = "xg_historical_backfill_v1"
DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY = 6
DEFAULT_LIMIT = 250
MAX_LIMIT = 1000

router = APIRouter(prefix="/operations/xg-backfill", tags=["operations"])


@dataclass(frozen=True)
class XGBackfillCandidate:
    fixture_id: int
    sportmonks_fixture_id: int
    league: str
    starts_at: datetime
    previous_snapshot_id: int


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _requested_league_keys(leagues: list[str] | None) -> set[str]:
    keys: set[str] = set()
    for league in leagues or []:
        canonical = canonical_league(league)
        if canonical.get("target") and canonical.get("key"):
            keys.add(str(canonical["key"]))
    return keys


def _xg_payload(payload: dict[str, Any]) -> list[Any]:
    raw = payload.get("data") or {}
    return _as_list(raw.get("xgfixture") or raw.get("xGFixture"))


def _xg_type_names(rows: list[Any]) -> list[str]:
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_type = row.get("type")
        value = None
        if isinstance(raw_type, dict):
            value = raw_type.get("name") or raw_type.get("developer_name") or raw_type.get("code") or raw_type.get("id")
        elif raw_type is not None:
            value = raw_type
        if value is None:
            value = row.get("type_id")
        if value is not None:
            names.add(str(value))
    return sorted(names)


def _latest_snapshot_rows(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None,
) -> list[tuple[Fixture, FixtureDataSnapshot]]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    requested_keys = _requested_league_keys(leagues)
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)

    with SessionLocal() as session:
        fixtures = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()
        fixture_ids = [int(fixture.id) for fixture in fixtures]
        if not fixture_ids:
            return []
        snapshots = session.scalars(
            select(FixtureDataSnapshot)
            .where(FixtureDataSnapshot.fixture_id.in_(fixture_ids))
            .order_by(
                FixtureDataSnapshot.fixture_id.asc(),
                FixtureDataSnapshot.fetched_at.desc(),
                FixtureDataSnapshot.id.desc(),
            )
        ).all()

    latest_by_fixture: dict[int, FixtureDataSnapshot] = {}
    for snapshot in snapshots:
        latest_by_fixture.setdefault(int(snapshot.fixture_id), snapshot)

    rows: list[tuple[Fixture, FixtureDataSnapshot]] = []
    for fixture in fixtures:
        canonical = canonical_league(fixture.league_name)
        if not canonical.get("target"):
            continue
        if requested_keys and canonical.get("key") not in requested_keys:
            continue
        snapshot = latest_by_fixture.get(int(fixture.id))
        if snapshot is None:
            continue
        rows.append((fixture, snapshot))
    return rows


def xg_gap_status(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
) -> dict[str, Any]:
    rows = _latest_snapshot_rows(start_date=start_date, end_date=end_date, leagues=leagues)
    by_league: dict[str, Counter[str]] = {}
    candidates: list[dict[str, Any]] = []

    for fixture, snapshot in rows:
        canonical_name = str(canonical_league(fixture.league_name).get("canonical_name") or fixture.league_name or "Unknown")
        counts = by_league.setdefault(canonical_name, Counter())
        has_statistics = bool(_as_list(snapshot.statistics))
        has_xg = bool(_as_list(snapshot.xg))
        counts["snapshots"] += 1
        counts["with_statistics"] += int(has_statistics)
        counts["with_xg"] += int(has_xg)
        if has_statistics and not has_xg:
            counts["eligible_missing_xg"] += 1
            candidates.append(
                {
                    "fixture_id": int(fixture.id),
                    "sportmonks_fixture_id": int(fixture.sportmonks_id),
                    "league": canonical_name,
                    "starts_at": fixture.starts_at.isoformat(),
                    "previous_snapshot_id": int(snapshot.id),
                }
            )

    return {
        "status": "ok",
        "version": XG_BACKFILL_VERSION,
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "leagues": leagues or [],
        "totals": {
            "fixtures_with_snapshot": len(rows),
            "with_xg": sum(1 for _, snapshot in rows if _as_list(snapshot.xg)),
            "eligible_missing_xg": len(candidates),
        },
        "by_league": {league: dict(sorted(counter.items())) for league, counter in sorted(by_league.items())},
        "candidate_sample": candidates[:25],
        "policy": {
            "latest_snapshot_only_for_gap_detection": True,
            "statistics_required_for_evaluation_recovery": True,
            "empty_xg_is_missing_not_zero": True,
            "backfill_is_append_only": True,
            "existing_snapshots_are_never_overwritten": True,
        },
    }


def _candidate_rows(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None,
    limit: int,
) -> list[XGBackfillCandidate]:
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    rows = _latest_snapshot_rows(start_date=start_date, end_date=end_date, leagues=leagues)
    candidates: list[XGBackfillCandidate] = []
    for fixture, snapshot in rows:
        if not _as_list(snapshot.statistics) or _as_list(snapshot.xg):
            continue
        canonical_name = str(canonical_league(fixture.league_name).get("canonical_name") or fixture.league_name or "Unknown")
        candidates.append(
            XGBackfillCandidate(
                fixture_id=int(fixture.id),
                sportmonks_fixture_id=int(fixture.sportmonks_id),
                league=canonical_name,
                starts_at=fixture.starts_at,
                previous_snapshot_id=int(snapshot.id),
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def _persist_append_only_xg(candidate: XGBackfillCandidate, xg_rows: list[Any]) -> dict[str, Any]:
    with SessionLocal() as session:
        latest = session.scalar(
            select(FixtureDataSnapshot)
            .where(FixtureDataSnapshot.fixture_id == candidate.fixture_id)
            .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
            .limit(1)
        )
        if latest is None:
            return {"status": "snapshot_disappeared"}
        if _as_list(latest.xg):
            return {"status": "already_has_xg", "snapshot_id": int(latest.id)}
        if not _as_list(latest.statistics):
            return {"status": "statistics_missing", "snapshot_id": int(latest.id)}

        snapshot = FixtureDataSnapshot(
            fixture_id=candidate.fixture_id,
            source="sportmonks_xg_backfill_v1",
            lineups=latest.lineups,
            statistics=latest.statistics,
            xg=xg_rows,
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return {
            "status": "created",
            "previous_snapshot_id": int(latest.id),
            "snapshot_id": int(snapshot.id),
        }


async def backfill_missing_xg(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, Any]:
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")

    started_at = datetime.now(timezone.utc)
    candidates = _candidate_rows(
        start_date=start_date,
        end_date=end_date,
        leagues=leagues,
        limit=limit,
    )
    semaphore = asyncio.Semaphore(concurrency)

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

        results = await asyncio.gather(*(recover_one(candidate) for candidate in candidates))
        transport_audit = client.transport_audit()

    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
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

    finished_at = datetime.now(timezone.utc)
    after = xg_gap_status(start_date=start_date, end_date=end_date, leagues=leagues)
    created = counts["created"]
    return {
        "status": "partial" if counts["persistence_failed"] else "ok",
        "version": XG_BACKFILL_VERSION,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "leagues": leagues or [],
        "selected": len(candidates),
        "created": created,
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
            "previous_lineups_and_statistics_copied_exactly": True,
            "empty_upstream_xg_not_persisted": True,
            "xg_missing_never_imputed_as_zero": True,
            "bounded_concurrency": concurrency,
            "idempotent_against_latest_snapshot": True,
        },
    }


@router.get("/status")
def xg_backfill_status_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return xg_gap_status(start_date=start_date, end_date=end_date, leagues=leagues)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run")
async def xg_backfill_run_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    concurrency: int = Query(default=DEFAULT_CONCURRENCY, ge=1, le=MAX_CONCURRENCY),
) -> dict[str, Any]:
    try:
        return await backfill_missing_xg(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            limit=limit,
            concurrency=concurrency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
