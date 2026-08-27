from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings

router = APIRouter(prefix="/providers/sportmonks", tags=["Sportmonks Enrichment"])
PROVIDER_VERSION = "sportmonks_enrichment_provider_v1"


def _normalized_provider_url(env_name: str, *, fixture_id: int | None = None) -> str:
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        raise RuntimeError(f"{env_name} is not configured")

    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname != "api.sportmonks.com":
        raise RuntimeError(f"{env_name} must point to api.sportmonks.com over HTTPS")

    path = parsed.path
    if fixture_id is not None:
        match = re.search(r"/fixtures/(\d+)(?:/)?$", path)
        if match:
            path = path[: match.start(1)] + str(int(fixture_id)) + path[match.end(1) :]
        elif "/fixtures/" not in path:
            raise RuntimeError(f"{env_name} is not a fixture endpoint template")

    query_pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "api_token"]
    query_pairs.append(("api_token", get_settings().sportmonks_api_token))
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, urlencode(query_pairs, doseq=True), parsed.fragment))


async def _get_json(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0, headers={"Accept": "application/json"}) as client:
        response = await client.get(url)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail={"provider": "sportmonks", "upstream_status": response.status_code})
    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Sportmonks returned an unexpected payload")
    return payload


def _safe_league_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "short_code": row.get("short_code"),
        "country_id": row.get("country_id"),
        "type": row.get("type"),
        "sub_type": row.get("sub_type"),
        "active": row.get("active"),
        "last_played_at": row.get("last_played_at"),
        "seasons": row.get("seasons") if isinstance(row.get("seasons"), list) else [],
    }


def _xg_matches(value: Any, path: str = "$") -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if isinstance(value, dict):
        text = " ".join(str(value.get(k) or "") for k in ("name", "type", "developer_name", "label", "description")).casefold()
        key_text = " ".join(str(k) for k in value.keys()).casefold()
        if any(token in (text + " " + key_text) for token in ("expected goals", "expected_goals", " xg", "xg_", "_xg", "xga")):
            matches.append({"path": path, "record": value})
        for key, child in value.items():
            matches.extend(_xg_matches(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(_xg_matches(child, f"{path}[{index}]"))
    return matches


@router.get("/leagues")
async def leagues(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    payload = await _get_json(_normalized_provider_url("Leagues"))
    rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    return {
        "status": "ok",
        "version": PROVIDER_VERSION,
        "provider": "sportmonks",
        "count": min(len(rows), limit),
        "leagues": [_safe_league_item(row) for row in rows[:limit] if isinstance(row, dict)],
        "pagination": payload.get("pagination"),
        "policy": {"read_only": True, "central_token_used": True, "embedded_token_ignored": True},
    }


@router.get("/fixture-analysis/{fixture_id}")
async def fixture_analysis(fixture_id: int) -> dict[str, Any]:
    payload = await _get_json(_normalized_provider_url("Analysing_probabily_Xg", fixture_id=fixture_id))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    xg_matches = _xg_matches(data)
    return {
        "status": "ok",
        "version": PROVIDER_VERSION,
        "provider": "sportmonks",
        "fixture_id": fixture_id,
        "fixture": {
            "id": data.get("id"),
            "name": data.get("name"),
            "league_id": data.get("league_id"),
            "season_id": data.get("season_id"),
            "starting_at": data.get("starting_at"),
            "participants": data.get("participants") if isinstance(data.get("participants"), list) else [],
            "lineups": data.get("lineups") if isinstance(data.get("lineups"), list) else [],
            "scores": data.get("scores") if isinstance(data.get("scores"), list) else [],
            "events": data.get("events") if isinstance(data.get("events"), list) else [],
            "details": data.get("details"),
        },
        "xg_candidates": xg_matches[:100],
        "xg_candidate_count": len(xg_matches),
        "policy": {
            "read_only": True,
            "central_token_used": True,
            "embedded_token_ignored": True,
            "xg_not_yet_used_for_decision": True,
        },
    }
