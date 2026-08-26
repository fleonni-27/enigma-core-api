from __future__ import annotations

import re
import unicodedata


TARGET_LEAGUES = {
    "br_serie_a": {
        "canonical_name": "Serie A",
        "priority": 1,
        "aliases": [
            "Serie A",
            "Brazil Serie A",
            "Brasileirao Serie A",
            "Brasileirão Série A",
            "Brasileiro Serie A",
            "Brazilian Serie A",
        ],
    },
    "br_serie_b": {
        "canonical_name": "Serie B",
        "priority": 1,
        "aliases": [
            "Serie B",
            "Brazil Serie B",
            "Brasileirao Serie B",
            "Brasileirão Série B",
            "Brasileiro Serie B",
            "Brazilian Serie B",
        ],
    },
    "copa_do_brasil": {
        "canonical_name": "Copa do Brasil",
        "priority": 1,
        "aliases": [
            "Copa do Brasil",
            "Brazil Copa do Brasil",
            "Brazilian Cup",
        ],
    },
    "libertadores": {
        "canonical_name": "Copa Libertadores",
        "priority": 1,
        "aliases": [
            "Copa Libertadores",
            "Libertadores",
            "Libertadores da America",
            "Libertadores da América",
            "CONMEBOL Libertadores",
            "CONMEBOL Copa Libertadores",
        ],
    },
    "sudamericana": {
        "canonical_name": "Copa Sudamericana",
        "priority": 1,
        "aliases": [
            "Copa Sudamericana",
            "Copa Sulamericana",
            "Sudamericana",
            "Sul-Americana",
            "Copa Sul-Americana",
            "CONMEBOL Sudamericana",
            "CONMEBOL Copa Sudamericana",
        ],
    },
    "premier_league": {"canonical_name": "Premier League", "priority": 1, "aliases": ["Premier League", "Premiere League"]},
    "la_liga": {"canonical_name": "La Liga", "priority": 1, "aliases": ["La Liga", "LaLiga"]},
    "champions_league": {"canonical_name": "Champions League", "priority": 1, "aliases": ["Champions League", "UEFA Champions League"]},
}


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_ALIAS_INDEX: dict[str, str] = {}
for key, item in TARGET_LEAGUES.items():
    for alias in item["aliases"]:
        _ALIAS_INDEX[_normalize(alias)] = key
    _ALIAS_INDEX[_normalize(item["canonical_name"])] = key


def canonical_league(name: str | None) -> dict:
    normalized = _normalize(name)
    key = _ALIAS_INDEX.get(normalized)
    if key is None:
        return {"key": None, "canonical_name": name or "Unknown", "priority": None, "target": False}
    item = TARGET_LEAGUES[key]
    return {"key": key, "canonical_name": item["canonical_name"], "priority": item["priority"], "target": True}


def registry_definition() -> list[dict]:
    return [
        {"key": key, "canonical_name": value["canonical_name"], "priority": value["priority"], "aliases": value["aliases"]}
        for key, value in TARGET_LEAGUES.items()
    ]
