from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.dashboard_enrichment_cache import persist_dashboard_enrichment
from app.dashboard_j1_team_enrichment import build_bulk_team_enrichment
from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture
from app.xg_historical_backfill import backfill_missing_xg

RUNNER_VERSION = "dashboard_enrichment_runner_v1"
BUSINESS_TIMEZONE = "America/Sao_Paulo"
TARGET_DAYS_AHEAD = 1
XG_BACKFILL_DAYS = 180
XG_BACKFILL_LIMIT = 12
XG_BACKFILL_CONCURRENCY = 2


def _utc_window() -> tuple[datetime, datetime]:
    tz = ZoneInfo(BUSINESS_TIMEZONE)
    local_today = datetime.now(tz).date()
    local_start = datetime.combine(local_today, time.min, tzinfo=tz)
    local_end = datetime.combine(local_today + timedelta(days=TARGET_DAYS_AHEAD + 1), time.min, tzinfo=tz)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _target_items() -> list[dict]:
    start_utc, end_utc = _utc_window()
    with SessionLocal() as session:
        rows = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at >= start_utc, Fixture.starts_at < end_utc)
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()
    items: list[dict] = []
    for fixture in rows:
        canonical = canonical_league(fixture.league_name)
        if not canonical.get("target"):
            continue
        items.append(
            {
                "fixture_id": int(fixture.id),
                "sportmonks_fixture_id": int(fixture.sportmonks_id),
                "league": str(canonical.get("canonical_name") or fixture.league_name or "Unknown"),
                "home_team": fixture.home_team,
                "away_team": fixture.away_team,
                "starts_at": fixture.starts_at.isoformat(),
            }
        )
    return items


def _should_run_provider_backfill(now: datetime) -> bool:
    # Cron runs every 15 minutes. Provider recovery runs only once every 6 hours,
    # keeping dashboard materialization frequent without hammering Sportmonks.
    return now.minute == 0 and now.hour % 6 == 0


async def run_background_enrichment() -> dict:
    started = datetime.now(timezone.utc)
    items = _target_items()
    provider_backfill = {"status": "skipped", "reason": "six_hour_cadence"}

    if items and _should_run_provider_backfill(started):
        local_today = datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()
        leagues = sorted({str(item["league"]) for item in items})
        try:
            provider_backfill = await backfill_missing_xg(
                start_date=local_today - timedelta(days=XG_BACKFILL_DAYS),
                end_date=local_today - timedelta(days=1),
                leagues=leagues,
                limit=XG_BACKFILL_LIMIT,
                concurrency=XG_BACKFILL_CONCURRENCY,
            )
        except Exception as exc:
            provider_backfill = {
                "status": "failed",
                "error": exc.__class__.__name__,
                "isolated_from_dashboard_and_j1": True,
            }

    enrichments = build_bulk_team_enrichment(items) if items else {}
    materialized: dict[int, dict] = {}
    for item in items:
        fixture_id = int(item["fixture_id"])
        enrichment = enrichments.get(fixture_id) or {}
        materialized[fixture_id] = {
            "team_metrics": enrichment.get("team_metrics") or {},
            "facts": list(enrichment.get("facts") or []),
            "data_quality": {
                **dict(enrichment.get("data_quality") or {}),
                "materialized_in_background": True,
                "provider_calls_during_dashboard_request": False,
                "xg_xga_informational_only": True,
                "prediction_engine_unchanged": True,
            },
            "source": "sportmonks_persisted_history",
            "runner_version": RUNNER_VERSION,
        }

    persistence = persist_dashboard_enrichment(materialized)
    finished = datetime.now(timezone.utc)
    return {
        "status": "ok",
        "version": RUNNER_VERSION,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "target_fixtures": len(items),
        "materialized": len(materialized),
        "persistence": persistence,
        "provider_backfill": {
            "status": provider_backfill.get("status"),
            "selected": provider_backfill.get("selected"),
            "created": provider_backfill.get("created"),
            "upstream_failed": provider_backfill.get("upstream_failed"),
            "upstream_xg_unavailable": provider_backfill.get("upstream_xg_unavailable"),
            "reason": provider_backfill.get("reason"),
            "error": provider_backfill.get("error"),
        },
        "policy": {
            "separate_from_j1_runner": True,
            "dashboard_reads_cache_only": True,
            "provider_backfill_every_six_hours": True,
            "materialization_every_fifteen_minutes": True,
            "no_prediction_or_decision_writes": True,
        },
    }


def main() -> None:
    result = asyncio.run(run_background_enrichment())
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
