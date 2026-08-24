from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from app import dashboard as dashboard_module
from app import outcome_settlement as settlement_module
from app.config import get_settings
from app.fixture_results import (
    fixture_results_by_sportmonks_ids,
    persist_fixture_result,
)
from app.models import Fixture, FixtureDataSnapshot
from app.training_dataset import STAT_NAMES, _as_list, _stat_value

DASHBOARD_CLARITY_VERSION = "dashboard_v1_2_3"
MAX_LEGACY_SCORE_BACKFILLS_PER_DASHBOARD_REQUEST = 10
_installed = False
_original_recent_records_payload = dashboard_module._recent_records_payload


def _selection_context(item: dict[str, Any]) -> dict[str, Any]:
    selection = str(item.get("selection") or "").strip().upper()
    home_team = str(item.get("home_team") or "").strip()
    away_team = str(item.get("away_team") or "").strip()

    if selection == "1":
        return {
            "selection_side": "HOME",
            "selection_label": f"Mandante (1) — {home_team}" if home_team else "Mandante (1)",
            "selected_team": home_team or None,
        }
    if selection == "X":
        return {
            "selection_side": "DRAW",
            "selection_label": "Empate (X)",
            "selected_team": None,
        }
    if selection == "2":
        return {
            "selection_side": "AWAY",
            "selection_label": f"Visitante (2) — {away_team}" if away_team else "Visitante (2)",
            "selected_team": away_team or None,
        }
    return {
        "selection_side": "UNKNOWN",
        "selection_label": selection or "—",
        "selected_team": None,
    }


def _result_from_score(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "1"
    if home_goals < away_goals:
        return "2"
    return "X"


def _fixtures_by_sportmonks_ids(ids: list[int]) -> dict[int, Fixture]:
    normalized = sorted({int(value) for value in ids})
    if not normalized:
        return {}
    with dashboard_module.SessionLocal() as session:
        fixtures = session.scalars(
            select(Fixture).where(Fixture.sportmonks_id.in_(normalized))
        ).all()
    return {int(fixture.sportmonks_id): fixture for fixture in fixtures}


def _snapshot_score_fallback(
    sportmonks_fixture_ids: list[int],
) -> dict[int, dict[str, Any]]:
    ids = sorted({int(value) for value in sportmonks_fixture_ids})
    if not ids:
        return {}

    results: dict[int, dict[str, Any]] = {}
    with dashboard_module.SessionLocal() as session:
        fixtures = session.scalars(
            select(Fixture).where(Fixture.sportmonks_id.in_(ids))
        ).all()
        for fixture in fixtures:
            snapshot = session.scalar(
                select(FixtureDataSnapshot)
                .where(FixtureDataSnapshot.fixture_id == fixture.id)
                .order_by(
                    FixtureDataSnapshot.fetched_at.desc(),
                    FixtureDataSnapshot.id.desc(),
                )
                .limit(1)
            )
            if snapshot is None:
                continue
            statistics = _as_list(snapshot.statistics)
            home_value = _stat_value(statistics, STAT_NAMES["goals"], "home")
            away_value = _stat_value(statistics, STAT_NAMES["goals"], "away")
            if home_value is None or away_value is None:
                continue
            home_goals = int(home_value)
            away_goals = int(away_value)
            results[int(fixture.sportmonks_id)] = {
                "home_goals": home_goals,
                "away_goals": away_goals,
                "actual_result": _result_from_score(home_goals, away_goals),
                "score_source": "FINAL_SNAPSHOT_FALLBACK",
            }
    return results


def _upstream_score_backfill(
    sportmonks_fixture_ids: list[int],
) -> dict[int, dict[str, Any]]:
    ids = sorted({int(value) for value in sportmonks_fixture_ids})[
        :MAX_LEGACY_SCORE_BACKFILLS_PER_DASHBOARD_REQUEST
    ]
    if not ids:
        return {}

    fixtures = _fixtures_by_sportmonks_ids(ids)
    settings = get_settings()
    results: dict[int, dict[str, Any]] = {}

    with httpx.Client(timeout=15.0) as client:
        for sportmonks_id in ids:
            fixture = fixtures.get(sportmonks_id)
            if fixture is None:
                continue
            try:
                response = client.get(
                    f"{settings.sportmonks_base_url}/fixtures/{sportmonks_id}",
                    params={
                        "api_token": settings.sportmonks_api_token,
                        "include": "scores;state;participants",
                    },
                )
                response.raise_for_status()
                outcome = settlement_module._parse_fixture_outcome(
                    response.json(),
                    sportmonks_id,
                )
            except Exception:
                continue

            if outcome.get("status") != "ok":
                continue
            score = outcome.get("regulation_score") or {}
            state = outcome.get("state") or {}
            home_goals = score.get("home")
            away_goals = score.get("away")
            if home_goals is None or away_goals is None:
                continue

            stored = persist_fixture_result(
                fixture_id=int(fixture.id),
                sportmonks_fixture_id=sportmonks_id,
                home_goals=int(home_goals),
                away_goals=int(away_goals),
                actual_result=str(outcome.get("actual_result") or ""),
                score_source=str(score.get("source") or "SPORTMONKS"),
                state_id=state.get("id"),
                state_code=state.get("code"),
            )
            record = stored.get("record") or {}
            if stored.get("status") in {"persisted", "exists"} and record:
                results[sportmonks_id] = record

    return results


def _score_contexts(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    settled_ids = [
        int(item["sportmonks_fixture_id"])
        for item in items
        if item.get("settlement_status") == "SETTLED"
        and item.get("sportmonks_fixture_id") is not None
    ]
    stored = fixture_results_by_sportmonks_ids(settled_ids)
    missing_after_store = [value for value in settled_ids if value not in stored]
    snapshot = _snapshot_score_fallback(missing_after_store)
    missing_after_snapshot = [
        value
        for value in missing_after_store
        if value not in snapshot
    ]
    upstream = _upstream_score_backfill(missing_after_snapshot)
    return {**snapshot, **upstream, **stored}


def _score_context(
    item: dict[str, Any],
    score_by_fixture: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    if item.get("settlement_status") != "SETTLED":
        return {
            "final_score": None,
            "home_goals": None,
            "away_goals": None,
            "score_source": None,
        }

    sportmonks_id = item.get("sportmonks_fixture_id")
    score = score_by_fixture.get(int(sportmonks_id)) if sportmonks_id is not None else None
    if not score:
        return {
            "final_score": None,
            "home_goals": None,
            "away_goals": None,
            "score_source": None,
        }

    home_goals = int(score["home_goals"])
    away_goals = int(score["away_goals"])
    score_result = str(score.get("actual_result") or _result_from_score(home_goals, away_goals))
    ledger_result = str(item.get("actual_result") or "")
    if ledger_result and score_result != ledger_result:
        return {
            "final_score": None,
            "home_goals": None,
            "away_goals": None,
            "score_source": "RESULT_MISMATCH_BLOCKED",
        }

    return {
        "final_score": f"{home_goals} x {away_goals}",
        "home_goals": home_goals,
        "away_goals": away_goals,
        "score_source": score.get("score_source"),
    }


def _clarified_recent_records_payload(days: int, limit: int) -> dict[str, Any]:
    payload = _original_recent_records_payload(days, limit)
    payload["version"] = DASHBOARD_CLARITY_VERSION
    items = payload.get("items") or []
    score_by_fixture = _score_contexts(items)
    for item in items:
        item.update(_selection_context(item))
        item.update(_score_context(item, score_by_fixture))
    return payload


def _clarified_html(html: str) -> str:
    replacements = {
        "BET rate": "Entradas aprovadas",
        "${k.bet_records} BET · ${k.no_bet_records} NO_BET": "${k.bet_records} aprovadas · ${k.no_bet_records} rejeitadas",
        "BETs liquidadas": "Entradas liquidadas",
        "BETs executadas": "Entradas aprovadas",
        "Somente BETs liquidadas": "Somente entradas aprovadas liquidadas",
        "dos NO_BET liquidados": "das entradas rejeitadas liquidadas",
        "Contrafactual NO_BET": "Contrafactual das rejeitadas",
        "Se todos fossem apostados": "Se todas as seleções rejeitadas fossem entradas",
        "Diagnostics · bloqueadores NO_BET": "Diagnóstico · motivos de rejeição",
        "Assinaturas NO_BET": "Combinações de motivos de rejeição",
        "Sem BETs no periodo.": "Sem entradas aprovadas no período.",
        "Sem BETs liquidadas no periodo.": "Sem entradas aprovadas liquidadas no período.",
        "<tr><td colspan=\"10\" class=\"muted\">Nenhum registro.</td></tr>": "<tr><td colspan=\"11\" class=\"muted\">Nenhum registro.</td></tr>",
        "<th>Decisao</th><th>Sel.</th>": "<th>Placar</th><th>Ação</th><th>Seleção avaliada</th>",
        "<td><strong>${esc(r.decision)}</strong></td><td>${esc(r.selection||'—')}</td>": (
            "<td><strong>${esc(r.final_score||'—')}</strong>"
            "<div class=\"reason\">${esc(r.score_source||'')}</div></td>"
            "<td><strong>${esc(r.decision==='BET'?'ENTRAR':'NÃO ENTRAR')}</strong>"
            "<div class=\"reason\">${esc(r.decision)}</div></td>"
            "<td><strong>${esc(r.selection_label||r.selection||'—')}</strong>"
            "<div class=\"reason\">${esc(r.selection_side||'')}</div></td>"
        ),
    }
    for old, new in replacements.items():
        html = html.replace(old, new)

    legend = (
        '<div class="sample"><strong>Como ler as decisões</strong>'
        '<p><b>ENTRAR</b> = a política aprovou a seleção indicada. '
        '<b>NÃO ENTRAR</b> = a política rejeitou essa seleção. '
        '1 = mandante · X = empate · 2 = visitante. '
        'Jogos liquidados exibem o placar final confirmado.</p></div>'
    )
    html = html.replace(
        '<div id="content"><div class="loading">Carregando metricas...</div></div>',
        legend + '<div id="content"><div class="loading">Carregando metricas...</div></div>',
    )
    return html


def install_dashboard_selection_clarity() -> None:
    global _installed
    if _installed:
        return
    dashboard_module.DASHBOARD_VERSION = DASHBOARD_CLARITY_VERSION
    dashboard_module._recent_records_payload = _clarified_recent_records_payload
    dashboard_module.DASHBOARD_HTML = _clarified_html(dashboard_module.DASHBOARD_HTML)
    _installed = True
