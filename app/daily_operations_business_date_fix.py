from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import app.daily_operations as daily_operations
from app.sportmonks import SportmonksClient

DAILY_OPERATIONS_BUSINESS_DATE_FIX_VERSION = "daily_operations_business_date_fix_v1"
BUSINESS_TIMEZONE = daily_operations.BUSINESS_TIMEZONE


def _aware_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _belongs_to_business_date(row: dict[str, Any], target_date: date) -> bool:
    starts_at = _aware_datetime(row.get("starting_at"))
    if starts_at is None:
        return False
    return starts_at.astimezone(ZoneInfo(BUSINESS_TIMEZONE)).date() == target_date


def _merge_business_date_payloads(
    *,
    target_date: date,
    payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge adjacent Sportmonks date buckets into one Sao Paulo business day.

    A Sao Paulo calendar day spans UTC 03:00 through 02:59 of the following UTC
    date. Sportmonks' date endpoint can therefore place late Brazilian kickoffs
    (for example 21:00 BRT = 00:00 UTC next day) in the following date bucket.
    """

    merged: dict[int, dict[str, Any]] = {}
    received = 0
    for payload in payloads:
        rows = list((payload or {}).get("data") or [])
        received += len(rows)
        for row in rows:
            if not isinstance(row, dict) or not _belongs_to_business_date(row, target_date):
                continue
            try:
                fixture_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            merged.setdefault(fixture_id, row)

    return {
        "data": list(merged.values()),
        "meta": {
            "business_date_fix_version": DAILY_OPERATIONS_BUSINESS_DATE_FIX_VERSION,
            "business_timezone": BUSINESS_TIMEZONE,
            "target_date": target_date.isoformat(),
            "queried_upstream_dates": [
                target_date.isoformat(),
                (target_date + timedelta(days=1)).isoformat(),
            ],
            "upstream_rows_received": received,
            "business_date_rows_selected": len(merged),
            "deduplicated_by_sportmonks_fixture_id": True,
        },
    }


class BusinessDateSportmonksClient(SportmonksClient):
    async def fixtures_by_date(self, query_date: date) -> dict:
        primary = await super().fixtures_by_date(query_date)
        following = await super().fixtures_by_date(query_date + timedelta(days=1))
        return _merge_business_date_payloads(
            target_date=query_date,
            payloads=[primary, following],
        )


def install_daily_operations_business_date_fix() -> None:
    """Scope the adjacent-date discovery fix to Daily Operations only."""

    if daily_operations.SportmonksClient is BusinessDateSportmonksClient:
        return
    daily_operations.SportmonksClient = BusinessDateSportmonksClient
