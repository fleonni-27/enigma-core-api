from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query

from app import daily_prediction_runner_v2 as runner_module
from app.j1_capacity import HARD_MAX_J1_FIXTURES, configured_j1_max_fixtures
from app.j1_pending_selector_v2 import install_j1_pending_selector_v2

_installed = False


async def _scheduled_daily_prediction_runner_endpoint(
    max_lateness_minutes: int = Query(
        default=runner_module.DEFAULT_MAX_LATENESS_MINUTES,
        ge=1,
        le=30,
    ),
    max_fixtures: int | None = Query(
        default=None,
        ge=1,
        le=HARD_MAX_J1_FIXTURES,
    ),
) -> dict[str, Any]:
    try:
        from app.j1_scheduler import run_j1_cycle

        effective_max_fixtures = (
            int(max_fixtures)
            if max_fixtures is not None
            else configured_j1_max_fixtures()
        )
        return await run_j1_cycle(
            source="github_actions_http_fallback",
            max_lateness_minutes=max_lateness_minutes,
            max_fixtures=effective_max_fixtures,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc


def install_j1_scheduler_routes() -> None:
    global _installed
    if _installed:
        return

    # The HTTP fallback and the Render cron must use the same pending selector.
    # Install it before the mutable J1 route is exposed so already-recorded
    # fixtures can never consume the per-cycle selection capacity.
    install_j1_pending_selector_v2()

    runner_module.router.routes = [
        route
        for route in runner_module.router.routes
        if not (
            getattr(route, "path", None) == "/operations/daily-prediction-runner"
            and "POST" in (getattr(route, "methods", set()) or set())
        )
    ]
    runner_module.router.add_api_route(
        "/daily-prediction-runner",
        _scheduled_daily_prediction_runner_endpoint,
        methods=["POST"],
        response_model=None,
    )
    _installed = True