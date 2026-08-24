from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timezone
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


async def run_daily_sync(*, target_date: date | None = None, refresh_odds: bool = True) -> dict:
    effective_date = target_date or _business_today()
    client = SportmonksClient()

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
        for fixture in fixtures:
            sportmonks_fixture_id = int(fixture.sportmonks_id)
            try:
                payload = await client.prematch_odds_by_fixture(sportmonks_fixture_id)
                result = ingest_prematch_odds_payload(
                    sportmonks_fixture_id=sportmonks_fixture_id,
                    payload=payload,
                    snapshot_window=snapshot_window,
                )
                created = int(result.get("created") or 0)
                counts["odds_fixture_ok"] += 1
                counts["odds_rows_created"] += created
                odds_results.append(
                    {
                        "sportmonks_fixture_id": sportmonks_fixture_id,
                        "status": result.get("status"),
                        "received": result.get("received", 0),
                        "created": created,
                        "filtered_out": result.get("filtered_out", 0),
                        "skipped": result.get("skipped", 0),
                        "error_count": len(result.get("errors") or []),
                    }
                )
            except Exception as exc:
                counts["odds_fixture_failed"] += 1
                odds_results.append(
                    {
                        "sportmonks_fixture_id": sportmonks_fixture_id,
                        "status": "upstream_failed",
                        "error": exc.__class__.__name__,
                    }
                )

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
            "items": odds_results,
        },
        "policy": {
            "target_leagues_only_for_odds": True,
            "sportmonks_fixture_ids_are_returned": True,
            "manual_fixture_id_lookup_required": False,
            "manual_odds_ingestion_required": False,
            "daily_sync_is_source_ingestion_only": True,
            "predictions_or_decisions_are_not_generated": True,
        },
    }


def build_daily_status(*, target_date: date | None = None) -> dict:
    effective_date = target_date or _business_today()
    fixtures = _target_fixtures_for_date(effective_date)

    items: list[dict] = []
    with SessionLocal() as session:
        for fixture in fixtures:
            odds_count = int(
                session.scalar(
                    select(func.count(OddsSnapshot.id)).where(OddsSnapshot.fixture_id == fixture.id)
                )
                or 0
            )
            latest_odds_at = session.scalar(
                select(func.max(OddsSnapshot.fetched_at)).where(OddsSnapshot.fixture_id == fixture.id)
            )
            prediction_count = int(
                session.scalar(
                    select(func.count(Prediction.id)).where(Prediction.fixture_id == fixture.id)
                )
                or 0
            )
            item = _fixture_payload(fixture)
            item.update(
                {
                    "odds_rows": odds_count,
                    "latest_odds_fetched_at": latest_odds_at.isoformat() if latest_odds_at else None,
                    "prediction_count": prediction_count,
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
