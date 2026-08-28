from __future__ import annotations

from typing import Any

import httpx

from app import daily_prediction_runner as legacy
from app.dashboard_enrichment_cache import merge_dashboard_enrichment
from app.prematch_provider_context import extract_prematch_provider_context
from app.sportmonks import SportmonksClient

PATCH_VERSION = "j1_rich_provider_context_v1"

# Sportmonks documents these as fixture includes. Only pre-match-safe relations
# are requested here. Live/post-match relations (events, scores, timeline,
# pressure, ballCoordinates, postmatchNews) are intentionally omitted.
RICH_PREMATCH_INCLUDE = (
    "participants;league;season;stage;round;group;aggregate;venue;state;weatherReport;"
    "lineups.player;lineups.details.type;lineups.xGLineup.type;expectedLineups;formations;"
    "coaches;referees;sidelined.player;prematchNews;metadata;predictions;"
    "statistics.type;xGFixture.type"
)

_FALLBACK_STATUSES = {400, 403, 422}
_installed = False


def install_j1_rich_provider_context() -> None:
    """Install process-local J1 enrichment without changing the prediction model.

    The worker keeps exactly one enriched-fixture request in the normal path. If
    the subscription rejects one of the optional rich includes, the original
    core request is used as a fail-safe. Cache persistence failures are isolated
    from Prediction/Decision execution.
    """

    global _installed
    if _installed:
        return

    original_enriched_fixture = SportmonksClient.enriched_fixture
    original_persist_lineup_context = legacy._persist_lineup_context

    async def rich_enriched_fixture(self: SportmonksClient, fixture_id: int) -> dict[str, Any]:
        url = f"{self.settings.sportmonks_base_url}/fixtures/{int(fixture_id)}"
        params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": RICH_PREMATCH_INCLUDE,
        }
        try:
            async with self._client_scope() as (client, pooled):
                payload = await self._get_json(
                    client,
                    pooled=pooled,
                    url=url,
                    params=params,
                    timeout=45.0,
                )
            if isinstance(payload, dict):
                payload["_enigma_include_profile"] = "rich_prematch"
            return payload
        except httpx.HTTPStatusError as exc:
            status = int(exc.response.status_code) if exc.response is not None else None
            if status not in _FALLBACK_STATUSES:
                raise
            payload = await original_enriched_fixture(self, int(fixture_id))
            if isinstance(payload, dict):
                payload["_enigma_include_profile"] = f"core_fallback_after_{status}"
            return payload

    def persist_lineup_and_rich_context(
        *,
        fixture,
        snapshot_window: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = original_persist_lineup_context(
            fixture=fixture,
            snapshot_window=snapshot_window,
            payload=payload,
        )
        try:
            context = extract_prematch_provider_context(payload)
            context["snapshot_window"] = snapshot_window
            cache_result = merge_dashboard_enrichment(
                int(fixture.id),
                {
                    "prematch_provider": context,
                    "data_quality": {
                        "rich_prematch_provider_context": True,
                        "rich_prematch_provider_context_version": PATCH_VERSION,
                        "provider_calls_during_dashboard_request": False,
                        "provider_context_captured_at_j1": True,
                    },
                },
            )
            result["provider_context"] = {
                "status": "persisted",
                "available_sections": context.get("available_sections") or [],
                "counts": context.get("counts") or {},
                "include_profile": context.get("include_profile"),
                "cache": cache_result,
                "informational_only": True,
            }
        except Exception as exc:
            # The cache is supplementary. Never let it block the official J1
            # prediction, decision or immutable forward-test record.
            result["provider_context"] = {
                "status": "cache_failed",
                "error": exc.__class__.__name__,
                "informational_only": True,
            }
        return result

    SportmonksClient.enriched_fixture = rich_enriched_fixture
    legacy._persist_lineup_context = persist_lineup_and_rich_context
    _installed = True
