from __future__ import annotations

import math
from typing import Any

FOOTBALL_PROBABILITY_MODELS_VERSION = "football_probability_models_v1"
DEFAULT_MAX_GOALS = 10
DEFAULT_DIXON_COLES_RHO = -0.08
DEFAULT_ELO_HOME_ADVANTAGE = 65.0
DEFAULT_ELO_DRAW_PARAMETER = 0.70


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def poisson_pmf(goals: int, expected_goals: float) -> float:
    if goals < 0:
        raise ValueError("goals must be non-negative")
    lam = float(expected_goals)
    if lam <= 0.0:
        raise ValueError("expected_goals must be positive")
    return math.exp(-lam) * (lam**goals) / math.factorial(goals)


def _normalized_1x2_from_grid(grid: list[list[float]]) -> dict[str, float]:
    home = draw = away = 0.0
    total = 0.0
    for h, row in enumerate(grid):
        for a, raw in enumerate(row):
            p = max(0.0, float(raw))
            total += p
            if h > a:
                home += p
            elif h == a:
                draw += p
            else:
                away += p
    if total <= 0.0:
        raise ValueError("score grid has no probability mass")
    return {
        "1": home / total,
        "X": draw / total,
        "2": away / total,
    }


def poisson_1x2(
    expected_home_goals: float,
    expected_away_goals: float,
    *,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> dict[str, Any]:
    if max_goals < 5 or max_goals > 20:
        raise ValueError("max_goals must be between 5 and 20")
    home_lambda = _clip(expected_home_goals, 0.05, 6.0)
    away_lambda = _clip(expected_away_goals, 0.05, 6.0)
    grid = [
        [poisson_pmf(h, home_lambda) * poisson_pmf(a, away_lambda) for a in range(max_goals + 1)]
        for h in range(max_goals + 1)
    ]
    probabilities = _normalized_1x2_from_grid(grid)
    return {
        "version": FOOTBALL_PROBABILITY_MODELS_VERSION,
        "model": "independent_poisson",
        "expected_goals": {"home": round(home_lambda, 6), "away": round(away_lambda, 6)},
        "probabilities": {key: round(value, 8) for key, value in probabilities.items()},
        "max_goals": max_goals,
    }


def _dixon_coles_tau(home_goals: int, away_goals: int, home_lambda: float, away_lambda: float, rho: float) -> float:
    if home_goals == 0 and away_goals == 0:
        return 1.0 - (home_lambda * away_lambda * rho)
    if home_goals == 0 and away_goals == 1:
        return 1.0 + (home_lambda * rho)
    if home_goals == 1 and away_goals == 0:
        return 1.0 + (away_lambda * rho)
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def dixon_coles_1x2(
    expected_home_goals: float,
    expected_away_goals: float,
    *,
    rho: float = DEFAULT_DIXON_COLES_RHO,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> dict[str, Any]:
    if rho < -0.30 or rho > 0.30:
        raise ValueError("rho must be between -0.30 and 0.30")
    home_lambda = _clip(expected_home_goals, 0.05, 6.0)
    away_lambda = _clip(expected_away_goals, 0.05, 6.0)
    grid: list[list[float]] = []
    for h in range(max_goals + 1):
        row: list[float] = []
        for a in range(max_goals + 1):
            base = poisson_pmf(h, home_lambda) * poisson_pmf(a, away_lambda)
            tau = max(0.0, _dixon_coles_tau(h, a, home_lambda, away_lambda, float(rho)))
            row.append(base * tau)
        grid.append(row)
    probabilities = _normalized_1x2_from_grid(grid)
    return {
        "version": FOOTBALL_PROBABILITY_MODELS_VERSION,
        "model": "dixon_coles_low_score_adjustment",
        "rho": round(float(rho), 6),
        "expected_goals": {"home": round(home_lambda, 6), "away": round(away_lambda, 6)},
        "probabilities": {key: round(value, 8) for key, value in probabilities.items()},
        "max_goals": max_goals,
    }


def _blend(primary: float | None, fallback: float | None) -> tuple[float | None, str | None]:
    if primary is not None and fallback is not None:
        return (0.65 * float(primary)) + (0.35 * float(fallback)), "xg_plus_goals"
    if primary is not None:
        return float(primary), "xg"
    if fallback is not None:
        return float(fallback), "goals"
    return None, None


def derive_expected_goals(
    *,
    home_goals_for_avg: float | None,
    away_goals_for_avg: float | None,
    home_goals_against_avg: float | None,
    away_goals_against_avg: float | None,
    home_xg_for_avg: float | None = None,
    away_xg_for_avg: float | None = None,
    home_xg_against_avg: float | None = None,
    away_xg_against_avg: float | None = None,
    home_advantage_multiplier: float = 1.08,
) -> dict[str, Any]:
    if home_advantage_multiplier < 0.8 or home_advantage_multiplier > 1.3:
        raise ValueError("home_advantage_multiplier must be between 0.8 and 1.3")

    home_attack, home_attack_source = _blend(home_xg_for_avg, home_goals_for_avg)
    away_attack, away_attack_source = _blend(away_xg_for_avg, away_goals_for_avg)
    home_defence_concedes, home_defence_source = _blend(home_xg_against_avg, home_goals_against_avg)
    away_defence_concedes, away_defence_source = _blend(away_xg_against_avg, away_goals_against_avg)

    values = [home_attack, away_attack, home_defence_concedes, away_defence_concedes]
    if any(value is None for value in values):
        return {
            "status": "not_ready",
            "version": FOOTBALL_PROBABILITY_MODELS_VERSION,
            "reason_codes": ["INSUFFICIENT_ATTACK_DEFENCE_INPUTS"],
        }

    home_lambda = ((float(home_attack) + float(away_defence_concedes)) / 2.0) * float(home_advantage_multiplier)
    away_lambda = (float(away_attack) + float(home_defence_concedes)) / 2.0
    sources = {
        "home_attack": home_attack_source,
        "away_attack": away_attack_source,
        "home_defence": home_defence_source,
        "away_defence": away_defence_source,
    }
    if all("xg" in str(source) for source in sources.values()):
        quality = "FULL_XG_XGA"
    elif any("xg" in str(source) for source in sources.values()):
        quality = "MIXED_XG_GOALS"
    else:
        quality = "GOALS_ONLY"

    return {
        "status": "ok",
        "version": FOOTBALL_PROBABILITY_MODELS_VERSION,
        "expected_goals": {
            "home": round(_clip(home_lambda, 0.05, 6.0), 6),
            "away": round(_clip(away_lambda, 0.05, 6.0), 6),
        },
        "input_quality": quality,
        "sources": sources,
        "home_advantage_multiplier": float(home_advantage_multiplier),
    }


def elo_davidson_1x2(
    home_elo: float,
    away_elo: float,
    *,
    home_advantage_elo: float = DEFAULT_ELO_HOME_ADVANTAGE,
    draw_parameter: float = DEFAULT_ELO_DRAW_PARAMETER,
) -> dict[str, Any]:
    if draw_parameter <= 0.0 or draw_parameter > 3.0:
        raise ValueError("draw_parameter must be > 0 and <= 3")
    home_strength = 10.0 ** ((float(home_elo) + float(home_advantage_elo)) / 400.0)
    away_strength = 10.0 ** (float(away_elo) / 400.0)
    draw_strength = float(draw_parameter) * math.sqrt(home_strength * away_strength)
    total = home_strength + away_strength + draw_strength
    probabilities = {
        "1": home_strength / total,
        "X": draw_strength / total,
        "2": away_strength / total,
    }
    return {
        "version": FOOTBALL_PROBABILITY_MODELS_VERSION,
        "model": "elo_davidson_1x2",
        "home_elo": float(home_elo),
        "away_elo": float(away_elo),
        "home_advantage_elo": float(home_advantage_elo),
        "draw_parameter": float(draw_parameter),
        "probabilities": {key: round(value, 8) for key, value in probabilities.items()},
    }


def elo_update(
    home_elo: float,
    away_elo: float,
    *,
    home_score: float,
    k_factor: float = 20.0,
    home_advantage_elo: float = DEFAULT_ELO_HOME_ADVANTAGE,
) -> tuple[float, float]:
    if home_score not in {0.0, 0.5, 1.0}:
        raise ValueError("home_score must be 0.0, 0.5, or 1.0")
    if k_factor <= 0.0:
        raise ValueError("k_factor must be positive")
    expected_home = 1.0 / (1.0 + 10.0 ** ((float(away_elo) - (float(home_elo) + float(home_advantage_elo))) / 400.0))
    delta = float(k_factor) * (float(home_score) - expected_home)
    return float(home_elo) + delta, float(away_elo) - delta
