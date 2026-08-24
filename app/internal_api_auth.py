from __future__ import annotations

import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jwt import PyJWKClient

from app.config import get_settings

INTERNAL_API_AUTH_VERSION = "internal_api_auth_v1"
INTERNAL_KEY_HEADER = "X-Enigma-Internal-Key"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "enigma-core-api"
GITHUB_REPOSITORY = "fleonni-27/enigma-core-api"
GITHUB_MAIN_REF = "refs/heads/main"

# HTTP routes that can mutate persistent state, trigger expensive upstream work,
# or produce immutable operational artifacts. Read-only/status/dashboard/model
# routes remain public.
PROTECTED_MUTATION_PREFIXES: tuple[str, ...] = (
    "/ingest/",
    "/backfill/",
    "/repair/",
    "/exceptions/upstream",
    "/inference/fixture/",
    "/operations/daily-sync",
    "/operations/daily-prediction-runner",
    "/research/forward-test/record/",
    "/research/forward-test/settle/",
    "/research/future-batch/run",
)

# GitHub Actions is allowed only on the two operational HTTP endpoints it owns.
# Other protected mutations require the internal API key.
GITHUB_WORKFLOW_ACCESS: dict[str, str] = {
    "/operations/daily-sync": (
        f"{GITHUB_REPOSITORY}/.github/workflows/daily-operations-sync.yml@{GITHUB_MAIN_REF}"
    ),
    "/operations/daily-prediction-runner": (
        f"{GITHUB_REPOSITORY}/.github/workflows/daily-prediction-runner-v1.yml@{GITHUB_MAIN_REF}"
    ),
}


@dataclass(frozen=True)
class MutationPrincipal:
    kind: str
    subject: str


class MutationAuthError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = int(status_code)
        self.code = str(code)


def is_protected_mutation(method: str, path: str) -> bool:
    method = str(method or "").upper()
    path = str(path or "")
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PROTECTED_MUTATION_PREFIXES)


def _workflow_for_path(path: str) -> str | None:
    for prefix, workflow_ref in GITHUB_WORKFLOW_ACCESS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return workflow_ref
    return None


def _internal_key_matches(candidate: str | None) -> bool:
    configured = str(get_settings().internal_api_key or "").strip()
    candidate = str(candidate or "").strip()
    if not configured or not candidate:
        return False
    return secrets.compare_digest(configured, candidate)


@lru_cache(maxsize=1)
def _github_jwk_client() -> PyJWKClient:
    return PyJWKClient(
        f"{GITHUB_OIDC_ISSUER}/.well-known/jwks",
        cache_keys=True,
        cache_jwk_set=True,
        lifespan=300,
        timeout=10,
    )


def _decode_github_oidc(token: str) -> dict[str, Any]:
    try:
        signing_key = _github_jwk_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=GITHUB_OIDC_AUDIENCE,
            issuer=GITHUB_OIDC_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except Exception as exc:  # do not leak token validation details
        raise MutationAuthError(401, "INVALID_GITHUB_OIDC_TOKEN") from exc
    return dict(claims)


def _validate_github_claims(claims: dict[str, Any], path: str) -> MutationPrincipal:
    expected_workflow = _workflow_for_path(path)
    if expected_workflow is None:
        raise MutationAuthError(403, "GITHUB_OIDC_NOT_ALLOWED_FOR_ENDPOINT")

    repository = str(claims.get("repository") or "")
    ref = str(claims.get("ref") or "")
    workflow_ref = str(claims.get("workflow_ref") or "")
    event_name = str(claims.get("event_name") or "")

    if repository != GITHUB_REPOSITORY:
        raise MutationAuthError(403, "GITHUB_REPOSITORY_NOT_ALLOWED")
    if ref != GITHUB_MAIN_REF:
        raise MutationAuthError(403, "GITHUB_REF_NOT_ALLOWED")
    if workflow_ref != expected_workflow:
        raise MutationAuthError(403, "GITHUB_WORKFLOW_NOT_ALLOWED")
    if event_name not in {"schedule", "workflow_dispatch"}:
        raise MutationAuthError(403, "GITHUB_EVENT_NOT_ALLOWED")

    return MutationPrincipal(
        kind="github_oidc",
        subject=str(claims.get("sub") or workflow_ref),
    )


def authorize_mutation_request(request: Request) -> MutationPrincipal:
    internal_key = request.headers.get(INTERNAL_KEY_HEADER)
    if _internal_key_matches(internal_key):
        return MutationPrincipal(kind="internal_key", subject="render_internal_api_key")

    authorization = str(request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            return _validate_github_claims(_decode_github_oidc(token), request.url.path)

    raise MutationAuthError(401, "MUTATION_AUTH_REQUIRED")


def install_internal_mutation_guard(app: FastAPI) -> None:
    if getattr(app.state, "internal_mutation_guard_installed", False):
        return

    @app.middleware("http")
    async def internal_mutation_guard(request: Request, call_next):
        if not is_protected_mutation(request.method, request.url.path):
            return await call_next(request)

        try:
            principal = authorize_mutation_request(request)
        except MutationAuthError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "status": "unauthorized" if exc.status_code == 401 else "forbidden",
                    "version": INTERNAL_API_AUTH_VERSION,
                    "reason_code": exc.code,
                    "path": request.url.path,
                },
                headers={"Cache-Control": "no-store"},
            )

        request.state.mutation_principal = principal
        response = await call_next(request)
        response.headers["X-Enigma-Mutation-Auth"] = principal.kind
        return response

    app.state.internal_mutation_guard_installed = True
