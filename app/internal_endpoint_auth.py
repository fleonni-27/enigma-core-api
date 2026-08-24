from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jwt import PyJWKClient

from app.config import get_settings

INTERNAL_ENDPOINT_AUTH_VERSION = "internal_endpoint_auth_v2"
INTERNAL_KEY_HEADER = "x-enigma-internal-key"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = f"{GITHUB_OIDC_ISSUER}/.well-known/jwks"
GITHUB_OIDC_AUDIENCE = "enigma-core-api"
GITHUB_REPOSITORY = "fleonni-27/enigma-core-api"
GITHUB_MAIN_REF = "refs/heads/main"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# GitHub OIDC is restricted to the exact automation route/workflow pairs below.
# Every other mutating endpoint still requires the private internal API key.
GITHUB_AUTOMATION_WORKFLOWS: dict[str, str] = {
    "/operations/daily-sync": ".github/workflows/daily-operations-sync.yml",
    "/operations/daily-prediction-runner": ".github/workflows/daily-prediction-runner-v1.yml",
    "/research/forward-test/settle/pending": ".github/workflows/forward-test-settlement-runner-v1.yml",
}

_jwk_client = PyJWKClient(GITHUB_OIDC_JWKS_URL, cache_keys=True)
_installed = False


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    mechanism: str | None = None
    reason: str | None = None


def _internal_key_matches(request: Request) -> bool:
    configured = (get_settings().internal_api_key or "").strip()
    supplied = (request.headers.get(INTERNAL_KEY_HEADER) or "").strip()
    if not configured or not supplied:
        return False
    return hmac.compare_digest(configured, supplied)


def _bearer_token(request: Request) -> str | None:
    header = (request.headers.get("authorization") or "").strip()
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


def _workflow_claim_allowed(*, path: str, claims: dict[str, Any]) -> bool:
    expected_workflow = GITHUB_AUTOMATION_WORKFLOWS.get(path)
    if expected_workflow is None:
        return False

    if str(claims.get("repository") or "") != GITHUB_REPOSITORY:
        return False
    if str(claims.get("ref") or "") != GITHUB_MAIN_REF:
        return False
    if str(claims.get("event_name") or "") not in {"schedule", "workflow_dispatch"}:
        return False

    workflow_ref = str(claims.get("workflow_ref") or "")
    expected_ref = f"{GITHUB_REPOSITORY}/{expected_workflow}@{GITHUB_MAIN_REF}"
    return hmac.compare_digest(workflow_ref, expected_ref)


def _github_oidc_allowed(request: Request) -> bool:
    path = request.url.path.rstrip("/") or "/"
    if path not in GITHUB_AUTOMATION_WORKFLOWS:
        return False

    token = _bearer_token(request)
    if token is None:
        return False

    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=GITHUB_OIDC_AUDIENCE,
            issuer=GITHUB_OIDC_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "repository", "ref", "workflow_ref"]},
        )
    except Exception:
        return False

    return _workflow_claim_allowed(path=path, claims=claims)


def authorize_mutating_request(request: Request) -> AuthDecision:
    if request.method.upper() in SAFE_METHODS:
        return AuthDecision(True, mechanism="safe_method")

    if _internal_key_matches(request):
        return AuthDecision(True, mechanism="internal_api_key")

    if _github_oidc_allowed(request):
        return AuthDecision(True, mechanism="github_oidc")

    return AuthDecision(False, reason="INTERNAL_AUTH_REQUIRED")


def install_internal_endpoint_auth(app: FastAPI) -> None:
    global _installed
    if _installed:
        return

    @app.middleware("http")
    async def internal_endpoint_auth_middleware(request: Request, call_next):
        decision = authorize_mutating_request(request)
        if not decision.allowed:
            return JSONResponse(
                status_code=401,
                content={
                    "status": "unauthorized",
                    "version": INTERNAL_ENDPOINT_AUTH_VERSION,
                    "reason": decision.reason,
                    "policy": {
                        "safe_methods_public": sorted(SAFE_METHODS),
                        "mutating_methods_require_internal_auth": True,
                        "github_oidc_limited_to_automation_routes": True,
                        "github_repository": GITHUB_REPOSITORY,
                    },
                },
            )
        response = await call_next(request)
        response.headers["X-Enigma-Auth-Version"] = INTERNAL_ENDPOINT_AUTH_VERSION
        return response

    _installed = True
