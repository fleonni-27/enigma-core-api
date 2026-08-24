from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, time, timezone
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.database import SessionLocal
from app.ingestion import ingest_fixtures_payload
from app.league_registry import canonical_league
from app.models import Fixture, OddsSnapshot, Prediction
from app.odds_ingestion import ingest_prematch_odds_payload
from app.sportmonks import SportmonksClient

DAILY_OPERATIONS_VERSION = "daily_operations_v1"
BUSINESS_TIMEZONE = "America/Sao_Paulo"
MAX_DAILY_TARGET_FIXTURES = 200
DEFAULT_DAILY_ODDS_CONCURRENCY = 5

router = APIRouter(prefix="/operations", tags=["operations"])


def _business_today() -> date:
    return datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()


def _utc_bounds(target_date: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(BUSINESS_TIMEZONE)
    local_start = datetime.combine(target_date, time.min, tzinfo=tz)
    local_end = datetime.combine(target_date, time.max, tzinfo=tz)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _fixture_payload(fixture: Fixture) -> dict:
    canonical = canonical_league(fixture.league_name)
    return {
        "fixture_id": int(fixture.id),
        "sportmonks_fixture_id": int(fixture.sportmonks_id),
        "league": canonical.get("canonical_name") or fixture.league_name,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
        "status": fixture.status,
    }


def _target_fixtures_for_date(target_date: date) -> list[Fixture]:
    start_dt, end_dt = _utc_bounds(target_date)
    with SessionLocal() as session:
        fixtures = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

    target: list[Fixture] = []
    for fixture in fixtures:
        canonical = canonical_league(fixture.league_name)
        if canonical.get("target") and canonical.get("key"):
            target.append(fixture)
    return target


async def _fetch_daily_odds_payloads(
    client: SportmonksClient,
    fixture_ids: list[int],
    *,
    concurrency: int = DEFAULT_DAILY_ODDS_CONCURRENCY,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    if concurrency < 1 or concurrency > 12:
        raise ValueError("concurrency must be between 1 and 12")

    semaphore = asyncio.Semaphore(concurrency)
    started = perf_counter()

    async def fetch_one(fixture_id: int) -> tuple[int, dict[str, Any]]:
        request_started = perf_counter()
        async with semaphore:
            try:
                payload = await client.prematch_odds_by_fixture(fixture_id)
                return fixture_id, {
                    "status": "ok",
                    "payload": payload,
                    "fetch_seconds": round(perf_counter() - request_started, 6),
                }
            except Exception as exc:
                return fixture_id, {
                    "status": "upstream_failed",
                    "error": exc.__class__.__name__,
                    "fetch_seconds": round(perf_counter() - request_started, 6),
                }

    pairs = await asyncio.gather(*(fetch_one(fixture_id) for fixture_id in fixture_ids))
    return dict(pairs), {
        "concurrency": concurrency,
        "requested_fixtures": len(fixture_ids),
        "fetch_seconds": round(perf_counter() - started, 6),
    }


async def run_daily_sync(*, target_date: date | None = None, refresh_odds: bool = True) -> dict:
    effective_date = target_date or _business_today()
    cycle_started = perf_counter()
    transport_audit: dict[str, Any] = {}
    odds_fetch_audit: dict[str, Any] = {
        "concurrency": DEFAULT_DAILY_ODDS_CONCURRENCY,
        "requested_fixtures": 0,
        "fetch_seconds": 0.0,
    }

    async with SportmonksClient() as client:
        fixture_payload = await client.fixtures_by_date(effective_date)
        fixture_ingestion = ingest_fixtures_payload(fixture_payload)

        fixtures = _target_fixtures_for_date(effective_date)
        if len(fixtures) > MAX_DAILY_TARGET_FIXTURES:
            raise ValueError(
                f"target fixture count {len(fixtures)} exceeds safety limit {MAX_DAILY_TARGET_FIXTURES}"
            )

        snapshot_window = f"daily_{effective_date.strftime('%Y%m%d')}"
        odds_results: list[dict] = []
        counts: Counter[str] = Counter()

        if refresh_odds:
            fixture_ids = [int(fixture.sportmonks_id) for fixture in fixtures]
            fetched, odds_fetch_audit = await _fetch_daily_odds_payloads(
                client,
                fixture_ids,
                concurrency=DEFAULT_DAILY_ODDS_CONCURRENCY,
            )

            # Network I/O is concurrent; database persistence remains sequential
            # to keep ingestion transactions simple and deterministic.
            for sportmonks_fixture_id in fixture_ids:
                fetched_item = fetched[sportmonks_fixture_id]
                if fetched_item.get("status") != "ok":
                    counts["odds_fixture_failed"] += 1
                    odds_results.append(
                        {
                            "sportmonks_fixture_id": sportmonks_fixture_id,
                            "status": "upstream_failed",
                            "error": fetched_item.get("error"),
                            "fetch_seconds": fetched_item.get("fetch_seconds"),
                        }
                    )
                    continue

                try:
                    result = ingest_prematch_odds_payload(
                        sportmonks_fixture_id=sportmonks_fixture_id,
                        payload=fetched_item.get("payload") or {},
                        snapshot_window=snapshot_window,
                    )
                    created = int(result.get("created") or 0)
                    initial_states = int(result.get("initial_states_created") or 0)
                    movements = int(result.get("movements_created") or 0)
                    deduplicated = int(result.get("deduplicated_unchanged") or 0)
                    counts["odds_fixture_ok"] += 1
                    counts["odds_rows_created"] += created
                    counts["odds_initial_states_created"] += initial_states
                    counts["odds_movements_created"] += movements
                    counts["odds_unchanged_deduplicated"] += deduplicated
                    counts["odds_storage_rows_avoided"] += int(
                        result.get("storage_rows_avoided") or deduplicated
                    )
                    odds_results.append(
                        {
                            "sportmonks_fixture_id": sportmonks_fixture_id,
                            "status": result.get("status"),
                            "received": result.get("received", 0),
                            "created": created,
                            "initial_states_created": initial_states,
                            "movements_created": movements,
                            "deduplicated_unchanged": deduplicated,
                            "storage_rows_avoided": int(
                                result.get("storage_rows_avoided") or deduplicated
                            ),
                            "filtered_out": result.get("filtered_out", 0),
                            "skipped": result.get("skipped", 0),
                            "error_count": len(result.get("errors") or []),
                            "fetch_seconds": fetched_item.get("fetch_seconds"),
                        }
                    )
                except Exception as exc:
                    counts["odds_fixture_failed"] += 1
                    odds_results.append(
                        {
                            "sportmonks_fixture_id": sportmonks_fixture_id,
                            "status": "ingestion_failed",
                            "error": exc.__class__.__name__,
                            "fetch_seconds": fetched_item.get("fetch_seconds"),
                        }
                    )

        transport_audit = client.transport_audit()

    return {
        "status": "ok" if fixture_ingestion.get("status") == "ok" else "partial",
        "version": DAILY_OPERATIONS_VERSION,
        "target_date": effective_date.isoformat(),
        "timezone": BUSINESS_TIMEZONE,
        "fixture_ingestion": fixture_ingestion,
        "target_fixtures": {
            "count": len(fixtures),
            "items": [_fixture_payload(fixture) for fixture in fixtures],
        },
        "odds": {
            "refresh_requested": refresh_odds,
            "snapshot_window": snapshot_window if refresh_odds else None,
            "fixtures_ok": counts["odds_fixture_ok"],
            "fixtures_failed": counts["odds_fixture_failed"],
            "rows_created": counts["odds_rows_created"],
            "initial_states_created": counts["odds_initial_states_created"],
            "movements_created": counts["odds_movements_created"],
            "deduplicated_unchanged": counts["odds_unchanged_deduplicated"],
            "storage_rows_avoided": counts["odds_storage_rows_avoided"],
            "items": odds_results,
        },
        "performance": {
            "cycle_seconds": round(perf_counter() - cycle_started, 6),
            "odds_fetch": odds_fetch_audit,
            "sportmonks_transport": transport_audit,
            "network_fetch_concurrent_db_persistence_sequential": True,
        },
        "policy": {
            "target_leagues_only_for_odds": True,
            "sportmonks_fixture_ids_are_returned": True,
            "manual_fixture_id_lookup_required": False,
            "manual_odds_ingestion_required": False,
            "daily_sync_is_source_ingestion_only": True,
            "predictions_or_decisions_are_not_generated": True,
            "unchanged_odds_are_collapsed_without_losing_price_movements": True,
        },
    }


def build_daily_status(*, target_date: date | None = None) -> dict:
    effective_date = target_date or _business_today()
    fixtures = _target_fixtures_for_date(effective_date)
    fixture_ids = [int(fixture.id) for fixture in fixtures]

    odds_by_fixture: dict[int, tuple[int, datetime | None]] = {}
    predictions_by_fixture: dict[int, int] = {}

    if fixture_ids:
        with SessionLocal() as session:
            odds_rows = session.execute(
                select(
                    OddsSnapshot.fixture_id,
                    func.count(OddsSnapshot.id),
                    func.max(OddsSnapshot.fetched_at),
                )
                .where(OddsSnapshot.fixture_id.in_(fixture_ids))
                .group_by(OddsSnapshot.fixture_id)
            ).all()
            odds_by_fixture = {
                int(fixture_id): (int(count or 0), latest_at)
                for fixture_id, count, latest_at in odds_rows
            }

            prediction_rows = session.execute(
                select(Prediction.fixture_id, func.count(Prediction.id))
                .where(Prediction.fixture_id.in_(fixture_ids))
                .group_by(Prediction.fixture_id)
            ).all()
            predictions_by_fixture = {
                int(fixture_id): int(count or 0)
                for fixture_id, count in prediction_rows
            }

    items: list[dict] = []
    for fixture in fixtures:
        odds_count, latest_odds_at = odds_by_fixture.get(int(fixture.id), (0, None))
        item = _fixture_payload(fixture)
        item.update(
            {
                "odds_rows": odds_count,
                "latest_odds_fetched_at": latest_odds_at.isoformat() if latest_odds_at else None,
                "prediction_count": predictions_by_fixture.get(int(fixture.id), 0),
            }
        )
        items.append(item)

    return {
        "status": "ok",
        "version": DAILY_OPERATIONS_VERSION,
        "target_date": effective_date.isoformat(),
        "timezone": BUSINESS_TIMEZONE,
        "target_fixture_count": len(items),
        "fixtures": items,
        "performance": {
            "status_queries": 2 if fixture_ids else 0,
            "n_plus_one_status_queries_removed": True,
        },
    }


@router.post("/daily-sync")
async def daily_sync_endpoint(
    target_date: date | None = None,
    refresh_odds: bool = True,
) -> dict:
    try:
        return await run_daily_sync(target_date=target_date, refresh_odds=refresh_odds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc


@router.get("/today")
def daily_status_endpoint(
    target_date: date | None = Query(default=None),
) -> dict:
    try:
        return build_daily_status(target_date=target_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
