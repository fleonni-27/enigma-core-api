from __future__ import annotations

from typing import Any

LINEUP_IMPACT_VERSION = "lineup_impact_v1"


def quantify_lineup_impact(
    *,
    expected_xi_value: float,
    absent_value: float,
) -> dict[str, Any]:
    expected = float(expected_xi_value)
    absent = float(absent_value)
    if expected <= 0.0:
        raise ValueError("expected_xi_value must be positive")
    if absent < 0.0:
        raise ValueError("absent_value must be non-negative")
    if absent > expected:
        raise ValueError("absent_value cannot exceed expected_xi_value")

    absence_ratio = absent / expected
    retained = 1.0 - absence_ratio
    return {
        "version": LINEUP_IMPACT_VERSION,
        "expected_xi_value": round(expected, 6),
        "absent_value": round(absent, 6),
        "absence_impact_pct": round(absence_ratio * 100.0, 4),
        "strength_retained": round(retained, 8),
        "policy": {
            "player_values_must_be_supplied_by_auditable_upstream_model": True,
            "missing_player_value_is_not_guessed": True,
            "rating_input_range": [0.0, 1.0],
        },
    }


def relative_lineup_support(selection: str, home_strength: float, away_strength: float) -> float:
    selection = str(selection).strip().upper()
    if selection not in {"1", "X", "2"}:
        raise ValueError("selection must be 1, X, or 2")
    home = max(0.0, min(1.0, float(home_strength)))
    away = max(0.0, min(1.0, float(away_strength)))
    delta = home - away
    if selection == "1":
        return max(0.0, min(1.0, 0.5 + delta / 2.0))
    if selection == "2":
        return max(0.0, min(1.0, 0.5 - delta / 2.0))
    return max(0.0, min(1.0, 1.0 - abs(delta)))
