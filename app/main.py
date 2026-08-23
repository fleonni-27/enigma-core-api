from __future__ import annotations

from datetime import date

from fastapi import HTTPException, Query

from app.baseline_1x2 import build_baseline_1x2_temporal_v1
from app.baseline_1x2_policy import build_baseline_1x2_confidence_policy_v1
from app.baseline_1x2_policy_v2 import build_baseline_1x2_confidence_policy_v2
from app.confidence_calibration import build_confidence_calibration_v1
from app.decision_engine import evaluate_1x2_quote, evaluate_fixture_decision
from app.favorite_confidence_calibration import build_favorite_confidence_calibration_v1
from app.probability_calibration import build_probability_calibration_v1
from app.historical_controller_v2 import run_historical_controller_v2
from app.main_legacy_v014 import app
from app.model_dataset import build_model_dataset_v1
from app.training_dataset_full import build_full_training_dataset
from app.training_dataset_split import build_temporal_training_split
from app.training_dataset_v11 import build_training_dataset_v11
from app.upstream_exceptions import register_upstream_exceptions

app.version = "0.27.0"

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


@app.get("/models/baseline/1x2/policy/v2")
def baseline_1x2_confidence_policy_v2_endpoint(
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
        return build_baseline_1x2_confidence_policy_v2(
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


@app.get("/models/baseline/1x2/calibration")
def confidence_calibration_v1_endpoint(
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
) -> dict:
    try:
        return build_confidence_calibration_v1(
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


@app.get("/models/baseline/1x2/favorite-confidence-calibration")
def favorite_confidence_calibration_v1_endpoint(
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
    oof_folds: int = Query(default=5, ge=3, le=10),
    min_initial_train_rows: int = Query(default=120, ge=60, le=2000),
) -> dict:
    try:
        return build_favorite_confidence_calibration_v1(
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
            oof_folds=oof_folds,
            min_initial_train_rows=min_initial_train_rows,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/decision/1x2/evaluate")
def decision_1x2_evaluate_endpoint(
    p_home: float = Query(..., ge=0.0, le=1.0),
    p_draw: float = Query(..., ge=0.0, le=1.0),
    p_away: float = Query(..., ge=0.0, le=1.0),
    odd_home: float = Query(..., gt=1.0, le=1000.0),
    odd_draw: float = Query(..., gt=1.0, le=1000.0),
    odd_away: float = Query(..., gt=1.0, le=1000.0),
    min_edge: float = Query(default=0.05, ge=0.0, le=0.30),
    min_expected_value: float = Query(default=0.03, ge=0.0, le=0.50),
    min_calibrated_confidence: float = Query(default=0.45, ge=0.30, le=0.80),
    max_overround: float = Query(default=0.12, ge=0.0, le=0.30),
    require_team_favorite_top_class: bool = True,
) -> dict:
    try:
        return evaluate_1x2_quote(
            p_home=p_home,
            p_draw=p_draw,
            p_away=p_away,
            odd_home=odd_home,
            odd_draw=odd_draw,
            odd_away=odd_away,
            min_edge=min_edge,
            min_expected_value=min_expected_value,
            min_calibrated_confidence=min_calibrated_confidence,
            max_overround=max_overround,
            require_team_favorite_top_class=require_team_favorite_top_class,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc


@app.get("/decision/fixture/{sportmonks_fixture_id}")
def decision_fixture_endpoint(
    sportmonks_fixture_id: int,
    prediction_window: str | None = Query(default=None, max_length=30),
    model_version: str | None = Query(default=None, max_length=30),
    snapshot_window: str | None = Query(default=None, max_length=30),
    min_edge: float = Query(default=0.05, ge=0.0, le=0.30),
    min_expected_value: float = Query(default=0.03, ge=0.0, le=0.50),
    min_calibrated_confidence: float = Query(default=0.45, ge=0.30, le=0.80),
    max_overround: float = Query(default=0.12, ge=0.0, le=0.30),
    max_quote_span_seconds: int = Query(default=300, ge=0, le=3600),
    require_team_favorite_top_class: bool = True,
    include_market_candidates: bool = False,
) -> dict:
    try:
        result = evaluate_fixture_decision(
            sportmonks_fixture_id=sportmonks_fixture_id,
            prediction_window=prediction_window,
            model_version=model_version,
            snapshot_window=snapshot_window,
            min_edge=min_edge,
            min_expected_value=min_expected_value,
            min_calibrated_confidence=min_calibrated_confidence,
            max_overround=max_overround,
            max_quote_span_seconds=max_quote_span_seconds,
            require_team_favorite_top_class=require_team_favorite_top_class,
            include_market_candidates=include_market_candidates,
        )
        if result.get("status") == "fixture_not_found":
            raise HTTPException(status_code=404, detail=result)
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"status": "failed", "error": exc.__class__.__name__}) from exc
