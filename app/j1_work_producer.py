from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from time import perf_counter

from sqlalchemy import text

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
# Same lock as the legacy batch scheduler: during migration, producer and batch
# execution must never mutate J1 state at the same time.
J1_PRODUCER_ADVISORY_LOCK_KEY = 450026


async def run_producer_cycle() -> dict:
    started = perf_counter()
    now = datetime.now(timezone.utc)
    capacity = activate_j1_runner_capacity()
    max_fixtures = configured_j1_max_fixtures()

    install_j1_pending_selector_v2()
    install_prediction_window_policy()

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
            odds_window_clv = await run_odds_window_clv_cycle(now=now)
        except TypeError:
            # Compatibility with the current function signature if `now` is not
            # accepted by an older deployed odds-window module.
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
            "prediction_window_policy": recovery,
            "queue": queue,
            "odds_window_clv": odds_window_clv,
            "policy": {
                "producer_never_runs_prediction_or_decision": True,
                "one_queue_item_per_fixture_snapshot_window": True,
                "workers_claim_with_skip_locked": True,
                "closing_clv_remains_isolated_from_j1_claiming": True,
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
