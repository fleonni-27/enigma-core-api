from datetime import date

import httpx

from app.config import get_settings


class SportmonksClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def fixtures_by_date(self, target_date: date) -> dict:
        url = f"{self.settings.sportmonks_base_url}/fixtures/date/{target_date.isoformat()}"
        params = {
            "api_token": self.settings.sportmonks_api_token,
            "include": "participants;league",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

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
