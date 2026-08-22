from __future__ import annotations

from datetime import date

from fastapi import HTTPException, Query

from app.baseline_1x2 import build_baseline_1x2_temporal_v1
from app.baseline_1x2_policy import build_baseline_1x2_confidence_policy_v1
from app.historical_controller_v2 import run_historical_controller_v2
from app.main_legacy_v014 import app
from app.model_dataset import build_model_dataset_v1
from app.training_dataset_full import build_full_training_dataset
from app.training_dataset_split import build_temporal_training_split
from app.training_dataset_v11 import build_training_dataset_v11
from app.upstream_exceptions import register_upstream_exceptions

app.version = "0.22.0"

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
        return register_upstream_exceptions(start_date=start_date, end_date=end_date, leagues=leagues, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/training/dataset")
def training_dataset_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    lookback_matches: int = Query(default=5, ge=1, le=10),
    min_history_matches: int = Query(default=3, ge=1, le=10),
    include_skipped_details: bool = False,
    skipped_detail_limit: int = Query(default=50, ge=0, le=200),
) -> dict:
    try:
        return build_training_dataset_v11(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            page=page,
            page_size=page_size,
            lookback_matches=lookback_matches,
            min_history_matches=min_history_matches,
            include_skipped_details=include_skipped_details,
            skipped_detail_limit=skipped_detail_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/training/dataset/full")
def full_training_dataset_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
    lookback_matches: int = Query(default=5, ge=1, le=10),
    min_history_matches: int = Query(default=3, ge=1, le=10),
    include_skipped_details: bool = False,
    skipped_detail_limit: int = Query(default=100, ge=0, le=200),
    max_rows: int = Query(default=5000, ge=1, le=5000),
) -> dict:
    try:
        return build_full_training_dataset(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            lookback_matches=lookback_matches,
            min_history_matches=min_history_matches,
            include_skipped_details=include_skipped_details,
            skipped_detail_limit=skipped_detail_limit,
            max_rows=max_rows,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/training/dataset/split")
def temporal_training_split_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
    lookback_matches: int = Query(default=5, ge=1, le=10),
    min_history_matches: int = Query(default=3, ge=1, le=10),
    train_ratio: float = Query(default=0.70, gt=0, lt=1),
    validation_ratio: float = Query(default=0.15, gt=0, lt=1),
    max_rows: int = Query(default=5000, ge=1, le=5000),
    include_rows: bool = True,
) -> dict:
    try:
        return build_temporal_training_split(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            lookback_matches=lookback_matches,
            min_history_matches=min_history_matches,
            train_ratio=train_ratio,
            validation_ratio=validation_ratio,
            max_rows=max_rows,
            include_rows=include_rows,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/training/model-dataset")
def model_dataset_v1_endpoint(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = Query(default=None),
    lookback_matches: int = Query(default=5, ge=1, le=10),
    min_history_matches: int = Query(default=3, ge=1, le=10),
    train_ratio: float = Query(default=0.70, gt=0, lt=1),
    validation_ratio: float = Query(default=0.15, gt=0, lt=1),
    max_rows: int = Query(default=5000, ge=1, le=5000),
    include_rows: bool = False,
) -> dict:
    try:
        return build_model_dataset_v1(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            lookback_matches=lookback_matches,
            min_history_matches=min_history_matches,
            train_ratio=train_ratio,
            validation_ratio=validation_ratio,
            max_rows=max_rows,
            include_rows=include_rows,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/models/baseline/1x2")
def baseline_1x2_temporal_endpoint(
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
    include_predictions: bool = False,
) -> dict:
    try:
        return build_baseline_1x2_temporal_v1(
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
            include_predictions=include_predictions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/models/baseline/1x2/policy")
def baseline_1x2_confidence_policy_endpoint(
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
    include_predictions: bool = False,
) -> dict:
    try:
        return build_baseline_1x2_confidence_policy_v1(
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
            include_predictions=include_predictions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc
