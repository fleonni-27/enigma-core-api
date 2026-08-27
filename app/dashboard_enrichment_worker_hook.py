from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text

from app.dashboard_enrichment_runner import run_background_enrichment
from app.database import SessionLocal, engine
from app.models import DashboardEnrichmentSnapshot

ENRICHMENT_ADVISORY_LOCK_KEY = 450028
ENRICHMENT_IDLE_INTERVAL_SECONDS = 15 * 60
ENRICHMENT_FRESH_SECONDS = 12 * 60
ENRICHMENT_TIMEOUT_SECONDS = 120

_last_attempt_monotonic = 0.0


def _production_enabled() -> bool:
    return str(os.getenv("APP_ENV", "")).strip().lower() == "production"


def _cache_is_fresh() -> bool:
    with SessionLocal() as session:
        latest = session.scalar(select(func.max(DashboardEnrichmentSnapshot.generated_at)))
    if latest is None:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - latest <= timedelta(seconds=ENRICHMENT_FRESH_SECONDS)


async def maybe_run_idle_dashboard_enrichment(*, worker_id: str) -> dict[str, Any]:
    global _last_attempt_monotonic

    if not _production_enabled():
        return {"status": "disabled", "reason": "non_production"}

    now_mono = time.monotonic()
    if now_mono - _last_attempt_monotonic < ENRICHMENT_IDLE_INTERVAL_SECONDS:
        return {"status": "skipped", "reason": "local_interval"}
    _last_attempt_monotonic = now_mono

    # Production uses PostgreSQL. Fail closed rather than attempt a different
    # locking semantic in tests or unsupported environments.
    if engine.dialect.name != "postgresql":
        return {"status": "disabled", "reason": "postgres_lock_required"}

    with engine.connect() as lock_conn:
        acquired = bool(
            lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": ENRICHMENT_ADVISORY_LOCK_KEY},
            ).scalar()
        )
        if not acquired:
            return {"status": "skipped", "reason": "lock_busy"}

        try:
            # Re-check freshness only after taking the global lock so another
            # worker that just finished materialization suppresses this run.
            if _cache_is_fresh():
                return {"status": "skipped", "reason": "cache_fresh"}
            try:
                result = await asyncio.wait_for(
                    run_background_enrichment(),
                    timeout=ENRICHMENT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                return {
                    "status": "failed",
                    "reason": "timeout",
                    "worker_id": worker_id,
                    "timeout_seconds": ENRICHMENT_TIMEOUT_SECONDS,
                }
            return {
                "status": "completed",
                "worker_id": worker_id,
                "result": result,
            }
        finally:
            try:
                lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": ENRICHMENT_ADVISORY_LOCK_KEY},
                )
            except Exception:
                pass
