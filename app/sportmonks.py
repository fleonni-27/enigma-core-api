from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any, AsyncIterator

import httpx

from app.config import get_settings


MAX_FIXTURE_PAGES_PER_DATE = 100
FIXTURE_PAGE_SIZE = 50
SPORTMONKS_HTTP_MAX_CONNECTIONS = 12
SPORTMONKS_HTTP_MAX_KEEPALIVE_CONNECTIONS = 6
SPORTMONKS_HTTP_KEEPALIVE_EXPIRY_SECONDS = 30.0


def _pagination(payload: dict[str, Any]) -> dict[str, Any]:
    direct = payload.get("pagination")
    if isinstance(direct, dict):
        return direct

    meta = payload.get("meta")
    if isinstance(meta, dict):
        nested = meta.get("pagination")
        if isinstance(nested, dict):
            return nested

    return {}


class SportmonksClient:
    """Sportmonks API client with optional cycle-local connection pooling.

    Existing call sites remain compatible: when the client is not used as an
    async context manager, each public method owns and closes a temporary
    AsyncClient. Hot paths can use ``async with SportmonksClient()`` so every
    request in the cycle shares one keep-alive pool and TLS connections.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: httpx.AsyncClient | None = None
        self._requests = 0
        self._pooled_requests = 0
        self._temporary_sessions = 0

    @staticmethod
    def _limits() -> httpx.Limits:
        return httpx.Limits(
            max_connections=SPORTMONKS_HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=SPORTMONKS_HTTP_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=SPORTMONKS_HTTP_KEEPALIVE_EXPIRY_SECONDS,
        )

    @classmethod
    def _new_http_client(cls) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=45.0,
            limits=cls._limits(),
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "SportmonksClient":
        if self._client is None:
            self._client = self._new_http_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    @asynccontextmanager
    async def _client_scope(self) -> AsyncIterator[tuple[httpx.AsyncClient, bool]]:
        if self._client is not None:
            yield self._client, True
            return

        self._temporary_sessions += 1
        async with self._new_http_client() as client:
            yield client, False

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        *,
        pooled: bool,
        url: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict:
        self._requests += 1
        if pooled:
            self._pooled_requests += 1
        response = await client.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def transport_audit(self) -> dict[str, Any]:
        return {
            "requests": int(self._requests),
            "pooled_requests": int(self._pooled_requests),
            "temporary_sessions": int(self._temporary_sessions),
            "managed_pool_active": self._client is not None,
            "max_connections": SPORTMONKS_HTTP_MAX_CONNECTIONS,
            "max_keepalive_connections": SPORTMONKS_HTTP_MAX_KEEPALIVE_CONNECTIONS,
            "keepalive_expiry_seconds": SPORTMONKS_HTTP_KEEPALIVE_EXPIRY_SECONDS,
        }

    async def fixtures_by_date(self, target_date: date) -> dict:
        url = f"{self.settings.sportmonks_base_url}/fixtures/date/{target_date.isoformat()}"
        base_params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": "participants;league",
            "per_page": FIXTURE_PAGE_SIZE,
        }

        all_rows: list[dict[str, Any]] = []
        seen_fixture_ids: set[int] = set()
        first_payload: dict[str, Any] | None = None
        last_pagination: dict[str, Any] = {}
        page = 1

        async with self._client_scope() as (client, pooled):
            while page <= MAX_FIXTURE_PAGES_PER_DATE:
                params = {**base_params, "page": page}
                payload = await self._get_json(
                    client,
                    pooled=pooled,
                    url=url,
                    params=params,
                    timeout=30.0,
                )

                if first_payload is None:
                    first_payload = payload

                rows = payload.get("data") or []
                if not isinstance(rows, list):
                    raise ValueError("Sportmonks fixtures response 'data' must be a list")

                new_rows = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    fixture_id = row.get("id")
                    if fixture_id is None:
                        all_rows.append(row)
                        new_rows += 1
                        continue
                    try:
                        normalized_id = int(fixture_id)
                    except (TypeError, ValueError):
                        all_rows.append(row)
                        new_rows += 1
                        continue
                    if normalized_id in seen_fixture_ids:
                        continue
                    seen_fixture_ids.add(normalized_id)
                    all_rows.append(row)
                    new_rows += 1

                pagination = _pagination(payload)
                last_pagination = pagination
                has_more = bool(pagination.get("has_more"))

                if not has_more:
                    break

                if new_rows == 0:
                    raise RuntimeError(
                        f"Sportmonks fixture pagination stalled on {target_date.isoformat()} page {page}"
                    )

                page += 1
            else:
                raise RuntimeError(
                    f"Sportmonks fixture pagination exceeded {MAX_FIXTURE_PAGES_PER_DATE} pages "
                    f"for {target_date.isoformat()}"
                )

        result = dict(first_payload or {})
        result["data"] = all_rows
        result["pagination"] = {
            **last_pagination,
            "aggregated": True,
            "pages_fetched": page,
            "aggregated_count": len(all_rows),
            "has_more": False,
        }
        return result

    async def prematch_odds_by_fixture(self, fixture_id: int) -> dict:
        url = f"{self.settings.sportmonks_base_url}/odds/pre-match/fixtures/{fixture_id}"
        params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": "market;bookmaker",
        }
        async with self._client_scope() as (client, pooled):
            return await self._get_json(
                client,
                pooled=pooled,
                url=url,
                params=params,
                timeout=30.0,
            )

    async def enriched_fixture(self, fixture_id: int) -> dict:
        url = f"{self.settings.sportmonks_base_url}/fixtures/{fixture_id}"
        params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": (
                "participants;league;lineups.player;lineups.details.type;"
                "statistics.type;xGFixture.type"
            ),
        }
        async with self._client_scope() as (client, pooled):
            return await self._get_json(
                client,
                pooled=pooled,
                url=url,
                params=params,
                timeout=45.0,
            )

    async def fixture_result(self, fixture_id: int) -> dict:
        url = f"{self.settings.sportmonks_base_url}/fixtures/{fixture_id}"
        params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": "scores;state;participants",
        }
        async with self._client_scope() as (client, pooled):
            return await self._get_json(
                client,
                pooled=pooled,
                url=url,
                params=params,
                timeout=30.0,
            )


# The production app composes the research router through future_batch. Installing
# sub-routers here keeps that legacy composition backward-compatible without
# introducing a second top-level FastAPI app dependency.
from app.outcome_settlement import install_outcome_settlement_routes
from app.outcome_score_capture import install_outcome_score_capture
from app.dashboard import install_dashboard_routes
from app.dashboard_operations import install_dashboard_operations_routes
from app.dashboard_selection_clarity import install_dashboard_selection_clarity

install_outcome_settlement_routes()
install_outcome_score_capture()
install_dashboard_routes()
install_dashboard_operations_routes()
install_dashboard_selection_clarity()
