from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import DateTime, Float, Integer, JSON, String, func, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal

PERFORMANCE_OBSERVATORY_VERSION = "performance_observatory_v1"
PIPELINE_DAILY_SYNC = "daily_sync"
PIPELINE_J1 = "j1"
KNOWN_PIPELINES = frozenset({PIPELINE_DAILY_SYNC, PIPELINE_J1})
DEFAULT_LOOKBACK_HOURS = 24
MAX_LOOKBACK_HOURS = 24 * 30
LATEST_SAMPLE_LIMIT = 20

router = APIRouter(prefix="/operations", tags=["operations"])


class PipelinePerformanceSample(Base):
    __tablename__ = "pipeline_performance_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline: Mapped[str] = mapped_column(String(40), index=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cycle_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    upstream_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    dataset_build_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    fit_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    selected_fixtures: Mapped[int] = mapped_column(Integer, default=0)
    logical_requests: Mapped[int] = mapped_column(Integer, default=0)
    http_requests: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    rate_limited_responses: Mapped[int] = mapped_column(Integer, default=0)
    raw_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _transport_metrics(transport: dict[str, Any] | None) -> dict[str, int]:
    transport = transport or {}
    retry = transport.get("retry") or {}
    rate_limit = transport.get("rate_limit") or {}
    return {
        "logical_requests": _as_int(transport.get("logical_requests")),
        "http_requests": _as_int(transport.get("requests")),
        "retries": _as_int(retry.get("retries")),
        "rate_limited_responses": _as_int(rate_limit.get("responses_429")),
    }


def try_persist_pipeline_sample(
    *,
    pipeline: str,
    source: str,
    status: str,
    cycle_seconds: float | None,
    upstream_seconds: float | None = None,
    dataset_build_seconds: float | None = None,
    fit_seconds: float | None = None,
    selected_fixtures: int = 0,
    logical_requests: int = 0,
    http_requests: int = 0,
    retries: int = 0,
    rate_limited_responses: int = 0,
    run_id: int | None = None,
    raw_metrics: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist observability without making telemetry a pipeline failure domain."""

    normalized_pipeline = str(pipeline or "").strip().lower()
    if normalized_pipeline not in KNOWN_PIPELINES:
        return {
            "status": "not_persisted",
            "version": PERFORMANCE_OBSERVATORY_VERSION,
            "error": "UNKNOWN_PIPELINE",
        }

    when = observed_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)

    try:
        with SessionLocal() as session:
            row = PipelinePerformanceSample(
                pipeline=normalized_pipeline,
                source=str(source or "unknown")[:80],
                status=str(status or "UNKNOWN")[:30],
                run_id=run_id,
                observed_at=when,
                cycle_seconds=_as_float(cycle_seconds),
                upstream_seconds=_as_float(upstream_seconds),
                dataset_build_seconds=_as_float(dataset_build_seconds),
                fit_seconds=_as_float(fit_seconds),
                selected_fixtures=max(0, _as_int(selected_fixtures)),
                logical_requests=max(0, _as_int(logical_requests)),
                http_requests=max(0, _as_int(http_requests)),
                retries=max(0, _as_int(retries)),
                rate_limited_responses=max(0, _as_int(rate_limited_responses)),
                raw_metrics=dict(raw_metrics or {}),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return {
                "status": "persisted",
                "version": PERFORMANCE_OBSERVATORY_VERSION,
                "sample_id": int(row.id),
                "pipeline": normalized_pipeline,
            }
    except Exception as exc:
        return {
            "status": "not_persisted",
            "version": PERFORMANCE_OBSERVATORY_VERSION,
            "error": exc.__class__.__name__,
            "pipeline": normalized_pipeline,
        }


def record_daily_sync_result(result: dict[str, Any], *, source: str = "daily_operations") -> dict[str, Any]:
    performance = result.get("performance") or {}
    odds_fetch = performance.get("odds_fetch") or {}
    transport = performance.get("sportmonks_transport") or {}
    transport_metrics = _transport_metrics(transport)
    target_fixtures = result.get("target_fixtures") or {}
    return try_persist_pipeline_sample(
        pipeline=PIPELINE_DAILY_SYNC,
        source=source,
        status=str(result.get("status") or "UNKNOWN").upper(),
        cycle_seconds=_as_float(performance.get("cycle_seconds")),
        upstream_seconds=_as_float(odds_fetch.get("fetch_seconds")),
        selected_fixtures=_as_int(target_fixtures.get("count")),
        **transport_metrics,
        raw_metrics={
            "performance": performance,
            "odds": {
                key: value
                for key, value in (result.get("odds") or {}).items()
                if key != "items"
            },
        },
    )


def record_j1_result(
    result: dict[str, Any],
    *,
    source: str,
    run_id: int | None,
    scheduler_status: str,
) -> dict[str, Any]:
    performance = result.get("performance") or {}
    prefetch = performance.get("upstream_prefetch") or {}
    transport = performance.get("sportmonks_transport") or {}
    runtime = result.get("inference_runtime") or {}
    transport_metrics = _transport_metrics(transport)
    return try_persist_pipeline_sample(
        pipeline=PIPELINE_J1,
        source=source,
        status=scheduler_status,
        cycle_seconds=_as_float(performance.get("cycle_seconds")),
        upstream_seconds=_as_float(prefetch.get("prefetch_seconds")),
        dataset_build_seconds=_as_float(runtime.get("dataset_build_seconds")),
        fit_seconds=_as_float(runtime.get("fit_seconds")),
        selected_fixtures=_as_int(result.get("selected_fixtures")),
        run_id=run_id,
        **transport_metrics,
        raw_metrics={
            "performance": performance,
            "inference_runtime": runtime,
            "run_health": result.get("run_health") or {},
            "counts": result.get("counts") or {},
        },
    )


def _percentile_cont(values: list[float], quantile: float) -> float | None:
    """Linear interpolation matching PostgreSQL percentile_cont semantics."""

    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def _metric_percentiles(session, column, where_clauses: list[Any]) -> dict[str, Any]:
    metric_where = [*where_clauses, column.is_not(None)]
    dialect = session.get_bind().dialect.name

    if dialect == "postgresql":
        row = session.execute(
            select(
                func.count(column),
                func.percentile_cont(0.50).within_group(column),
                func.percentile_cont(0.95).within_group(column),
                func.percentile_cont(0.99).within_group(column),
            ).where(*metric_where)
        ).one()
        count, p50, p95, p99 = row
    else:
        values = [
            float(value)
            for value in session.scalars(
                select(column).where(*metric_where).order_by(column.asc())
            ).all()
        ]
        count = len(values)
        p50 = _percentile_cont(values, 0.50)
        p95 = _percentile_cont(values, 0.95)
        p99 = _percentile_cont(values, 0.99)

    def rounded(value: Any) -> float | None:
        return round(float(value), 6) if value is not None else None

    return {
        "count": int(count or 0),
        "p50": rounded(p50),
        "p95": rounded(p95),
        "p99": rounded(p99),
    }


def build_performance_summary(
    *,
    pipeline: str | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    include_idle: bool = False,
) -> dict[str, Any]:
    if lookback_hours < 1 or lookback_hours > MAX_LOOKBACK_HOURS:
        raise ValueError(f"lookback_hours must be between 1 and {MAX_LOOKBACK_HOURS}")
    normalized_pipeline = str(pipeline or "").strip().lower() or None
    if normalized_pipeline is not None and normalized_pipeline not in KNOWN_PIPELINES:
        raise ValueError(f"pipeline must be one of: {', '.join(sorted(KNOWN_PIPELINES))}")

    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=lookback_hours)
    where_clauses: list[Any] = [PipelinePerformanceSample.observed_at >= since]
    if normalized_pipeline is not None:
        where_clauses.append(PipelinePerformanceSample.pipeline == normalized_pipeline)
    if not include_idle:
        # The J1 cron runs every minute, and most cycles are legitimately IDLE.
        # Excluding those by default prevents no-work heartbeats from collapsing
        # active J1 p50/p95/p99 toward near-zero while failures remain visible.
        where_clauses.append(
            or_(
                PipelinePerformanceSample.pipeline != PIPELINE_J1,
                PipelinePerformanceSample.status != "IDLE",
            )
        )

    metric_columns = {
        "cycle_seconds": PipelinePerformanceSample.cycle_seconds,
        "upstream_seconds": PipelinePerformanceSample.upstream_seconds,
        "dataset_build_seconds": PipelinePerformanceSample.dataset_build_seconds,
        "fit_seconds": PipelinePerformanceSample.fit_seconds,
        "selected_fixtures": PipelinePerformanceSample.selected_fixtures,
        "http_requests": PipelinePerformanceSample.http_requests,
        "retries": PipelinePerformanceSample.retries,
    }

    with SessionLocal() as session:
        total = int(
            session.scalar(
                select(func.count(PipelinePerformanceSample.id)).where(*where_clauses)
            )
            or 0
        )
        pipeline_counts = {
            str(name): int(count)
            for name, count in session.execute(
                select(
                    PipelinePerformanceSample.pipeline,
                    func.count(PipelinePerformanceSample.id),
                )
                .where(*where_clauses)
                .group_by(PipelinePerformanceSample.pipeline)
            ).all()
        }
        status_counts = {
            str(name): int(count)
            for name, count in session.execute(
                select(
                    PipelinePerformanceSample.status,
                    func.count(PipelinePerformanceSample.id),
                )
                .where(*where_clauses)
                .group_by(PipelinePerformanceSample.status)
            ).all()
        }
        percentiles = {
            name: _metric_percentiles(session, column, where_clauses)
            for name, column in metric_columns.items()
        }
        latest = session.scalars(
            select(PipelinePerformanceSample)
            .where(*where_clauses)
            .order_by(
                PipelinePerformanceSample.observed_at.desc(),
                PipelinePerformanceSample.id.desc(),
            )
            .limit(LATEST_SAMPLE_LIMIT)
        ).all()

    return {
        "status": "ok",
        "version": PERFORMANCE_OBSERVATORY_VERSION,
        "window": {
            "lookback_hours": lookback_hours,
            "since": since.isoformat(),
            "until": until.isoformat(),
        },
        "filter": {
            "pipeline": normalized_pipeline,
            "include_idle": include_idle,
        },
        "samples": {
            "total": total,
            "by_pipeline": pipeline_counts,
            "by_status": status_counts,
            "tail_readiness": {
                "p95_recommended_min_samples": 20,
                "p99_recommended_min_samples": 100,
                "p95_ready": total >= 20,
                "p99_ready": total >= 100,
            },
        },
        "percentiles": percentiles,
        "latest": [
            {
                "id": int(row.id),
                "pipeline": row.pipeline,
                "source": row.source,
                "status": row.status,
                "run_id": row.run_id,
                "observed_at": row.observed_at.isoformat(),
                "cycle_seconds": row.cycle_seconds,
                "upstream_seconds": row.upstream_seconds,
                "dataset_build_seconds": row.dataset_build_seconds,
                "fit_seconds": row.fit_seconds,
                "selected_fixtures": int(row.selected_fixtures or 0),
                "logical_requests": int(row.logical_requests or 0),
                "http_requests": int(row.http_requests or 0),
                "retries": int(row.retries or 0),
                "rate_limited_responses": int(row.rate_limited_responses or 0),
            }
            for row in latest
        ],
        "policy": {
            "production_percentile_method": "postgresql_percentile_cont",
            "non_postgresql_test_fallback": "linear_interpolation_equivalent_to_percentile_cont",
            "telemetry_failure_breaks_pipeline": False,
            "tail_percentiles_always_include_sample_count": True,
            "raw_metrics_persisted_for_future_analysis": True,
            "idle_j1_excluded_from_percentiles_by_default": True,
            "failed_j1_cycles_remain_in_default_percentiles": True,
        },
    }


@router.get("/performance")
def performance_summary_endpoint(
    pipeline: str | None = Query(default=None),
    lookback_hours: int = Query(
        default=DEFAULT_LOOKBACK_HOURS,
        ge=1,
        le=MAX_LOOKBACK_HOURS,
    ),
    include_idle: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return build_performance_summary(
            pipeline=pipeline,
            lookback_hours=lookback_hours,
            include_idle=include_idle,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
