from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any

from fastapi import APIRouter
from sqlalchemy import exists, select

from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot
from app.sportmonks import SportmonksClient

SNAPSHOT_RECOVERY_VERSION = "snapshot_recovery_2026_v1"
RECOVERY_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
RECOVERY_END = datetime.combine(
    datetime(2026, 8, 23).date(),
    time.max,
    tzinfo=timezone.utc,
)
DEFAULT_RECOVERY_CONCURRENCY = 3
MAX_RECOVERY_CONCURRENCY = 6
DEFAULT_RECOVERY_LIMIT = 200

# Frozen audit baseline from historical_2026_to_0823_audit_v2.
AUDIT_BASELINE_MISSING_BY_LEAGUE = {
    "Serie A": 35,
    "Serie B": 88,
    "Copa Libertadores": 1,
    "Premier League": 9,
    "La Liga": 6,
}
AUDIT_BASELINE_MISSING_TOTAL = sum(AUDIT_BASELINE_MISSING_BY_LEAGUE.values())

router = APIRouter(prefix="/operations/recovery", tags=["operations"])


@dataclass(frozen=True)
class RecoveryCandidate:
    fixture_id: int
    sportmonks_fixture_id: int
    league: str
    starts_at: datetime


def _payload_profile(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("data") or {}
    lineups = raw.get("lineups")
    statistics = raw.get("statistics")
    xg = raw.get("xgfixture") or raw.get("xGFixture")
    lineups_count = len(lineups or [])
    statistics_count = len(statistics or [])
    xg_count = len(xg or [])
    return {
        "lineups": lineups,
        "statistics": statistics,
        "xg": xg,
        "lineups_count": lineups_count,
        "statistics_count": statistics_count,
        "xg_count": xg_count,
        "has_any_enriched_data": bool(lineups_count or statistics_count or xg_count),
        "training_core_present": bool(lineups_count and statistics_count),
    }


def _missing_candidates(*, limit: int = DEFAULT_RECOVERY_LIMIT) -> list[RecoveryCandidate]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(Fixture)
            .where(
                Fixture.starts_at.between(RECOVERY_START, RECOVERY_END),
                ~exists().where(FixtureDataSnapshot.fixture_id == Fixture.id),
            )
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

    candidates: list[RecoveryCandidate] = []
    for fixture in rows:
        canonical = canonical_league(fixture.league_name)
        if not canonical.get("target") or not canonical.get("canonical_name"):
            continue
        candidates.append(
            RecoveryCandidate(
                fixture_id=int(fixture.id),
                sportmonks_fixture_id=int(fixture.sportmonks_id),
                league=str(canonical["canonical_name"]),
                starts_at=fixture.starts_at,
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def _persist_recovered_snapshot(candidate: RecoveryCandidate, profile: dict[str, Any]) -> dict[str, Any]:
    with SessionLocal() as session:
        existing = session.scalar(
            select(FixtureDataSnapshot.id)
            .where(FixtureDataSnapshot.fixture_id == candidate.fixture_id)
            .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
            .limit(1)
        )
        if existing is not None:
            return {"status": "already_present", "snapshot_id": int(existing)}

        snapshot = FixtureDataSnapshot(
            fixture_id=candidate.fixture_id,
            source="sportmonks",
            lineups=profile.get("lineups"),
            statistics=profile.get("statistics"),
            xg=profile.get("xg"),
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return {"status": "created", "snapshot_id": int(snapshot.id)}


def current_recovery_status() -> dict[str, Any]:
    candidates = _missing_candidates(limit=DEFAULT_RECOVERY_LIMIT)
    by_league = Counter(candidate.league for candidate in candidates)
    missing = len(candidates)
    return {
        "status": "ok",
        "version": SNAPSHOT_RECOVERY_VERSION,
        "window": {
            "start": RECOVERY_START.isoformat(),
            "end": RECOVERY_END.isoformat(),
        },
        "audit_baseline": {
            "missing_total": AUDIT_BASELINE_MISSING_TOTAL,
            "missing_by_league": dict(AUDIT_BASELINE_MISSING_BY_LEAGUE),
        },
        "current": {
            "missing_total": missing,
            "missing_by_league": dict(sorted(by_league.items())),
            "recovered_since_audit": max(0, AUDIT_BASELINE_MISSING_TOTAL - missing),
        },
        "policy": {
            "target_leagues_only": True,
            "empty_upstream_payload_is_not_persisted": True,
            "partial_enriched_payload_is_persisted_as_incomplete_evidence": True,
            "lineups_and_statistics_required_for_training_core": True,
            "xg_optional_for_standard_profile": True,
        },
    }


async def recover_missing_2026_snapshots(
    *,
    concurrency: int = DEFAULT_RECOVERY_CONCURRENCY,
    limit: int = DEFAULT_RECOVERY_LIMIT,
) -> dict[str, Any]:
    if concurrency < 1 or concurrency > MAX_RECOVERY_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_RECOVERY_CONCURRENCY}")
    if limit < 1 or limit > DEFAULT_RECOVERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {DEFAULT_RECOVERY_LIMIT}")

    started_at = datetime.now(timezone.utc)
    candidates = _missing_candidates(limit=limit)
    semaphore = asyncio.Semaphore(concurrency)
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    async with SportmonksClient() as client:
        async def recover_one(candidate: RecoveryCandidate) -> dict[str, Any]:
            async with semaphore:
                try:
                    payload = await client.enriched_fixture(candidate.sportmonks_fixture_id)
                except Exception as exc:
                    return {
                        "status": "upstream_failed",
                        "error": exc.__class__.__name__,
                        "candidate": candidate,
                    }

            profile = _payload_profile(payload)
            if not profile["has_any_enriched_data"]:
                return {
                    "status": "unrecoverable_empty_payload",
                    "candidate": candidate,
                    "profile": profile,
                }

            try:
                persisted = _persist_recovered_snapshot(candidate, profile)
            except Exception as exc:
                return {
                    "status": "persistence_failed",
                    "error": exc.__class__.__name__,
                    "candidate": candidate,
                    "profile": profile,
                }

            status = "already_present"
            if persisted.get("status") == "created":
                status = (
                    "recovered_training_core"
                    if profile["training_core_present"]
                    else "recovered_incomplete"
                )
            return {
                "status": status,
                "candidate": candidate,
                "profile": profile,
                "snapshot_id": persisted.get("snapshot_id"),
            }

        results = await asyncio.gather(*(recover_one(candidate) for candidate in candidates))
        transport_audit = client.transport_audit()

    for result in results:
        candidate: RecoveryCandidate = result["candidate"]
        status = str(result.get("status") or "unknown")
        counts[status] += 1
        profile = result.get("profile") or {}
        items.append(
            {
                "sportmonks_fixture_id": candidate.sportmonks_fixture_id,
                "fixture_id": candidate.fixture_id,
                "league": candidate.league,
                "starts_at": candidate.starts_at.isoformat(),
                "status": status,
                "snapshot_id": result.get("snapshot_id"),
                "lineups_count": int(profile.get("lineups_count") or 0),
                "statistics_count": int(profile.get("statistics_count") or 0),
                "xg_count": int(profile.get("xg_count") or 0),
                "error": result.get("error"),
            }
        )

    remaining = _missing_candidates(limit=DEFAULT_RECOVERY_LIMIT)
    remaining_by_league = Counter(candidate.league for candidate in remaining)
    recovered_count = counts["recovered_training_core"] + counts["recovered_incomplete"]
    finished_at = datetime.now(timezone.utc)

    return {
        "status": "ok" if not counts["persistence_failed"] else "partial",
        "version": SNAPSHOT_RECOVERY_VERSION,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "window": {
            "start": RECOVERY_START.isoformat(),
            "end": RECOVERY_END.isoformat(),
        },
        "selected_missing": len(candidates),
        "recovered": recovered_count,
        "recovered_training_core": counts["recovered_training_core"],
        "recovered_incomplete": counts["recovered_incomplete"],
        "unrecoverable_empty_payload": counts["unrecoverable_empty_payload"],
        "upstream_failed": counts["upstream_failed"],
        "persistence_failed": counts["persistence_failed"],
        "already_present": counts["already_present"],
        "remaining_missing": len(remaining),
        "remaining_missing_by_league": dict(sorted(remaining_by_league.items())),
        "sportmonks_transport": transport_audit,
        "items": items,
        "policy": {
            "bounded_concurrency": concurrency,
            "target_leagues_only": True,
            "missing_snapshots_only": True,
            "empty_upstream_payload_is_not_persisted": True,
            "partial_enriched_payload_is_preserved_but_not_claimed_training_ready": True,
            "training_core_requires_lineups_and_statistics": True,
            "no_existing_snapshot_overwritten": True,
        },
    }


@router.get("/2026-snapshots/status")
def snapshot_recovery_status_endpoint() -> dict[str, Any]:
    return current_recovery_status()
