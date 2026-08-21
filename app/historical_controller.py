from __future__ import annotations

from calendar import monthrange
from datetime import date

from app.backfill import backfill_fixtures
from app.data_backfill import backfill_fixture_data
from app.feature_profiles import build_feature_profile_report
from app.quality_batch import build_quality_batch_report

MAX_MONTHS_PER_CONTROLLER_RUN = 3
MAX_BATCHES_PER_MONTH = 8
MAX_BATCH_SIZE = 25
MAX_REPORT_FIXTURES = 200


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _month_end(value: date) -> date:
    return value.replace(day=monthrange(value.year, value.month)[1])


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _month_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    current = _month_start(start_date)
    while current <= end_date:
        windows.append((max(current, start_date), min(_month_end(current), end_date)))
        current = _next_month(current)
    return windows


def _compact_quality(report: dict) -> dict:
    return {
        "status": report.get("status"),
        "version": report.get("version"),
        "summary": report.get("summary") or {},
        "top_blockers": report.get("top_blockers") or [],
        "top_warnings": report.get("top_warnings") or [],
        "by_league": report.get("by_league") or [],
    }


def _compact_features(report: dict) -> dict:
    return {
        "status": report.get("status"),
        "version": report.get("version"),
        "summary": report.get("summary") or {},
        "by_league": report.get("by_league") or [],
    }


async def run_historical_controller(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    batch_size: int = 25,
    max_batches_per_month: int = 4,
    ingest_fixtures: bool = True,
    skip_existing: bool = True,
    report_limit: int = 200,
) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if not skip_existing:
        raise ValueError("historical controller requires skip_existing=true to preserve idempotent resume behavior")
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if max_batches_per_month < 1 or max_batches_per_month > MAX_BATCHES_PER_MONTH:
        raise ValueError(f"max_batches_per_month must be between 1 and {MAX_BATCHES_PER_MONTH}")
    if report_limit < 1 or report_limit > MAX_REPORT_FIXTURES:
        raise ValueError(f"report_limit must be between 1 and {MAX_REPORT_FIXTURES}")

    windows = _month_windows(start_date, end_date)
    if len(windows) > MAX_MONTHS_PER_CONTROLLER_RUN:
        raise ValueError(
            f"maximum {MAX_MONTHS_PER_CONTROLLER_RUN} calendar months per controller request; "
            "use multiple resumable requests for longer historical ranges"
        )

    months: list[dict] = []
    totals = {
        "months": len(windows),
        "batches_run": 0,
        "data_selected": 0,
        "data_completed": 0,
        "data_failed": 0,
        "lineups_total": 0,
        "statistics_total": 0,
        "xg_total": 0,
    }
    overall_partial = False

    for window_start, window_end in windows:
        month_result: dict = {
            "month": window_start.strftime("%Y-%m"),
            "start_date": window_start.isoformat(),
            "end_date": window_end.isoformat(),
            "batches": [],
        }

        if ingest_fixtures:
            fixture_result = await backfill_fixtures(window_start, window_end)
            month_result["fixtures"] = {
                "status": fixture_result.get("status"),
                "totals": fixture_result.get("totals") or {},
            }
            if fixture_result.get("status") != "ok":
                overall_partial = True

        exhausted = False
        for batch_number in range(1, max_batches_per_month + 1):
            data_result = await backfill_fixture_data(
                start_date=window_start,
                end_date=window_end,
                leagues=leagues,
                limit=batch_size,
                skip_existing=True,
            )
            selected = int(data_result.get("selected_fixtures", 0))
            completed = int(data_result.get("completed", 0))
            failed = int(data_result.get("failed", 0))

            batch = {
                "batch": batch_number,
                "status": data_result.get("status"),
                "selected": selected,
                "completed": completed,
                "failed": failed,
                "lineups_total": int(data_result.get("lineups_total", 0)),
                "statistics_total": int(data_result.get("statistics_total", 0)),
                "xg_total": int(data_result.get("xg_total", 0)),
            }
            month_result["batches"].append(batch)

            totals["batches_run"] += 1
            totals["data_selected"] += selected
            totals["data_completed"] += completed
            totals["data_failed"] += failed
            totals["lineups_total"] += batch["lineups_total"]
            totals["statistics_total"] += batch["statistics_total"]
            totals["xg_total"] += batch["xg_total"]

            if failed > 0 or data_result.get("status") not in {"ok", None}:
                overall_partial = True

            if selected == 0 or selected < batch_size:
                exhausted = True
                break

        quality = build_quality_batch_report(
            start_date=window_start,
            end_date=window_end,
            leagues=leagues,
            limit=report_limit,
        )
        features = build_feature_profile_report(
            start_date=window_start,
            end_date=window_end,
            leagues=leagues,
            limit=report_limit,
        )
        month_result["quality"] = _compact_quality(quality)
        month_result["features"] = _compact_features(features)

        feature_summary = features.get("summary") or {}
        missing = int(feature_summary.get("missing_snapshots", 0) or 0)
        incomplete = int(((feature_summary.get("profiles") or {}).get("INCOMPLETE", 0)) or 0)
        selected_report_fixtures = int(features.get("selected_fixtures", 0) or 0)
        report_truncated = selected_report_fixtures >= report_limit
        month_complete = exhausted and missing == 0 and incomplete == 0 and not report_truncated

        month_result["checkpoint"] = {
            "resumable": True,
            "skip_existing": True,
            "batch_exhausted": exhausted,
            "missing_snapshots": missing,
            "incomplete_snapshots": incomplete,
            "report_truncated_or_at_limit": report_truncated,
            "month_complete": month_complete,
        }
        if not month_complete:
            overall_partial = True

        months.append(month_result)

    return {
        "status": "partial" if overall_partial else "ok",
        "version": "historical_controller_v1",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "batch_size": batch_size,
        "max_batches_per_month": max_batches_per_month,
        "ingest_fixtures": ingest_fixtures,
        "skip_existing": True,
        "report_limit": report_limit,
        "limits": {
            "max_months_per_request": MAX_MONTHS_PER_CONTROLLER_RUN,
            "max_batches_per_month": MAX_BATCHES_PER_MONTH,
            "max_batch_size": MAX_BATCH_SIZE,
            "max_report_fixtures": MAX_REPORT_FIXTURES,
        },
        "totals": totals,
        "months": months,
        "policy": {
            "checkpoint_strategy": "database_state",
            "resume_strategy": "rerun_same_window_with_skip_existing=true",
            "quality_after_each_month": True,
            "feature_profile_after_each_month": True,
            "xg_absence_is_zero": False,
            "report_limit_safety": "a month is never marked complete when the audit report reaches its limit",
        },
    }
