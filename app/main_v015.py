from __future__ import annotations

import asyncio
import logging
from datetime import date

from fastapi import HTTPException, Query

from app.config import get_settings
from app.daily_operations import router as daily_operations_router
from app.daily_prediction_runner_v2 import router as daily_prediction_runner_router
from app.dashboard_operations_v2 import router as dashboard_operations_v2_router
from app.dashboard_operations_v2_bulk import install_dashboard_operations_v2_bulk_reads
from app.dashboard_operations_v2_health import install_dashboard_operations_v2_health
from app.db_release import ensure_database_release_current
from app.decision_engine_v2 import router as decision_engine_v2_router
from app.historical_controller_v2 import run_historical_controller_v2
from app.internal_endpoint_auth import install_internal_endpoint_auth
from app.j1_scheduler_routes import install_j1_scheduler_routes
from app.main import app
from app.outcome_score_capture import backfill_missing_settled_fixture_results
from app.prediction_window_policy import install_prediction_window_policy
from app.probability_calibration import build_probability_calibration_v1
from app.snapshot_recovery_2026 import (
    recover_missing_2026_snapshots,
    router as snapshot_recovery_router,
)
from app.upstream_exceptions import register_upstream_exceptions

app.version = "0.43.0"
logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task] = set()


async def _run_managed_startup_maintenance() -> None:
    settings = get_settings()
    if str(settings.app_env or "").lower() == "production":
        try:
            result = await asyncio.to_thread(ensure_database_release_current)
            logger.info(
                "database release background status=%s current=%s head=%s migrated=%s",
                result.get("status"),
                result.get("current_revision") or result.get("final_revision"),
                result.get("head_revision"),
                result.get("migration_executed", result.get("startup_fallback_executed", False)),
            )
        except Exception:
            # Production readiness must not be held hostage by a long concurrent
            # index build. The old release remains live while this background
            # maintenance attempt is observable in Render logs.
            logger.exception("managed database release background attempt failed")

    if settings.recover_2026_snapshots_on_startup:
        try:
            recovery = await recover_missing_2026_snapshots()
            logger.warning(
                "snapshot_recovery_2026_summary status=%s selected=%s recovered=%s "
                "training_core=%s incomplete=%s empty=%s upstream_failed=%s "
                "persistence_failed=%s remaining=%s remaining_by_league=%s",
                recovery.get("status"),
                recovery.get("selected_missing"),
                recovery.get("recovered"),
                recovery.get("recovered_training_core"),
                recovery.get("recovered_incomplete"),
                recovery.get("unrecoverable_empty_payload"),
                recovery.get("upstream_failed"),
                recovery.get("persistence_failed"),
                recovery.get("remaining_missing"),
                recovery.get("remaining_missing_by_league"),
            )
        except Exception:
            logger.exception("2026 snapshot recovery background attempt failed")


@app.on_event("startup")
async def schedule_managed_startup_maintenance() -> None:
    settings = get_settings()
    should_run = (
        str(settings.app_env or "").lower() == "production"
        or settings.recover_2026_snapshots_on_startup
    )
    if not should_run:
        return

    # Render's existing service did not inherit preDeployCommand from the
    # blueprint. Run managed migrations off the readiness path so CREATE INDEX
    # CONCURRENTLY cannot cause a port-open deployment timeout. Recovery, when
    # explicitly enabled, runs after the migration attempt in the same task.
    task = asyncio.create_task(_run_managed_startup_maintenance())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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


# Install the reserved prediction-window policy before exposing mutable routes.
# The public inference endpoint fails fast on j1_45m_v1, while the database
# mapper remains the final integrity boundary for every ORM insert/update path.
install_prediction_window_policy()

# Protect every unsafe HTTP method. The two automation POST routes accept a
# GitHub Actions OIDC token bound to this repository/main/workflow; all other
# mutable endpoints require X-Enigma-Internal-Key when manual access is needed.
install_internal_endpoint_auth(app)

# Patch the HTTP J1 endpoint through the same advisory lock used by the Render
# cron, then enrich Operations V2 with fixed bulk reads and persisted scheduler health.
install_j1_scheduler_routes()
install_dashboard_operations_v2_bulk_reads()
install_dashboard_operations_v2_health()

# Keep operations routers on the long-lived entrypoint as well as the current wrappers.
app.include_router(daily_operations_router)
app.include_router(daily_prediction_runner_router)
app.include_router(dashboard_operations_v2_router)
app.include_router(decision_engine_v2_router)
app.include_router(snapshot_recovery_router)
