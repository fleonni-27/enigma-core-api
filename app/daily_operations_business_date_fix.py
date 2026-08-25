from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import app.daily_operations as daily_operations
from app.sportmonks import (
    FIXTURE_PAGE_SIZE,
    MAX_FIXTURE_PAGES_PER_DATE,
    SportmonksClient,
    _pagination,
)

DAILY_OPERATIONS_BUSINESS_DATE_FIX_VERSION = "daily_operations_business_date_fix_v2"
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
    discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge provider buckets into one Sao Paulo business day, without guessing ids."""

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
            "between_date_discovery": discovery or {"status": "not_attempted"},
        },
    }


class BusinessDateSportmonksClient(SportmonksClient):
    async def _fixtures_between_dates(self, start_date: date, end_date: date) -> dict[str, Any]:
        """Use Sportmonks' alternate between-dates fixture index as a discovery fallback."""

        url = (
            f"{self.settings.sportmonks_base_url}/fixtures/between/"
            f"{start_date.isoformat()}/{end_date.isoformat()}"
        )
        base_params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": "participants;league",
            "per_page": FIXTURE_PAGE_SIZE,
        }
        all_rows: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        page = 1

        async with self._client_scope() as (client, pooled):
            while page <= MAX_FIXTURE_PAGES_PER_DATE:
                payload = await self._get_json(
                    client,
                    pooled=pooled,
                    url=url,
                    params={**base_params, "page": page},
                    timeout=30.0,
                )
                rows = payload.get("data") or []
                if not isinstance(rows, list):
                    raise ValueError("Sportmonks fixtures-between response 'data' must be a list")
                new_rows = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        fixture_id = int(row.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if fixture_id in seen_ids:
                        continue
                    seen_ids.add(fixture_id)
                    all_rows.append(row)
                    new_rows += 1

                pagination = _pagination(payload)
                if not bool(pagination.get("has_more")):
                    break
                if new_rows == 0:
                    raise RuntimeError(
                        f"Sportmonks between-date pagination stalled on page {page}"
                    )
                page += 1
            else:
                raise RuntimeError(
                    "Sportmonks between-date pagination exceeded safety page limit"
                )

        return {
            "data": all_rows,
            "meta": {
                "pages_fetched": page,
                "row_count": len(all_rows),
            },
        }

    async def fixtures_by_date(self, query_date: date) -> dict:
        primary = await super().fixtures_by_date(query_date)
        following = await super().fixtures_by_date(query_date + timedelta(days=1))
        payloads = [primary, following]
        discovery: dict[str, Any]
        try:
            between = await self._fixtures_between_dates(
                query_date,
                query_date + timedelta(days=1),
            )
            payloads.append(between)
            discovery = {
                "status": "ok",
                **(between.get("meta") or {}),
            }
        except Exception as exc:
            # Alternate discovery must never make the canonical date sync less reliable.
            discovery = {
                "status": "unavailable",
                "error": exc.__class__.__name__,
            }

        return _merge_business_date_payloads(
            target_date=query_date,
            payloads=payloads,
            discovery=discovery,
        )


def install_daily_operations_business_date_fix() -> None:
    """Scope the provider discovery fix to Daily Operations only."""

    if daily_operations.SportmonksClient is BusinessDateSportmonksClient:
        return
    daily_operations.SportmonksClient = BusinessDateSportmonksClient
