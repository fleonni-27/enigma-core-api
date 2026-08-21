from __future__ import annotations

from datetime import date

from fastapi import HTTPException, Query

from app.historical_controller_v2 import run_historical_controller_v2
from app.main import app
from app.upstream_exceptions import register_upstream_exceptions

app.version = "0.15.0"

# Replace only the historical controller route; preserve all other v0.14.0 routes.
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
