from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app import sportmonks


class _SequenceClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def get(self, url, params=None, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(status: int, *, headers=None, payload=None) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test/resource")
    return httpx.Response(
        status,
        headers=headers or {},
        json=payload if payload is not None else {"status": status},
        request=request,
    )


class SportmonksResilienceTests(unittest.TestCase):
    def _client(self) -> sportmonks.SportmonksClient:
        client = sportmonks.SportmonksClient()
        client._wait_for_rate_limit_window = AsyncMock()
        client._sleep_for_retry = AsyncMock()
        return client

    def test_transient_503_retries_then_returns_json(self) -> None:
        client = self._client()
        transport = _SequenceClient(
            [
                _response(503),
                _response(200, payload={"data": [1]}),
            ]
        )

        with patch("app.sportmonks.random.random", return_value=0.0):
            result = asyncio.run(
                client._get_json(
                    transport,
                    pooled=True,
                    url="https://example.test/resource",
                    params={},
                    timeout=1.0,
                )
            )

        self.assertEqual(result, {"data": [1]})
        self.assertEqual(transport.calls, 2)
        self.assertEqual(client.transport_audit()["retry"]["retries"], 1)
        client._sleep_for_retry.assert_awaited_once_with(0.5, rate_limited=False)

    def test_429_honors_retry_after_and_marks_shared_cooldown(self) -> None:
        client = self._client()
        transport = _SequenceClient(
            [
                _response(429, headers={"Retry-After": "2"}),
                _response(200, payload={"ok": True}),
            ]
        )

        with patch("app.sportmonks.random.random", return_value=0.0):
            result = asyncio.run(
                client._get_json(
                    transport,
                    pooled=True,
                    url="https://example.test/resource",
                    params={},
                    timeout=1.0,
                )
            )

        self.assertEqual(result, {"ok": True})
        audit = client.transport_audit()
        self.assertEqual(audit["rate_limit"]["responses_429"], 1)
        self.assertEqual(audit["rate_limit"]["retry_after_honored"], 1)
        client._sleep_for_retry.assert_awaited_once_with(2.0, rate_limited=True)

    def test_non_retryable_401_fails_fast(self) -> None:
        client = self._client()
        transport = _SequenceClient([_response(401)])

        with self.assertRaises(httpx.HTTPStatusError):
            asyncio.run(
                client._get_json(
                    transport,
                    pooled=False,
                    url="https://example.test/resource",
                    params={},
                    timeout=1.0,
                )
            )

        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.transport_audit()["retry"]["retries"], 0)
        client._sleep_for_retry.assert_not_awaited()

    def test_transport_error_retries_then_succeeds(self) -> None:
        client = self._client()
        request = httpx.Request("GET", "https://example.test/resource")
        transport = _SequenceClient(
            [
                httpx.ConnectError("temporary", request=request),
                _response(200, payload={"ok": True}),
            ]
        )

        with patch("app.sportmonks.random.random", return_value=0.0):
            result = asyncio.run(
                client._get_json(
                    transport,
                    pooled=False,
                    url="https://example.test/resource",
                    params={},
                    timeout=1.0,
                )
            )

        self.assertTrue(result["ok"])
        audit = client.transport_audit()
        self.assertEqual(audit["retry"]["transport_errors"], 1)
        self.assertEqual(audit["retry"]["retries"], 1)

    def test_retry_after_too_long_fails_instead_of_retrying_early(self) -> None:
        client = self._client()
        transport = _SequenceClient([_response(429, headers={"Retry-After": "120"})])

        with self.assertRaises(httpx.HTTPStatusError):
            asyncio.run(
                client._get_json(
                    transport,
                    pooled=False,
                    url="https://example.test/resource",
                    params={},
                    timeout=1.0,
                )
            )

        audit = client.transport_audit()
        self.assertEqual(audit["rate_limit"]["retry_after_too_long"], 1)
        self.assertEqual(audit["retry"]["retries"], 0)

    def test_retry_after_http_date_is_supported(self) -> None:
        now = datetime(2026, 8, 24, 22, 0, 0, tzinfo=timezone.utc)
        response = _response(
            429,
            headers={"Retry-After": "Mon, 24 Aug 2026 22:00:07 GMT"},
        )
        self.assertEqual(sportmonks._retry_after_seconds(response, now=now), 7.0)

    def test_rate_limit_reset_parsers_support_standard_and_x_headers(self) -> None:
        now = datetime(2026, 8, 24, 22, 0, 0, tzinfo=timezone.utc)
        standard = _response(200, headers={"RateLimit-Reset": "5"})
        epoch = _response(
            200,
            headers={"X-RateLimit-Reset": str(int(now.timestamp()) + 9)},
        )
        self.assertEqual(sportmonks._rate_limit_reset_seconds(standard, now=now), 5.0)
        self.assertEqual(sportmonks._rate_limit_reset_seconds(epoch, now=now), 9.0)

    def test_zero_remaining_success_arms_proactive_cooldown(self) -> None:
        client = self._client()
        client._set_rate_limit_cooldown = AsyncMock()
        response = _response(
            200,
            headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "3"},
        )

        asyncio.run(client._apply_success_rate_limit_headers(response))

        client._set_rate_limit_cooldown.assert_awaited_once_with(3.0)
        audit = client.transport_audit()
        self.assertEqual(audit["rate_limit"]["proactive_zero_remaining_cooldowns"], 1)


if __name__ == "__main__":
    unittest.main()
