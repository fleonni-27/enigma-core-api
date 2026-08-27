from __future__ import annotations

import os

from app import sportmonks_enrichment_provider as provider


def test_normalized_provider_url_replaces_embedded_token(monkeypatch):
    monkeypatch.setenv(
        "Leagues",
        "https://api.sportmonks.com/v3/football/leagues?api_token=old-token&include=seasons",
    )

    class Settings:
        sportmonks_api_token = "primary-token"

    monkeypatch.setattr(provider, "get_settings", lambda: Settings())
    url = provider._normalized_provider_url("Leagues")
    assert "old-token" not in url
    assert "api_token=primary-token" in url
    assert "include=seasons" in url


def test_fixture_analysis_url_replaces_fixture_id(monkeypatch):
    monkeypatch.setenv(
        "Analysing_probabily_Xg",
        "https://api.sportmonks.com/v3/football/fixtures/123?api_token=old&include=participants%3Bdetails",
    )

    class Settings:
        sportmonks_api_token = "primary-token"

    monkeypatch.setattr(provider, "get_settings", lambda: Settings())
    url = provider._normalized_provider_url("Analysing_probabily_Xg", fixture_id=999)
    assert "/fixtures/999?" in url
    assert "api_token=primary-token" in url


def test_xg_candidates_detect_expected_goals_records():
    payload = {
        "details": [
            {"type": {"name": "Expected Goals"}, "value": 1.42},
            {"type": {"name": "Possession"}, "value": 52},
        ]
    }
    matches = provider._xg_matches(payload)
    assert matches
    assert any("details" in item["path"] for item in matches)
