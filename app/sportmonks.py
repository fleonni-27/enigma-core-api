from datetime import date
from typing import Any

import httpx

from app.config import get_settings


MAX_FIXTURE_PAGES_PER_DATE = 100
FIXTURE_PAGE_SIZE = 50


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
    def __init__(self) -> None:
        self.settings = get_settings()

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

        async with httpx.AsyncClient(timeout=30.0) as client:
            while page <= MAX_FIXTURE_PAGES_PER_DATE:
                params = {**base_params, "page": page}
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()

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

                # If the upstream says another page exists but yields no new
                # fixtures, abort instead of risking an infinite pagination loop.
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
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def enriched_fixture(self, fixture_id: int) -> dict:
        url = f"{self.settings.sportmonks_base_url}/fixtures/{fixture_id}"
        params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": (
                "participants;league;lineups.player;lineups.details.type;"
                "statistics.type;xGFixture.type"
            ),
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def fixture_result(self, fixture_id: int) -> dict:
        url = f"{self.settings.sportmonks_base_url}/fixtures/{fixture_id}"
        params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": "scores;state;participants",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()


# The production app composes the research router through future_batch. Installing
# sub-routers here keeps that legacy composition backward-compatible without
# introducing a second top-level FastAPI app dependency.
from app.outcome_settlement import install_outcome_settlement_routes
from app.dashboard import install_dashboard_routes
from app.dashboard_operations import install_dashboard_operations_routes

install_outcome_settlement_routes()
install_dashboard_routes()
install_dashboard_operations_routes()
