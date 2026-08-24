from __future__ import annotations

from typing import Any

from app import dashboard as dashboard_module

DASHBOARD_CLARITY_VERSION = "dashboard_v1_2_1"
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


def _clarified_recent_records_payload(days: int, limit: int) -> dict[str, Any]:
    payload = _original_recent_records_payload(days, limit)
    payload["version"] = DASHBOARD_CLARITY_VERSION
    for item in payload.get("items") or []:
        item.update(_selection_context(item))
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
        "<th>Decisao</th><th>Sel.</th>": "<th>Ação</th><th>Seleção avaliada</th>",
        "<td><strong>${esc(r.decision)}</strong></td><td>${esc(r.selection||'—')}</td>": (
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
        '1 = mandante · X = empate · 2 = visitante.</p></div>'
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
