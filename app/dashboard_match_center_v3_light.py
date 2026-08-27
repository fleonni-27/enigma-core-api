from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.dashboard_enrichment_cache import load_dashboard_enrichment
from app.dashboard_match_center_v3 import DASHBOARD_MATCH_CENTER_V3_HTML
from app.dashboard_operations_v2 import build_dashboard_operations_v2
from app.database import SessionLocal
from app.fixture_results import fixture_results_by_sportmonks_ids
from app.models import OddsSnapshot

DASHBOARD_MATCH_CENTER_V3_VERSION = "dashboard_match_center_v3_light_cached_enrichment_v1"
router = APIRouter(tags=["Dashboard Match Center V3"])


CBF_EXTERNAL_COVERAGE = {
    date(2026, 8, 27): [
        {
            "external_id": "cbf-copa-do-brasil-2026-jogo-143",
            "competition": "Copa do Brasil",
            "home_team": "Internacional",
            "away_team": "Grêmio",
            "starts_at": "2026-08-27T23:00:00+00:00",
            "source": "CBF_EXTERNAL_COVERAGE",
            "source_reference": "Copa do Brasil 2026 · Quartas de Final · Jogo 143",
        }
    ]
}


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _pct(value: float | None) -> float | None:
    return round(value * 100.0, 1) if value is not None else None


def _confidence_band(value: float | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value >= 0.55:
        return "STRONG_FAVORITE"
    if value >= 0.45:
        return "EFFECTIVE_FAVORITE"
    return "BALANCED"


def _decision_reason_labels(reason_codes: list[str]) -> list[str]:
    labels = {
        "CONFIDENCE_BELOW_THRESHOLD": "confiança abaixo do mínimo",
        "EDGE_BELOW_THRESHOLD": "edge abaixo do mínimo",
        "EV_BELOW_THRESHOLD": "EV abaixo do mínimo",
        "ODD_OUTSIDE_POLICY": "odd fora da faixa da política",
        "MARKET_NOT_READY": "mercado J1 insuficiente",
        "NO_VALID_1X2_MARKET": "sem mercado 1X2 válido",
    }
    return [labels.get(code, code.replace("_", " ").lower()) for code in reason_codes]


def _normalize_side(selection: str | None, *, home_team: str, away_team: str) -> str | None:
    value = str(selection or "").strip().lower()
    if value in {"1", "home", "mandante", home_team.lower()}:
        return "1"
    if value in {"x", "draw", "empate"}:
        return "X"
    if value in {"2", "away", "visitante", away_team.lower()}:
        return "2"
    return None


def _bulk_j1_odds(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    if not items:
        return {}
    fixture_ids = [int(item["fixture_id"]) for item in items]
    windows = sorted({str(item["snapshot_window"]) for item in items})
    with SessionLocal() as session:
        rows = session.scalars(
            select(OddsSnapshot)
            .where(
                OddsSnapshot.fixture_id.in_(fixture_ids),
                OddsSnapshot.snapshot_window.in_(windows),
            )
            .order_by(OddsSnapshot.fetched_at.desc(), OddsSnapshot.id.desc())
        ).all()

    by_fixture: dict[int, list[OddsSnapshot]] = {}
    for row in rows:
        by_fixture.setdefault(int(row.fixture_id), []).append(row)

    result: dict[int, dict[str, Any]] = {}
    by_item_id = {int(item["fixture_id"]): item for item in items}
    for fixture_id, item in by_item_id.items():
        decision = item.get("decision") or {}
        bookmaker = decision.get("bookmaker")
        payload: dict[str, Any] = {"1": None, "X": None, "2": None, "bookmaker": bookmaker}
        ordered = sorted(
            by_fixture.get(fixture_id, []),
            key=lambda row: 0 if bookmaker and row.bookmaker == bookmaker else 1,
        )
        for row in ordered:
            side = _normalize_side(
                row.selection,
                home_team=str(item["home_team"]),
                away_team=str(item["away_team"]),
            )
            if side and payload[side] is None:
                payload[side] = _f(row.odd)
                if payload["bookmaker"] is None:
                    payload["bookmaker"] = row.bookmaker
            if all(payload[key] is not None for key in ("1", "X", "2")):
                break
        result[fixture_id] = payload
    return result


def _empty_team_metrics() -> dict[str, Any]:
    return {
        "xg": None,
        "xga": None,
        "goals_for_avg": None,
        "goals_against_avg": None,
        "attack_strength": None,
        "defense_strength": None,
        "form_5": [],
        "form_10_ppm": None,
        "elo": None,
    }


def _external_coverage_fixture(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fixture_id": row["external_id"],
        "sportmonks_fixture_id": None,
        "league": row["competition"],
        "competition": row["competition"],
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "starts_at": row["starts_at"],
        "status": "SCHEDULED_EXTERNAL_COVERAGE",
        "snapshot_window": None,
        "prediction": None,
        "probabilities": {},
        "decision": {
            "decision": "MONITORAMENTO_EXTERNO",
            "reason_codes": ["SPORTMONKS_FIXTURE_NOT_AVAILABLE"],
        },
        "confidence": None,
        "confidence_pct": None,
        "confidence_band": "UNAVAILABLE",
        "odds_1x2": {"1": None, "X": None, "2": None, "bookmaker": None},
        "decision_explanation": [
            "fixture confirmado pela CBF e ausente no feed diário da Sportmonks; sem Prediction/Decision J1 fabricada"
        ],
        "team_metrics": {"home": _empty_team_metrics(), "away": _empty_team_metrics()},
        "final_score": None,
        "competition_context": {
            "official_table_position": None,
            "first_leg_score": None,
            "status": "CBF_EXTERNAL_COVERAGE",
            "reason": row["source_reference"],
        },
        "news": {
            "items": ["Cobertura externa CBF ativada para manter o jogo visível no Match Center."],
            "status": "EXTERNAL_COVERAGE",
            "reason": "Sportmonks não retornou o fixture no feed diário; nenhuma métrica ou odd foi inventada.",
        },
        "data_quality": {
            "rating_context_status": "EXTERNAL_FIXTURE_NO_J1_DATA",
            "dashboard_request_is_bounded": True,
            "provider_calls_during_dashboard_request": False,
            "history_reconstruction_during_dashboard_request": False,
            "xg_xga_informational_only": True,
            "xg_xga_not_used_to_change_current_prediction": True,
            "external_coverage": True,
            "source": row["source"],
            "source_reference": row["source_reference"],
            "sportmonks_fixture_missing": True,
            "official_j1_prediction_available": False,
        },
    }


def _append_external_coverage(fixtures: list[dict[str, Any]], *, target_date: date | None) -> None:
    effective_date = target_date or date.today()
    rows = CBF_EXTERNAL_COVERAGE.get(effective_date) or []
    existing = {
        (str(item.get("home_team") or "").strip().lower(), str(item.get("away_team") or "").strip().lower())
        for item in fixtures
    }
    for row in rows:
        key = (row["home_team"].strip().lower(), row["away_team"].strip().lower())
        if key not in existing:
            fixtures.append(_external_coverage_fixture(row))


def build_dashboard_match_center_v3(*, target_date: date | None = None) -> dict[str, Any]:
    # HTTP invariant: only bounded database reads. Sportmonks and historical
    # reconstruction run in app.dashboard_enrichment_runner, never here.
    base = build_dashboard_operations_v2(target_date=target_date)
    items = list(base.get("fixtures") or [])
    fixture_ids = [int(item["fixture_id"]) for item in items]
    odds_by_fixture = _bulk_j1_odds(items)
    enrichment_by_fixture = load_dashboard_enrichment(fixture_ids)
    sportmonks_ids = [int(item["sportmonks_fixture_id"]) for item in items]
    final_results = fixture_results_by_sportmonks_ids(sportmonks_ids) if sportmonks_ids else {}

    fixtures: list[dict[str, Any]] = []
    for item in items:
        decision = item.get("decision") or {}
        probabilities = item.get("probabilities") or {}
        confidence = _f(decision.get("calibrated_confidence"))
        if confidence is None:
            values = [_f(probabilities.get(key)) for key in ("home", "draw", "away")]
            values = [value for value in values if value is not None]
            confidence = max(values) if values else None

        sid = int(item["sportmonks_fixture_id"])
        final_row = final_results.get(sid)
        final_score = None
        if final_row and final_row.get("home_goals") is not None and final_row.get("away_goals") is not None:
            final_score = f"{final_row.get('home_goals')} x {final_row.get('away_goals')}"

        cached = enrichment_by_fixture.get(int(item["fixture_id"])) or {}
        metrics = cached.get("team_metrics") or {"home": _empty_team_metrics(), "away": _empty_team_metrics()}
        facts = list(cached.get("facts") or [])
        quality = dict(cached.get("data_quality") or {})
        has_metrics = any(
            (metrics.get(side) or {}).get(key) is not None
            for side in ("home", "away")
            for key in ("xg", "xga", "goals_for_avg", "goals_against_avg")
        )

        fixtures.append(
            {
                **item,
                "confidence": confidence,
                "confidence_pct": _pct(confidence),
                "confidence_band": _confidence_band(confidence),
                "odds_1x2": odds_by_fixture.get(int(item["fixture_id"]), {"1": None, "X": None, "2": None, "bookmaker": None}),
                "decision_explanation": _decision_reason_labels(list(decision.get("reason_codes") or [])),
                "team_metrics": metrics,
                "final_score": final_score,
                "competition_context": {
                    "official_table_position": None,
                    "first_leg_score": None,
                    "status": "LEAGUE_METADATA_CONNECTED",
                    "reason": "metadados de liga disponíveis; classificação formal ainda não persistida no Match Center",
                },
                "news": {
                    "items": facts,
                    "status": "ENIGMA_ANALYSIS" if facts else ("DATA_AVAILABLE" if has_metrics else "BACKGROUND_ENRICHMENT_PENDING"),
                    "reason": "fatos quantitativos gerados em background pela Enigma Core; não são notícias editoriais externas",
                },
                "data_quality": {
                    **quality,
                    "rating_context_status": "INFORMATIONAL_ENRICHMENT_AVAILABLE" if has_metrics else "BACKGROUND_ENRICHMENT_PENDING",
                    "dashboard_request_is_bounded": True,
                    "provider_calls_during_dashboard_request": False,
                    "history_reconstruction_during_dashboard_request": False,
                    "enrichment_cache_generated_at": cached.get("cache_generated_at"),
                    "xg_xga_informational_only": True,
                    "xg_xga_not_used_to_change_current_prediction": True,
                },
            }
        )

    _append_external_coverage(fixtures, target_date=target_date)

    return {
        **base,
        "version": DASHBOARD_MATCH_CENTER_V3_VERSION,
        "fixtures": fixtures,
        "policy": {
            **(base.get("policy") or {}),
            "dashboard_self_feeds_from_database": True,
            "j1_pipeline_is_primary_live_prematch_source": True,
            "unsupported_sources_are_never_fabricated": True,
            "external_cbf_coverage_is_display_only": True,
            "dashboard_request_is_read_only_and_bounded": True,
            "provider_calls_during_dashboard_refresh": False,
            "history_reconstruction_during_dashboard_refresh": False,
            "enrichment_is_background_materialized": True,
            "xg_xga_are_informational_only": True,
            "confidence_strong_favorite_threshold": 0.55,
            "confidence_effective_favorite_threshold": 0.45,
        },
    }


@router.get("/dashboard/api/match-center-v3")
def dashboard_match_center_v3_api(target_date: date | None = Query(default=None)) -> dict[str, Any]:
    return build_dashboard_match_center_v3(target_date=target_date)


@router.get("/dashboard/match-center-v3", response_class=HTMLResponse, include_in_schema=False)
def dashboard_match_center_v3_page() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_MATCH_CENTER_V3_HTML)
