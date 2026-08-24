from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator

import httpx

from app.config import get_settings


MAX_FIXTURE_PAGES_PER_DATE = 100
FIXTURE_PAGE_SIZE = 50
SPORTMONKS_HTTP_MAX_CONNECTIONS = 12
SPORTMONKS_HTTP_MAX_KEEPALIVE_CONNECTIONS = 6
SPORTMONKS_HTTP_KEEPALIVE_EXPIRY_SECONDS = 30.0

SPORTMONKS_MAX_ATTEMPTS = 4
SPORTMONKS_BACKOFF_BASE_SECONDS = 0.5
SPORTMONKS_BACKOFF_MAX_SECONDS = 8.0
SPORTMONKS_BACKOFF_JITTER_RATIO = 0.25
SPORTMONKS_MAX_RETRY_AFTER_SECONDS = 60.0
SPORTMONKS_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


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


def _retry_after_seconds(
    response: httpx.Response,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse Retry-After seconds or HTTP-date without guessing provider semantics."""

    raw = response.headers.get("Retry-After")
    if not raw:
        return None

    try:
        value = float(raw.strip())
        return max(0.0, value)
    except (TypeError, ValueError):
        pass

    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (retry_at.astimezone(timezone.utc) - current).total_seconds())


def _remaining_quota(response: httpx.Response) -> int | None:
    for name in ("RateLimit-Remaining", "X-RateLimit-Remaining"):
        raw = response.headers.get(name)
        if raw is None:
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _rate_limit_reset_seconds(
    response: httpx.Response,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse common reset headers conservatively.

    RateLimit-Reset is treated as seconds-until-reset. X-RateLimit-Reset is
    commonly a Unix epoch; small values are accepted as seconds for compatibility.
    """

    current = now or datetime.now(timezone.utc)
    raw = response.headers.get("RateLimit-Reset")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass

    raw = response.headers.get("X-RateLimit-Reset")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    now_epoch = current.timestamp()
    if value > now_epoch - 1.0:
        return max(0.0, value - now_epoch)
    return max(0.0, value)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with bounded positive jitter.

    ``attempt`` is the 1-based failed attempt. Attempt 1 sleeps around 0.5s,
    attempt 2 around 1s, etc., capped before jitter.
    """

    base = min(
        SPORTMONKS_BACKOFF_MAX_SECONDS,
        SPORTMONKS_BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)),
    )
    jitter = base * SPORTMONKS_BACKOFF_JITTER_RATIO * random.random()
    return base + jitter


class SportmonksClient:
    """Sportmonks API client with pooling, bounded retries and shared cooldown.

    Existing call sites remain compatible: when the client is not used as an
    async context manager, each public method owns and closes a temporary
    AsyncClient. Hot paths can use ``async with SportmonksClient()`` so every
    request in the cycle shares one keep-alive pool and one rate-limit state.

    Only idempotent GET requests are retried. 429/transient 5xx/transport errors
    use exponential backoff with jitter. Retry-After is honored when present.
    A 429 or explicit remaining=0 response also creates a client-wide cooldown so
    concurrent J1/daily requests do not immediately stampede the upstream API.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: httpx.AsyncClient | None = None

        # Transport counters. ``requests`` remains actual HTTP attempts for
        # backward compatibility; logical_requests counts caller-level GETs.
        self._logical_requests = 0
        self._requests = 0
        self._pooled_requests = 0
        self._temporary_sessions = 0
        self._retries = 0
        self._retryable_status_responses = 0
        self._rate_limited_responses = 0
        self._transport_errors = 0
        self._retry_after_honored = 0
        self._retry_after_too_long = 0
        self._backoff_sleep_seconds = 0.0
        self._rate_limit_sleep_seconds = 0.0
        self._rate_limit_deferrals = 0
        self._proactive_rate_limit_cooldowns = 0
        self._last_retry_status: int | None = None

        # Shared per-client cooldown used by all concurrent requests in a cycle.
        self._rate_limit_lock = asyncio.Lock()
        self._next_request_not_before = 0.0

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

    async def _set_rate_limit_cooldown(self, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._rate_limit_lock:
            self._next_request_not_before = max(
                self._next_request_not_before,
                loop.time() + delay_seconds,
            )

    async def _wait_for_rate_limit_window(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._rate_limit_lock:
            remaining = self._next_request_not_before - loop.time()
            if remaining <= 0:
                return
            self._rate_limit_deferrals += 1
            self._rate_limit_sleep_seconds += remaining
            await asyncio.sleep(remaining)

    async def _sleep_for_retry(self, delay_seconds: float, *, rate_limited: bool) -> None:
        if delay_seconds <= 0:
            return
        self._backoff_sleep_seconds += delay_seconds
        if rate_limited:
            await self._set_rate_limit_cooldown(delay_seconds)
        await asyncio.sleep(delay_seconds)

    async def _apply_success_rate_limit_headers(self, response: httpx.Response) -> None:
        remaining = _remaining_quota(response)
        if remaining is None or remaining > 0:
            return

        delay = _rate_limit_reset_seconds(response)
        if delay is None:
            delay = _retry_after_seconds(response)
        if delay is None or delay <= 0:
            return
        # Do not hold an active cycle for an unexpectedly huge reset window.
        # The current request succeeded; future requests will naturally receive
        # provider feedback if the reset is too far away for this cycle.
        delay = min(delay, SPORTMONKS_MAX_RETRY_AFTER_SECONDS)
        self._proactive_rate_limit_cooldowns += 1
        await self._set_rate_limit_cooldown(delay)

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        *,
        pooled: bool,
        url: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict:
        self._logical_requests += 1
        last_transport_error: httpx.RequestError | None = None

        for attempt in range(1, SPORTMONKS_MAX_ATTEMPTS + 1):
            await self._wait_for_rate_limit_window()
            self._requests += 1
            if pooled:
                self._pooled_requests += 1

            try:
                response = await client.get(url, params=params, timeout=timeout)
            except httpx.RequestError as exc:
                self._transport_errors += 1
                last_transport_error = exc
                if attempt >= SPORTMONKS_MAX_ATTEMPTS:
                    raise
                self._retries += 1
                await self._sleep_for_retry(
                    _backoff_seconds(attempt),
                    rate_limited=False,
                )
                continue

            status = int(response.status_code)
            if status < 400:
                await self._apply_success_rate_limit_headers(response)
                return response.json()

            retryable = status in SPORTMONKS_RETRYABLE_STATUS_CODES
            if not retryable or attempt >= SPORTMONKS_MAX_ATTEMPTS:
                response.raise_for_status()

            self._retryable_status_responses += 1
            self._last_retry_status = status
            rate_limited = status == 429
            if rate_limited:
                self._rate_limited_responses += 1

            delay = _backoff_seconds(attempt)
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                if retry_after > SPORTMONKS_MAX_RETRY_AFTER_SECONDS:
                    # A long provider cooldown is better surfaced to the caller
                    # than holding J1/daily execution and retrying too early.
                    self._retry_after_too_long += 1
                    response.raise_for_status()
                delay = max(delay, retry_after)
                self._retry_after_honored += 1

            self._retries += 1
            await self._sleep_for_retry(delay, rate_limited=rate_limited)

        if last_transport_error is not None:
            raise last_transport_error
        raise RuntimeError("Sportmonks retry loop exhausted unexpectedly")

    def transport_audit(self) -> dict[str, Any]:
        return {
            "version": "sportmonks_transport_resilience_v1",
            "logical_requests": int(self._logical_requests),
            "requests": int(self._requests),
            "pooled_requests": int(self._pooled_requests),
            "temporary_sessions": int(self._temporary_sessions),
            "managed_pool_active": self._client is not None,
            "max_connections": SPORTMONKS_HTTP_MAX_CONNECTIONS,
            "max_keepalive_connections": SPORTMONKS_HTTP_MAX_KEEPALIVE_CONNECTIONS,
            "keepalive_expiry_seconds": SPORTMONKS_HTTP_KEEPALIVE_EXPIRY_SECONDS,
            "retry": {
                "max_attempts": SPORTMONKS_MAX_ATTEMPTS,
                "retryable_status_codes": sorted(SPORTMONKS_RETRYABLE_STATUS_CODES),
                "retries": int(self._retries),
                "retryable_status_responses": int(self._retryable_status_responses),
                "transport_errors": int(self._transport_errors),
                "backoff_sleep_seconds": round(self._backoff_sleep_seconds, 6),
                "last_retry_status": self._last_retry_status,
                "base_seconds": SPORTMONKS_BACKOFF_BASE_SECONDS,
                "max_seconds": SPORTMONKS_BACKOFF_MAX_SECONDS,
                "jitter_ratio": SPORTMONKS_BACKOFF_JITTER_RATIO,
            },
            "rate_limit": {
                "responses_429": int(self._rate_limited_responses),
                "retry_after_honored": int(self._retry_after_honored),
                "retry_after_too_long": int(self._retry_after_too_long),
                "shared_cooldown_deferrals": int(self._rate_limit_deferrals),
                "shared_cooldown_sleep_seconds": round(self._rate_limit_sleep_seconds, 6),
                "proactive_zero_remaining_cooldowns": int(
                    self._proactive_rate_limit_cooldowns
                ),
                "max_retry_after_seconds": SPORTMONKS_MAX_RETRY_AFTER_SECONDS,
            },
            "policy": {
                "idempotent_gets_only": True,
                "retry_after_honored": True,
                "429_shared_client_cooldown": True,
                "transient_5xx_retried": True,
                "transport_errors_retried": True,
                "non_retryable_4xx_fail_fast": True,
                "retry_avalanche_mitigated": True,
            },
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
