from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from fastapi import FastAPI

AUDIT_MARKER = "NEW_PROVIDER_ENV_AUDIT_RESULT"


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


def install_one_shot_new_provider_env_audit(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _run_once() -> None:
        print(AUDIT_MARKER + " " + json.dumps(sanitized_environment_audit(), ensure_ascii=False), flush=True)
