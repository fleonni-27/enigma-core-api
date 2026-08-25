from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.dashboard_match_center_v3 import DASHBOARD_MATCH_CENTER_V3_HTML
from app.dashboard_operations_v2 import build_dashboard_operations_v2
from app.database import SessionLocal
from app.fixture_results import fixture_results_by_sportmonks_ids
from app.models import OddsSnapshot

DASHBOARD_MATCH_CENTER_V3_VERSION = "dashboard_match_center_v3_light"
router = APIRouter(tags=["Dashboard Match Center V3"])


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


def build_dashboard_match_center_v3(*, target_date: date | None = None) -> dict[str, Any]:
    # Critical invariant: this HTTP path is read-only and bounded. Heavy Enigma
    # Rating/history reconstruction must be produced outside the dashboard request
    # and persisted before presentation.
    base = build_dashboard_operations_v2(target_date=target_date)
    items = list(base.get("fixtures") or [])
    odds_by_fixture = _bulk_j1_odds(items)
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

        fixtures.append(
            {
                **item,
                "confidence": confidence,
                "confidence_pct": _pct(confidence),
                "confidence_band": _confidence_band(confidence),
                "odds_1x2": odds_by_fixture.get(int(item["fixture_id"]), {"1": None, "X": None, "2": None, "bookmaker": None}),
                "decision_explanation": _decision_reason_labels(list(decision.get("reason_codes") or [])),
                "team_metrics": {
                    "home": _empty_team_metrics(),
                    "away": _empty_team_metrics(),
                },
                "final_score": final_score,
                "competition_context": {
                    "official_table_position": None,
                    "first_leg_score": None,
                    "status": "SOURCE_NOT_CONNECTED",
                    "reason": "competition standings and formal knockout tie metadata are not persisted yet",
                },
                "news": {
                    "items": [],
                    "status": "SOURCE_NOT_CONNECTED",
                    "reason": "editorial/injury news feed is not connected",
                },
                "data_quality": {
                    "rating_context_status": "BACKGROUND_ENRICHMENT_PENDING",
                    "dashboard_request_is_bounded": True,
                    "heavy_context_is_not_recomputed_on_refresh": True,
                },
            }
        )

    return {
        **base,
        "version": DASHBOARD_MATCH_CENTER_V3_VERSION,
        "fixtures": fixtures,
        "policy": {
            **(base.get("policy") or {}),
            "dashboard_self_feeds_from_database": True,
            "j1_pipeline_is_primary_live_prematch_source": True,
            "unsupported_sources_are_never_fabricated": True,
            "dashboard_request_is_read_only_and_bounded": True,
            "heavy_enrichment_runs_outside_http_refresh": True,
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
