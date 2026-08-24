from __future__ import annotations

import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.internal_endpoint_auth import (
    GITHUB_MAIN_REF,
    GITHUB_REPOSITORY,
    authorize_mutating_request,
    _workflow_claim_allowed,
)


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }
    )


class InternalEndpointAuthTests(unittest.TestCase):
    def test_safe_get_remains_public(self) -> None:
        decision = authorize_mutating_request(_request("GET", "/health"))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.mechanism, "safe_method")

    def test_mutating_request_without_auth_is_denied(self) -> None:
        request = _request("POST", "/backfill/historical/controller")
        with patch("app.internal_endpoint_auth._internal_key_matches", return_value=False), patch(
            "app.internal_endpoint_auth._github_oidc_allowed", return_value=False
        ):
            decision = authorize_mutating_request(request)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "INTERNAL_AUTH_REQUIRED")

    def test_internal_key_can_authorize_any_mutating_route(self) -> None:
        request = _request("POST", "/research/future-batch/run")
        with patch("app.internal_endpoint_auth._internal_key_matches", return_value=True):
            decision = authorize_mutating_request(request)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.mechanism, "internal_api_key")

    def test_daily_sync_oidc_claims_are_bound_to_exact_workflow_and_main(self) -> None:
        claims = {
            "repository": GITHUB_REPOSITORY,
            "ref": GITHUB_MAIN_REF,
            "event_name": "schedule",
            "workflow_ref": (
                f"{GITHUB_REPOSITORY}/.github/workflows/"
                f"daily-operations-sync.yml@{GITHUB_MAIN_REF}"
            ),
        }
        self.assertTrue(_workflow_claim_allowed(path="/operations/daily-sync", claims=claims))

        wrong_ref = dict(claims, ref="refs/heads/feature")
        self.assertFalse(_workflow_claim_allowed(path="/operations/daily-sync", claims=wrong_ref))

        wrong_workflow = dict(
            claims,
            workflow_ref=(
                f"{GITHUB_REPOSITORY}/.github/workflows/"
                f"daily-prediction-runner-v1.yml@{GITHUB_MAIN_REF}"
            ),
        )
        self.assertFalse(_workflow_claim_allowed(path="/operations/daily-sync", claims=wrong_workflow))

    def test_settlement_oidc_claims_are_bound_to_exact_workflow_and_route(self) -> None:
        claims = {
            "repository": GITHUB_REPOSITORY,
            "ref": GITHUB_MAIN_REF,
            "event_name": "schedule",
            "workflow_ref": (
                f"{GITHUB_REPOSITORY}/.github/workflows/"
                f"forward-test-settlement-runner-v1.yml@{GITHUB_MAIN_REF}"
            ),
        }
        settlement_path = "/research/forward-test/settle/pending"
        self.assertTrue(_workflow_claim_allowed(path=settlement_path, claims=claims))

        wrong_workflow = dict(
            claims,
            workflow_ref=(
                f"{GITHUB_REPOSITORY}/.github/workflows/"
                f"daily-operations-sync.yml@{GITHUB_MAIN_REF}"
            ),
        )
        self.assertFalse(_workflow_claim_allowed(path=settlement_path, claims=wrong_workflow))
        self.assertFalse(_workflow_claim_allowed(path="/research/future-batch/run", claims=claims))

    def test_oidc_is_not_accepted_for_arbitrary_mutating_endpoint(self) -> None:
        claims = {
            "repository": GITHUB_REPOSITORY,
            "ref": GITHUB_MAIN_REF,
            "event_name": "workflow_dispatch",
            "workflow_ref": (
                f"{GITHUB_REPOSITORY}/.github/workflows/"
                f"daily-operations-sync.yml@{GITHUB_MAIN_REF}"
            ),
        }
        self.assertFalse(
            _workflow_claim_allowed(path="/backfill/historical/controller", claims=claims)
        )


if __name__ == "__main__":
    unittest.main()
