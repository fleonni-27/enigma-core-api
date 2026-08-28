from __future__ import annotations

from typing import Any

CONTEXT_VERSION = "prematch_provider_context_v1"
MAX_LIST_ITEMS = 80
MAX_DICT_ITEMS = 60
MAX_STRING_LENGTH = 500
MAX_DEPTH = 5

# Only relations that are meaningful before kickoff are retained. Live/post-match
# fields such as events, scores, timeline, comments, pressure and ball coordinates
# are deliberately excluded from the J1 materialized context.
PREMATCH_RELATIONS = (
    "participants",
    "league",
    "season",
    "stage",
    "round",
    "group",
    "aggregate",
    "venue",
    "state",
    "weatherreport",
    "weatherReport",
    "lineups",
    "expectedlineups",
    "expectedLineups",
    "formations",
    "coaches",
    "referees",
    "sidelined",
    "prematchnews",
    "prematchNews",
    "metadata",
    "predictions",
    "statistics",
    "xgfixture",
    "xGFixture",
)

TOP_LEVEL_FIELDS = (
    "id",
    "sport_id",
    "league_id",
    "season_id",
    "stage_id",
    "group_id",
    "aggregate_id",
    "round_id",
    "state_id",
    "venue_id",
    "name",
    "starting_at",
    "starting_at_timestamp",
    "leg",
    "details",
    "length",
    "placeholder",
    "has_odds",
    "has_premium_odds",
)


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_DEPTH:
        if isinstance(value, (dict, list)):
            return None
        return value
    if isinstance(value, str):
        return value[:MAX_STRING_LENGTH]
    if isinstance(value, list):
        return [_bounded(item, depth=depth + 1) for item in value[:MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_DICT_ITEMS:
                break
            bounded = _bounded(child, depth=depth + 1)
            if bounded is not None:
                result[str(key)] = bounded
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_STRING_LENGTH]


def _count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return 1 if value else 0
    return 0


def extract_prematch_provider_context(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        raw = {}

    metadata = {key: _bounded(raw.get(key)) for key in TOP_LEVEL_FIELDS if raw.get(key) is not None}
    sections: dict[str, Any] = {}
    available: list[str] = []
    missing: list[str] = []

    # Normalize duplicate casing used by different Sportmonks responses.
    canonical = {
        "participants": ("participants",),
        "league": ("league",),
        "season": ("season",),
        "stage": ("stage",),
        "round": ("round",),
        "group": ("group",),
        "aggregate": ("aggregate",),
        "venue": ("venue",),
        "state": ("state",),
        "weather": ("weatherReport", "weatherreport"),
        "lineups": ("lineups",),
        "expected_lineups": ("expectedLineups", "expectedlineups"),
        "formations": ("formations",),
        "coaches": ("coaches",),
        "referees": ("referees",),
        "sidelined": ("sidelined",),
        "prematch_news": ("prematchNews", "prematchnews"),
        "provider_predictions": ("predictions",),
        "metadata": ("metadata",),
        "statistics": ("statistics",),
        "xg_fixture": ("xGFixture", "xgfixture"),
    }

    counts: dict[str, int] = {}
    for section, aliases in canonical.items():
        value = None
        for alias in aliases:
            if alias in raw:
                value = raw.get(alias)
                break
        count = _count(value)
        counts[section] = count
        if count:
            sections[section] = _bounded(value)
            available.append(section)
        else:
            missing.append(section)

    include_profile = payload.get("_enigma_include_profile") if isinstance(payload, dict) else None
    return {
        "version": CONTEXT_VERSION,
        "provider": "sportmonks",
        "captured_phase": "J1_PREMATCH",
        "fixture": metadata,
        "sections": sections,
        "counts": counts,
        "available_sections": available,
        "missing_sections": missing,
        "include_profile": include_profile,
        "policy": {
            "informational_only": True,
            "provider_prediction_not_used_by_enigma_model": True,
            "xg_xga_not_used_to_change_current_prediction": True,
            "post_kickoff_events_scores_timeline_excluded": True,
            "payload_bounded_before_persistence": True,
            "provider_calls_during_dashboard_request": False,
        },
    }
