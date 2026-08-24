from __future__ import annotations

import asyncio
import logging
from datetime import date

from fastapi import HTTPException, Query

from app.daily_operations import router as daily_operations_router
from app.daily_prediction_runner import router as daily_prediction_runner_router
from app.dashboard_operations_v2 import router as dashboard_operations_v2_router
from app.dashboard_operations_v2_health import install_dashboard_operations_v2_health
from app.historical_controller_v2 import run_historical_controller_v2
from app.j1_scheduler_routes import install_j1_scheduler_routes
from app.main import app
from app.outcome_score_capture import backfill_missing_settled_fixture_results
from app.probability_calibration import build_probability_calibration_v1
from app.upstream_exceptions import register_upstream_exceptions

app.version = "0.33.0"
logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task] = set()


async def _run_legacy_score_backfill() -> None:
    try:
        await backfill_missing_settled_fixture_results(limit=25)
    except Exception:
        logger.exception("legacy settled fixture score backfill failed")


@app.on_event("startup")
async def schedule_legacy_score_backfill() -> None:
    # Do not delay service readiness for historical score enrichment. The task
    # only fills the separate post-match result store; DecisionRecord remains
    # immutable and the dashboard stays read-only.
    task = asyncio.create_task(_run_legacy_score_backfill())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# Replace only the historical controller route; preserve all routes already exposed by app.main.
app.router.routes = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == "/backfill/historical/controller"
        and "POST" in (getattr(route, "methods", set()) or set())
    )
]


@app.post("/backfill/historical/controller")
async def historical_controller_v2_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
    batch_size: int = Query(default=25, ge=1, le=25),
    max_batches_per_month: int = Query(default=4, ge=1, le=8),
    ingest_fixtures: bool = True,
    skip_existing: bool = True,
    report_limit: int = Query(default=200, ge=1, le=200),
) -> dict:
    try:
        return await run_historical_controller_v2(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            batch_size=batch_size,
            max_batches_per_month=max_batches_per_month,
            ingest_fixtures=ingest_fixtures,
            skip_existing=skip_existing,
            report_limit=report_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.post("/exceptions/upstream")
def upstream_exception_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=25),
) -> dict:
    try:
        return register_upstream_exceptions(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/models/baseline/1x2/probability-calibration")
def probability_calibration_v1_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
    family: str = Query(default="STANDARD"),
    lookback_matches: int = Query(default=5, ge=1, le=10),
    min_history_matches: int = Query(default=3, ge=1, le=10),
    train_ratio: float = Query(default=0.70, gt=0, lt=1),
    validation_ratio: float = Query(default=0.15, gt=0, lt=1),
    max_rows: int = Query(default=5000, ge=1, le=5000),
    class_weight_balanced: bool = False,
    calibration_ratio: float = Query(default=0.20, ge=0.10, le=0.40),
) -> dict:
    try:
        return build_probability_calibration_v1(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            family=family,
            lookback_matches=lookback_matches,
            min_history_matches=min_history_matches,
            train_ratio=train_ratio,
            validation_ratio=validation_ratio,
            max_rows=max_rows,
            class_weight_balanced=class_weight_balanced,
            calibration_ratio=calibration_ratio,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


# Patch the legacy HTTP J1 endpoint through the same advisory lock used by the
# Render cron, then enrich Operations V2 with a persisted scheduler heartbeat.
install_j1_scheduler_routes()
install_dashboard_operations_v2_health()

# Keep operations routers on the long-lived entrypoint as well as the current wrappers.
# This protects production environments whose Render start command is still pinned to
# app.main_v015:app instead of following render.yaml changes automatically.
app.include_router(daily_operations_router)
app.include_router(daily_prediction_runner_router)
app.include_router(dashboard_operations_v2_router)
