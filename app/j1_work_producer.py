from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from time import perf_counter
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.daily_operations import BUSINESS_TIMEZONE, run_daily_sync
from app.daily_operations_business_date_fix import install_daily_operations_business_date_fix
from app.database import engine
from app.daily_prediction_runner_v2 import (
    DEFAULT_MAX_LATENESS_MINUTES,
    J1_PREDICTION_WINDOW,
    J1_TARGET_LEAD_MINUTES,
)
from app.j1_capacity import activate_j1_runner_capacity, configured_j1_max_fixtures
from app.j1_pending_selector_v2 import install_j1_pending_selector_v2
from app.j1_work_queue import (
    enqueue_due_j1_work,
    expire_past_kickoff_work,
    queue_status,
)
from app.odds_window_clv import run_odds_window_clv_cycle
from app.prematch_inference import MODEL_VERSION
from app.prediction_window_policy import (
    install_prediction_window_policy,
    quarantine_invalid_reserved_j1_predictions,
)

J1_WORK_PRODUCER_VERSION = "j1_work_producer_v1"
J1_PRODUCER_ADVISORY_LOCK_KEY = 450026
PRODUCER_FIXTURE_DISCOVERY_OPERATION = "PRODUCER_FIXTURE_DISCOVERY_V1"
PRODUCER_FIXTURE_DISCOVERY_INTERVAL_MINUTES = 15


def _latest_discovery_started_at() -> datetime | None:
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT started_at
                FROM operation_run_records
                WHERE operation = :operation
                  AND status IN ('OK', 'DEGRADED')
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"operation": PRODUCER_FIXTURE_DISCOVERY_OPERATION},
        ).scalar_one_or_none()


def _start_discovery_run() -> int:
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    """
                    INSERT INTO operation_run_records
                        (operation, source, status, started_at, selected_fixtures, counts)
                    VALUES
                        (:operation, :source, 'RUNNING', :started_at, 0, CAST(:counts AS jsonb))
                    RETURNING id
                    """
                ),
                {
                    "operation": PRODUCER_FIXTURE_DISCOVERY_OPERATION,
                    "source": "render_producer",
                    "started_at": now,
                    "counts": "{}",
                },
            ).scalar_one()
        )


def _finish_discovery_run(
    run_id: int,
    *,
    status: str,
    selected_fixtures: int = 0,
    counts: dict | None = None,
    error: str | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE operation_run_records
                SET status = :status,
                    finished_at = :finished_at,
                    selected_fixtures = :selected_fixtures,
                    counts = CAST(:counts AS jsonb),
                    error = :error
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "status": status,
                "finished_at": datetime.now(timezone.utc),
                "selected_fixtures": int(selected_fixtures),
                "counts": json.dumps(counts or {}, ensure_ascii=False),
                "error": error,
            },
        )


async def _maybe_refresh_business_date_fixtures() -> dict:
    now = datetime.now(timezone.utc)
    latest = _latest_discovery_started_at()
    if latest is not None and latest >= now - timedelta(minutes=PRODUCER_FIXTURE_DISCOVERY_INTERVAL_MINUTES):
        return {
            "status": "ok",
            "action": "skipped_recent",
            "interval_minutes": PRODUCER_FIXTURE_DISCOVERY_INTERVAL_MINUTES,
            "last_started_at": latest.isoformat(),
        }

    run_id = _start_discovery_run()
    try:
        install_daily_operations_business_date_fix()
        target_date = datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()
        result = await run_daily_sync(target_date=target_date, refresh_odds=False)
        fixture_ingestion = result.get("fixture_ingestion") or {}
        target_fixtures = int((result.get("target_fixtures") or {}).get("count") or 0)
        status = "OK" if result.get("status") == "ok" else "DEGRADED"
        counts = {
            "target_fixtures": target_fixtures,
            "received": int(fixture_ingestion.get("received") or 0),
            "created": int(fixture_ingestion.get("created") or 0),
            "updated": int(fixture_ingestion.get("updated") or 0),
            "skipped": int(fixture_ingestion.get("skipped") or 0),
        }
        _finish_discovery_run(
            run_id,
            status=status,
            selected_fixtures=target_fixtures,
            counts=counts,
        )
        return {
            "status": result.get("status"),
            "action": "refreshed",
            "target_date": target_date.isoformat(),
            "target_fixtures": target_fixtures,
            "fixture_ingestion": fixture_ingestion,
            "business_date_fallback_installed": True,
            "interval_minutes": PRODUCER_FIXTURE_DISCOVERY_INTERVAL_MINUTES,
            "run_id": run_id,
        }
    except Exception as exc:
        _finish_discovery_run(run_id, status="FAILED", error=exc.__class__.__name__)
        return {
            "status": "failed",
            "action": "refresh_failed",
            "error": exc.__class__.__name__,
            "interval_minutes": PRODUCER_FIXTURE_DISCOVERY_INTERVAL_MINUTES,
            "run_id": run_id,
        }


async def run_producer_cycle() -> dict:
    started = perf_counter()
    now = datetime.now(timezone.utc)
    capacity = activate_j1_runner_capacity()
    max_fixtures = configured_j1_max_fixtures()

    install_j1_pending_selector_v2()
    install_prediction_window_policy()

    fixture_discovery = await _maybe_refresh_business_date_fixtures()

    connection = engine.connect()
    lock_acquired = False
    try:
        lock_acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": J1_PRODUCER_ADVISORY_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            return {
                "status": "ok",
                "version": J1_WORK_PRODUCER_VERSION,
                "run_health": {
                    "status": "IDLE",
                    "reason_codes": ["J1_BATCH_OR_PRODUCER_ALREADY_ACTIVE"],
                },
                "producer": {"lock_acquired": False},
                "fixture_discovery": fixture_discovery,
            }

        recovery = quarantine_invalid_reserved_j1_predictions(
            now=now,
            prediction_window=J1_PREDICTION_WINDOW,
            model_version=MODEL_VERSION,
            target_lead_minutes=J1_TARGET_LEAD_MINUTES,
            max_lateness_minutes=DEFAULT_MAX_LATENESS_MINUTES,
        )
        expired = expire_past_kickoff_work(now=now)
        queue = enqueue_due_j1_work(
            now=now,
            max_lateness_minutes=DEFAULT_MAX_LATENESS_MINUTES,
            max_fixtures=max_fixtures,
        )
        status = queue_status(now=now)

        try:
            odds_window_clv = await run_odds_window_clv_cycle()
        except Exception as exc:
            odds_window_clv = {
                "status": "failed",
                "version": "odds_window_clv_v1",
                "error": exc.__class__.__name__,
            }

        selected = int(queue.get("selected_fixtures") or 0)
        enqueued = int(queue.get("enqueued") or 0)
        return {
            "status": "ok",
            "version": J1_WORK_PRODUCER_VERSION,
            "evaluated_at": now.isoformat(),
            "run_health": {
                "status": "OK" if selected else "IDLE",
                "reason_codes": [] if selected else ["NO_J1_FIXTURES_DUE"],
            },
            "producer": {
                "lock_acquired": True,
                "fixture_capacity": capacity,
                "selected_fixtures": selected,
                "enqueued": enqueued,
                "already_queued": int(queue.get("already_queued") or 0),
                "expired_past_kickoff": expired,
                "queue_status": status,
                "cycle_seconds": round(perf_counter() - started, 6),
            },
            "fixture_discovery": fixture_discovery,
            "prediction_window_policy": recovery,
            "queue": queue,
            "odds_window_clv": odds_window_clv,
            "policy": {
                "producer_never_runs_prediction_or_decision": True,
                "one_queue_item_per_fixture_snapshot_window": True,
                "workers_claim_with_skip_locked": True,
                "closing_clv_remains_isolated_from_j1_claiming": True,
                "fixture_discovery_uses_business_date_fallback": True,
            },
        }
    finally:
        if lock_acquired:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": J1_PRODUCER_ADVISORY_LOCK_KEY},
                )
            except Exception:
                pass
        connection.close()


def main() -> None:
    result = asyncio.run(run_producer_cycle())
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    if result.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
