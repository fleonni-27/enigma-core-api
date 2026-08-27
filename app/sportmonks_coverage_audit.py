from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, Query

from app.sportmonks import SportmonksClient

router = APIRouter(tags=["Research Sportmonks Audit"])
AUDIT_VERSION = "sportmonks_coverage_audit_v1"


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    league = row.get("league") if isinstance(row.get("league"), dict) else None
    return {
        "id": row.get("id"),
        "league_id": row.get("league_id"),
        "league_name": (league or {}).get("name") if league else None,
        "season_id": row.get("season_id"),
        "name": row.get("name"),
        "starting_at": row.get("starting_at"),
        "has_odds": row.get("has_odds"),
        "placeholder": row.get("placeholder"),
    }


def _error_summary(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        message = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error")
        except Exception:
            message = None
        return {
            "status": "http_error",
            "http_status": int(response.status_code),
            "message": str(message)[:300] if message else None,
        }
    return {"status": "error", "error": exc.__class__.__name__, "message": str(exc)[:300]}


async def _call(client: SportmonksClient, *, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{client.settings.sportmonks_base_url}{path}"
    query = {
        "api_token": client.settings.sportmonks_api_token,
        "include": "participants;league",
        "per_page": 100,
        **(params or {}),
    }
    try:
        async with client._client_scope() as (http_client, pooled):
            payload = await client._get_json(
                http_client,
                pooled=pooled,
                url=url,
                params=query,
                timeout=30.0,
            )
        data = payload.get("data")
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        return {
            "status": "ok",
            "count": len(rows),
            "items": [_row_summary(row) for row in rows[:100] if isinstance(row, dict)],
            "pagination": payload.get("pagination") or (payload.get("meta") or {}).get("pagination"),
        }
    except Exception as exc:
        return _error_summary(exc)


@router.get("/research/sportmonks/coverage-audit")
async def sportmonks_coverage_audit(
    target_date: date = Query(default=date(2026, 8, 26)),
) -> dict[str, Any]:
    next_date = target_date + timedelta(days=1)
    searches = {
        "vasco_vitoria": "Vasco vs Vitória",
        "palmeiras_santos": "Palmeiras vs Santos",
        "river_santa_fe": "River Plate vs Independiente Santa Fe",
    }

    async with SportmonksClient() as client:
        checks: dict[str, Any] = {}
        checks["date_target"] = await _call(
            client, path=f"/fixtures/date/{target_date.isoformat()}"
        )
        checks["date_next_utc_bucket"] = await _call(
            client, path=f"/fixtures/date/{next_date.isoformat()}"
        )
        checks["between_target_next"] = await _call(
            client,
            path=f"/fixtures/between/{target_date.isoformat()}/{next_date.isoformat()}",
        )
        checks["copa_do_brasil_league_654"] = await _call(
            client, path="/leagues/654", params={"include": "currentSeason"}
        )
        for key, search_name in searches.items():
            checks[f"search_{key}"] = await _call(
                client,
                path=f"/fixtures/search/{quote(search_name, safe='')}",
            )

        all_items: list[dict[str, Any]] = []
        for check in checks.values():
            if isinstance(check, dict):
                all_items.extend(check.get("items") or [])

        needles = ("vasco", "vitória", "vitoria", "palmeiras", "santos", "river plate", "santa fe")
        matching = []
        seen: set[Any] = set()
        for item in all_items:
            name = str(item.get("name") or "").casefold()
            if not any(needle in name for needle in needles):
                continue
            identity = item.get("id") or (item.get("name"), item.get("starting_at"))
            if identity in seen:
                continue
            seen.add(identity)
            matching.append(item)

        return {
            "status": "ok",
            "version": AUDIT_VERSION,
            "target_date": target_date.isoformat(),
            "next_date": next_date.isoformat(),
            "checks": checks,
            "matching_fixtures": matching,
            "transport_audit": client.transport_audit(),
            "policy": {
                "research_only": True,
                "api_token_exposed": False,
                "no_database_mutation": True,
                "no_prediction_or_decision_created": True,
            },
        }


def install_one_shot_startup_audit(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _sportmonks_one_shot_audit() -> None:
        result = await sportmonks_coverage_audit(target_date=date(2026, 8, 26))
        print(
            "SPORTMONKS_COVERAGE_AUDIT_RESULT "
            + json.dumps(result, ensure_ascii=False, default=str),
            flush=True,
        )
