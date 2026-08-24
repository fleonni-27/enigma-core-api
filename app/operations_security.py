from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

OPERATIONS_TOKEN_ENV = "ENIGMA_OPERATIONS_TOKEN"
OPERATIONS_TOKEN_HEADER = "X-Enigma-Operations-Token"

_PROTECTED_EXACT_PATHS = {
    "/research/future-batch/run",
    "/research/forward-test/settle/pending",
}
_PROTECTED_PREFIXES = (
    "/research/forward-test/settle/fixture/",
)


def _is_protected_operations_request(request: Request) -> bool:
    if request.method.upper() != "POST":
        return False
    path = request.url.path
    if path in _PROTECTED_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PROTECTED_PREFIXES)


def operations_security_status() -> dict[str, Any]:
    expected = str(os.getenv(OPERATIONS_TOKEN_ENV) or "").strip()
    app_env = str(os.getenv("APP_ENV") or "").strip().lower()
    return {
        "token_configured": bool(expected),
        "production_fail_closed": app_env == "production",
        "header": OPERATIONS_TOKEN_HEADER,
        "protected_exact_paths": sorted(_PROTECTED_EXACT_PATHS),
        "protected_prefixes": list(_PROTECTED_PREFIXES),
    }


def validate_operations_request(request: Request) -> JSONResponse | None:
    if not _is_protected_operations_request(request):
        return None

    expected = str(os.getenv(OPERATIONS_TOKEN_ENV) or "").strip()
    app_env = str(os.getenv("APP_ENV") or "").strip().lower()

    if not expected:
        if app_env == "production":
            return JSONResponse(
                status_code=503,
                content={
                    "status": "failed",
                    "reason_codes": ["OPERATIONS_TOKEN_NOT_CONFIGURED"],
                    "execution_mode": "RESEARCH_ONLY",
                },
            )
        return None

    supplied = str(request.headers.get(OPERATIONS_TOKEN_HEADER) or "").strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        return JSONResponse(
            status_code=401,
            content={
                "status": "unauthorized",
                "reason_codes": ["INVALID_OPERATIONS_TOKEN"],
                "execution_mode": "RESEARCH_ONLY",
            },
            headers={"WWW-Authenticate": "EnigmaOperationsToken"},
        )

    return None
