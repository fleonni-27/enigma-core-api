from __future__ import annotations

from calendar import monthrange
from datetime import date

from app.backfill import backfill_fixtures
from app.data_backfill import backfill_fixture_data

MAX_MONTHS_PER_RUN = 6


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
        window_start = max(current, start_date)
        window_end = min(_month_end(current), end_date)
        windows.append((window_start, window_end))
        current = _next_month(current)
    return windows


async def backfill_monthly(start_date: date, end_date: date, leagues: list[str] | None = None, enrich_data: bool = False, data_limit_per_month: int = 25, skip_existing: bool = True) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if data_limit_per_month < 1 or data_limit_per_month > 25:
        raise ValueError("data_limit_per_month must be between 1 and 25")

    windows = _month_windows(start_date, end_date)
    if len(windows) > MAX_MONTHS_PER_RUN:
        raise ValueError(f"maximum {MAX_MONTHS_PER_RUN} calendar months per request; split longer historical ranges into multiple requests")

    months: list[dict] = []
    totals = {"months": len(windows), "fixture_days_completed": 0, "fixture_days_failed": 0, "fixtures_received": 0, "fixtures_created": 0, "fixtures_updated": 0, "data_selected": 0, "data_completed": 0, "data_failed": 0, "lineups_total": 0, "statistics_total": 0, "xg_total": 0}
    overall_partial = False

    for window_start, window_end in windows:
        fixture_result = await backfill_fixtures(window_start, window_end)
        fixture_totals = fixture_result.get("totals") or {}
        totals["fixture_days_completed"] += int(fixture_totals.get("completed_days", 0))
        totals["fixture_days_failed"] += int(fixture_totals.get("failed_days", 0))
        totals["fixtures_received"] += int(fixture_totals.get("received", 0))
        totals["fixtures_created"] += int(fixture_totals.get("created", 0))
        totals["fixtures_updated"] += int(fixture_totals.get("updated", 0))

        month_result: dict = {"month": window_start.strftime("%Y-%m"), "start_date": window_start.isoformat(), "end_date": window_end.isoformat(), "fixtures": {"status": fixture_result.get("status"), "totals": fixture_totals}}
        if fixture_result.get("status") != "ok":
            overall_partial = True

        if enrich_data:
            data_result = await backfill_fixture_data(start_date=window_start, end_date=window_end, leagues=leagues, limit=data_limit_per_month, skip_existing=skip_existing)
            month_result["data"] = data_result
            totals["data_selected"] += int(data_result.get("selected_fixtures", 0))
            totals["data_completed"] += int(data_result.get("completed", 0))
            totals["data_failed"] += int(data_result.get("failed", 0))
            totals["lineups_total"] += int(data_result.get("lineups_total", 0))
            totals["statistics_total"] += int(data_result.get("statistics_total", 0))
            totals["xg_total"] += int(data_result.get("xg_total", 0))
            if data_result.get("status") not in {"ok", None}:
                overall_partial = True

        months.append(month_result)

    return {"status": "partial" if overall_partial else "ok", "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "max_months_per_request": MAX_MONTHS_PER_RUN, "leagues": leagues or [], "enrich_data": enrich_data, "data_limit_per_month": data_limit_per_month, "skip_existing": skip_existing, "totals": totals, "months": months}
