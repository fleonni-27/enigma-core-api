from __future__ import annotations

import asyncio
import logging
import os
from datetime import date

from fastapi import FastAPI

from app import xg_historical_backfill
from app.xg_backfill_memory_scan import latest_snapshot_rows_memory_bounded

# Replace only the scanner implementation. The public/status/run contracts and
# append-only persistence semantics remain in xg_historical_backfill_v1.
xg_historical_backfill._latest_snapshot_rows = latest_snapshot_rows_memory_bounded

logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task] = set()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _startup_config() -> dict:
    leagues = [item.strip() for item in os.getenv("XG_BACKFILL_LEAGUES", "Serie A").split(",") if item.strip()]
    return {
        "start_date": date.fromisoformat(os.getenv("XG_BACKFILL_START_DATE", "2026-01-01")),
        "end_date": date.fromisoformat(os.getenv("XG_BACKFILL_END_DATE", "2026-08-24")),
        "leagues": leagues,
        "limit": int(os.getenv("XG_BACKFILL_LIMIT", "1000")),
        "concurrency": int(os.getenv("XG_BACKFILL_CONCURRENCY", "3")),
    }


async def _run_startup_backfill() -> None:
    try:
        config = _startup_config()
        result = await xg_historical_backfill.backfill_missing_xg(**config)
        logger.warning(
            "xg_historical_backfill_summary status=%s window=%s..%s leagues=%s selected=%s "
            "created=%s upstream_unavailable=%s upstream_failed=%s persistence_failed=%s remaining=%s requests=%s retries=%s",
            result.get("status"),
            (result.get("window") or {}).get("start"),
            (result.get("window") or {}).get("end"),
            result.get("leagues"),
            result.get("selected"),
            result.get("created"),
            result.get("upstream_xg_unavailable"),
            result.get("upstream_failed"),
            result.get("persistence_failed"),
            result.get("remaining_eligible_missing_xg"),
            (result.get("sportmonks_transport") or {}).get("requests"),
            ((result.get("sportmonks_transport") or {}).get("retry") or {}).get("retries"),
        )
    except Exception:
        logger.exception("xg historical backfill startup attempt failed")


def install_xg_backfill_startup(app: FastAPI) -> None:
    @app.on_event("startup")
    async def schedule_xg_historical_backfill() -> None:
        if not _truthy(os.getenv("XG_BACKFILL_ON_STARTUP")):
            return
        task = asyncio.create_task(_run_startup_backfill())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
