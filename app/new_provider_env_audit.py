from __future__ import annotations

import json
import os
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import FastAPI

AUDIT_MARKER = "NEW_PROVIDER_ENV_AUDIT_RESULT"
CONNECTIVITY_MARKER = "NEW_PROVIDER_CONNECTIVITY_AUDIT_RESULT"
AUTH_MARKER = "NEW_PROVIDER_AUTH_AUDIT_RESULT"
TARGET_NAMES = ("Leagues", "Analysing_probabily_Xg", "Analysing_playerX1")


def _looks_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH"))


def _looks_relevant_name(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in ("LEAGUE", "XG", "ANALYS", "PLAYER", "ATHLETE", "SPORT", "API", "FOOTBALL"))


def _safe_meta(name: str, value: str) -> dict:
    parsed = urlparse(value) if value.startswith(("http://", "https://")) else None
    return {
        "name": name,
        "kind": "url" if parsed and parsed.scheme else ("secret" if _looks_secret_name(name) else "value"),
        "host": parsed.hostname if parsed and parsed.hostname else None,
        "scheme": parsed.scheme if parsed and parsed.scheme else None,
        "nonempty": bool(value),
        "length": len(value),
        "value_exposed": False,
    }


def sanitized_environment_audit() -> dict:
    items = []
    for name, value in sorted(os.environ.items()):
        if not _looks_relevant_name(name):
            continue
        if name in {"SPORTMONKS_API_TOKEN", "SPORTMONKS_BASE_URL"}:
            continue
        items.append(_safe_meta(name, value))
    return {
        "status": "ok",
        "version": "new_provider_env_audit_v1",
        "candidate_count": len(items),
        "candidates": items,
        "policy": {"secret_values_exposed": False, "read_only": True},
    }


def _payload_shape(payload: object) -> dict:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            sample = next((item for item in data if isinstance(item, dict)), None)
            return {
                "payload_type": "object",
                "top_level_keys": sorted(str(k) for k in payload.keys())[:40],
                "data_type": "list",
                "data_count": len(data),
                "sample_keys": sorted(str(k) for k in sample.keys())[:80] if sample else [],
            }
        if isinstance(data, dict):
            return {
                "payload_type": "object",
                "top_level_keys": sorted(str(k) for k in payload.keys())[:40],
                "data_type": "object",
                "data_count": 1,
                "sample_keys": sorted(str(k) for k in data.keys())[:80],
            }
        return {
            "payload_type": "object",
            "top_level_keys": sorted(str(k) for k in payload.keys())[:40],
            "data_type": type(data).__name__ if data is not None else None,
            "data_count": 0,
            "sample_keys": [],
        }
    if isinstance(payload, list):
        sample = next((item for item in payload if isinstance(item, dict)), None)
        return {
            "payload_type": "list",
            "top_level_keys": [],
            "data_type": "list",
            "data_count": len(payload),
            "sample_keys": sorted(str(k) for k in sample.keys())[:80] if sample else [],
        }
    return {
        "payload_type": type(payload).__name__,
        "top_level_keys": [],
        "data_type": None,
        "data_count": 0,
        "sample_keys": [],
    }


async def _safe_get(client: httpx.AsyncClient, url: str) -> dict:
    started = time.perf_counter()
    try:
        response = await client.get(url)
        result = {
            "status": "ok" if response.status_code < 400 else "http_error",
            "http_status": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "content_type": response.headers.get("content-type"),
        }
        try:
            result.update(_payload_shape(response.json()))
        except Exception:
            result.update({"payload_type": "non_json", "data_count": 0, "sample_keys": []})
        return result
    except Exception as exc:
        return {
            "status": "transport_error",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error_type": exc.__class__.__name__,
        }


async def connectivity_audit() -> dict:
    results = []
    async with httpx.AsyncClient(timeout=30.0, headers={"Accept": "application/json"}) as client:
        for name in TARGET_NAMES:
            value = os.environ.get(name, "")
            parsed = urlparse(value) if value.startswith(("http://", "https://")) else None
            item = {
                "name": name,
                "configured": bool(value),
                "host": parsed.hostname if parsed else None,
                "url_exposed": False,
            }
            if not value or not parsed or parsed.hostname != "api.sportmonks.com":
                item.update({"status": "not_tested", "reason": "missing_or_unexpected_url"})
            else:
                item.update(await _safe_get(client, value))
            results.append(item)
    return {
        "status": "ok",
        "version": "new_provider_connectivity_audit_v1",
        "results": results,
        "policy": {
            "read_only": True,
            "secret_values_exposed": False,
            "request_urls_exposed": False,
            "response_values_exposed": False,
        },
    }


async def auth_audit() -> dict:
    primary_token = os.environ.get("SPORTMONKS_API_TOKEN", "")
    results = []
    async with httpx.AsyncClient(timeout=30.0, headers={"Accept": "application/json"}) as client:
        for name in TARGET_NAMES:
            value = os.environ.get(name, "")
            parsed = urlparse(value) if value.startswith(("http://", "https://")) else None
            item = {"name": name, "configured": bool(value), "url_exposed": False}
            if not value or not parsed or parsed.hostname != "api.sportmonks.com":
                item.update({"status": "not_tested", "reason": "missing_or_unexpected_url"})
                results.append(item)
                continue
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            query_keys = sorted({key for key, _ in pairs})
            embedded_tokens = [val for key, val in pairs if key == "api_token"]
            embedded = embedded_tokens[-1] if embedded_tokens else None
            item.update({
                "query_keys": query_keys,
                "api_token_present": embedded is not None,
                "embedded_token_nonempty": bool(embedded),
                "embedded_token_matches_primary": bool(primary_token) and embedded == primary_token,
                "primary_token_configured": bool(primary_token),
            })
            item["as_configured"] = await _safe_get(client, value)

            corrected_pairs = [(k, v) for k, v in pairs if k != "api_token"]
            if primary_token:
                corrected_pairs.append(("api_token", primary_token))
            corrected = urlunparse(parsed._replace(query=urlencode(corrected_pairs, doseq=True)))
            item["with_primary_token"] = await _safe_get(client, corrected)
            results.append(item)
    return {
        "status": "ok",
        "version": "new_provider_auth_audit_v1",
        "results": results,
        "policy": {
            "read_only": True,
            "token_values_exposed": False,
            "request_urls_exposed": False,
            "response_values_exposed": False,
        },
    }


def install_one_shot_new_provider_env_audit(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _run_once() -> None:
        print(AUDIT_MARKER + " " + json.dumps(sanitized_environment_audit(), ensure_ascii=False), flush=True)
        print(CONNECTIVITY_MARKER + " " + json.dumps(await connectivity_audit(), ensure_ascii=False), flush=True)
        print(AUTH_MARKER + " " + json.dumps(await auth_audit(), ensure_ascii=False), flush=True)
