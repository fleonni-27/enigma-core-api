from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query

from app import daily_prediction_runner as runner_module

_installed = False


async def _scheduled_daily_prediction_runner_endpoint(
    max_lateness_minutes: int = Query(
        default=runner_module.DEFAULT_MAX_LATENESS_MINUTES,
        ge=1,
        le=30,
    ),
    max_fixtures: int = Query(
        default=runner_module.DEFAULT_MAX_FIXTURES,
        ge=1,
        le=runner_module.MAX_FIXTURES_PER_RUN,
    ),
) -> dict[str, Any]:
    try:
        from app.j1_scheduler import run_j1_cycle

        return await run_j1_cycle(
            source="github_actions_http_fallback",
            max_lateness_minutes=max_lateness_minutes,
            max_fixtures=max_fixtures,
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
