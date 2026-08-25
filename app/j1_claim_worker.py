from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
from datetime import datetime, timezone
from threading import Event, Thread
from time import sleep
from typing import Any

from sqlalchemy import select

from app import daily_prediction_runner as legacy
from app import daily_prediction_runner_v2 as runner_module
from app.database import SessionLocal
from app.daily_prediction_runner_v2 import (
    DAILY_PREDICTION_RUNNER_VERSION,
    DEFAULT_MAX_LATENESS_MINUTES,
    InferenceRuntimeV2,
)
from app.j1_capacity import activate_j1_runner_capacity
from app.j1_pending_selector_v2 import install_j1_pending_selector_v2
from app.j1_work_queue import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_DELAY_SECONDS,
    claim_next_j1_work,
    complete_j1_work,
    expire_past_kickoff_work,
    fail_j1_work,
    renew_j1_claim,
)
from app.models import Fixture
from app.prediction_window_policy import (
    authorized_prediction_producer,
    install_prediction_window_policy,
)

J1_CLAIM_WORKER_VERSION = "j1_claim_worker_v1"
DEFAULT_POLL_SECONDS = 1.0

TERMINAL_ITEM_STATUSES = {"completed", "already_recorded"}
RETRYABLE_ITEM_STATUSES = {
    "inference_not_ready",
    "decision_not_ready",
    "inference_failed",
    "decision_failed",
    "ledger_not_ready",
}


def _worker_id() -> str:
    return str(
        os.getenv("J1_WORKER_ID")
        or os.getenv("RENDER_INSTANCE_ID")
        or f"{socket.gethostname()}:{os.getpid()}"
    )[:160]


def _load_fixture(fixture_id: int) -> Fixture | None:
    with SessionLocal() as session:
        row = session.scalar(select(Fixture).where(Fixture.id == int(fixture_id)).limit(1))
        if row is None:
            return None
        session.expunge(row)
        return row


def _result_item_status(result: dict[str, Any]) -> str:
    items = list(result.get("items") or [])
    if not items:
        return "no_item_result"
    return str(items[0].get("status") or "unknown")


def _classify_result(result: dict[str, Any]) -> tuple[bool, bool, str]:
    """Return (success, retryable, item_status)."""

    if result.get("status") != "ok":
        return False, True, str(result.get("status") or "runner_non_ok")
    item_status = _result_item_status(result)
    if item_status in TERMINAL_ITEM_STATUSES:
        return True, False, item_status
    if item_status in RETRYABLE_ITEM_STATUSES:
        return False, True, item_status
    return False, False, item_status


async def _run_claimed_fixture(
    *,
    claim: dict[str, Any],
    runtime: InferenceRuntimeV2,
) -> dict[str, Any]:
    """Run the canonical V2 pipeline for exactly the claimed fixture.

    This adapter deliberately reuses the existing V2 runner instead of copying
    inference/decision/ledger logic. The dedicated worker process is single-job
    at a time, so temporarily pinning the selector and runtime is process-local
    and cannot affect another worker instance.
    """

    fixture = _load_fixture(int(claim["fixture_id"]))
    if fixture is None:
        return {
            "status": "failed",
            "version": J1_CLAIM_WORKER_VERSION,
            "items": [{"status": "fixture_missing"}],
        }

    original_selector = legacy._due_target_fixtures
    original_runtime_factory = runner_module.InferenceRuntimeV2

    def claimed_selector(*, now, max_lateness_minutes, max_fixtures):
        return [fixture]

    def persistent_runtime_factory(**kwargs):
        return runtime

    legacy._due_target_fixtures = claimed_selector
    runner_module.InferenceRuntimeV2 = persistent_runtime_factory
    try:
        with authorized_prediction_producer(DAILY_PREDICTION_RUNNER_VERSION):
            result = await runner_module.run_daily_prediction_runner(
                max_lateness_minutes=DEFAULT_MAX_LATENESS_MINUTES,
                max_fixtures=1,
            )
    finally:
        runner_module.InferenceRuntimeV2 = original_runtime_factory
        legacy._due_target_fixtures = original_selector

    result["work_claim"] = {
        "version": J1_CLAIM_WORKER_VERSION,
        "work_id": int(claim["id"]),
        "worker_id": claim.get("claimed_by"),
        "attempt_count": int(claim.get("attempt_count") or 0),
        "claim_token_present": bool(claim.get("claim_token")),
        "one_fixture_per_claim": True,
        "inference_runtime_reused_across_worker_jobs": True,
    }
    return result


class _LeaseHeartbeat:
    def __init__(self, claim: dict[str, Any], *, lease_seconds: int) -> None:
        self.claim = claim
        self.lease_seconds = int(lease_seconds)
        self.stop_event = Event()
        self.thread = Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        interval = max(10, min(60, self.lease_seconds // 3))
        while not self.stop_event.wait(interval):
            try:
                ok = renew_j1_claim(
                    work_id=int(self.claim["id"]),
                    claim_token=str(self.claim["claim_token"]),
                    lease_seconds=self.lease_seconds,
                )
                if not ok:
                    return
            except Exception:
                # Losing a heartbeat must not kill the in-flight prediction.
                # The immutable prediction/ledger constraints remain the final
                # duplicate-write guard if another worker later reclaims it.
                continue

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop_event.set()
        self.thread.join(timeout=2)


async def process_one_claim(
    *,
    worker_id: str,
    runtime: InferenceRuntimeV2,
    lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any] | None:
    claim = claim_next_j1_work(worker_id=worker_id, lease_seconds=lease_seconds)
    if claim is None:
        return None

    try:
        with _LeaseHeartbeat(claim, lease_seconds=lease_seconds):
            result = await _run_claimed_fixture(claim=claim, runtime=runtime)
        success, retryable, item_status = _classify_result(result)
        if success:
            committed = complete_j1_work(
                work_id=int(claim["id"]),
                claim_token=str(claim["claim_token"]),
                result_status=item_status,
                result_payload=result,
            )
            return {
                "status": "completed" if committed else "claim_lost",
                "claim": claim,
                "item_status": item_status,
                "result": result,
            }

        transition = fail_j1_work(
            work_id=int(claim["id"]),
            claim_token=str(claim["claim_token"]),
            error=item_status,
            result_status=item_status,
            retryable=retryable,
            result_payload=result,
            retry_delay_seconds=retry_delay_seconds,
            max_attempts=max_attempts,
        )
        return {
            "status": (transition or {}).get("status") or "claim_lost",
            "claim": claim,
            "item_status": item_status,
            "result": result,
        }
    except Exception as exc:
        transition = fail_j1_work(
            work_id=int(claim["id"]),
            claim_token=str(claim["claim_token"]),
            error=exc.__class__.__name__,
            result_status="worker_exception",
            retryable=True,
            result_payload={"error": exc.__class__.__name__},
            retry_delay_seconds=retry_delay_seconds,
            max_attempts=max_attempts,
        )
        return {
            "status": (transition or {}).get("status") or "claim_lost",
            "claim": claim,
            "item_status": "worker_exception",
            "error": exc.__class__.__name__,
        }


async def run_worker_loop() -> None:
    activate_j1_runner_capacity()
    install_j1_pending_selector_v2()
    install_prediction_window_policy()

    worker_id = _worker_id()
    poll_seconds = max(0.2, float(os.getenv("J1_WORKER_POLL_SECONDS", DEFAULT_POLL_SECONDS)))
    lease_seconds = int(os.getenv("J1_WORKER_LEASE_SECONDS", DEFAULT_CLAIM_LEASE_SECONDS))
    retry_delay_seconds = int(
        os.getenv("J1_WORKER_RETRY_DELAY_SECONDS", DEFAULT_RETRY_DELAY_SECONDS)
    )
    max_attempts = int(os.getenv("J1_WORKER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    once = str(os.getenv("J1_WORKER_ONCE", "false")).lower() in {"1", "true", "yes"}

    runtime = InferenceRuntimeV2()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    print(
        json.dumps(
            {
                "status": "started",
                "version": J1_CLAIM_WORKER_VERSION,
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
                "poll_seconds": poll_seconds,
                "max_attempts": max_attempts,
            }
        ),
        flush=True,
    )

    while not stopping.is_set():
        expire_past_kickoff_work()
        outcome = await process_one_claim(
            worker_id=worker_id,
            runtime=runtime,
            lease_seconds=lease_seconds,
            retry_delay_seconds=retry_delay_seconds,
            max_attempts=max_attempts,
        )
        if outcome is not None:
            print(json.dumps(outcome, ensure_ascii=False, default=str), flush=True)
            if once:
                break
            continue
        if once:
            break
        try:
            await asyncio.wait_for(stopping.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass

    print(
        json.dumps(
            {
                "status": "stopped",
                "version": J1_CLAIM_WORKER_VERSION,
                "worker_id": worker_id,
                "stopped_at": datetime.now(timezone.utc).isoformat(),
                "runtime": runtime.audit(),
            },
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )


def main() -> None:
    asyncio.run(run_worker_loop())


if __name__ == "__main__":
    main()
