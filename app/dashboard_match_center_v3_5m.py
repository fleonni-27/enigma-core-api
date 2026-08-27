from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.dashboard_match_center_v3 import DASHBOARD_MATCH_CENTER_V3_HTML
from app.dashboard_match_center_v3_light import build_dashboard_match_center_v3
from app.dashboard_operations_v2 import BUSINESS_TIMEZONE

DASHBOARD_MATCH_CENTER_V3_REFRESH_MS = 300_000
DASHBOARD_MATCH_CENTER_V3_REFRESH_SECONDS = DASHBOARD_MATCH_CENTER_V3_REFRESH_MS // 1000
router = APIRouter(tags=["Dashboard Match Center V3"])

_NEWS_OLD = '''<div class="box"><div class="label">Notícias / alertas</div><div class="warning">${esc(f.news?.status||'—')}</div><div class="muted reason">${esc(f.news?.reason||'')}</div></div>'''
_NEWS_NEW = '''<div class="box"><div class="label">Fatos / alertas Enigma</div><div class="warning">${esc(f.news?.status||'—')}</div>${(f.news?.items||[]).map(x=>`<div class="reason">• ${esc(x)}</div>`).join('')||'<div class="muted reason">sem fato quantitativo adicional</div>'}<div class="muted reason" style="margin-top:7px">${esc(f.news?.reason||'')}</div></div>'''

DASHBOARD_MATCH_CENTER_V3_5M_HTML = DASHBOARD_MATCH_CENTER_V3_HTML.replace(
    "setInterval(load,60000)",
    f"setInterval(load,{DASHBOARD_MATCH_CENTER_V3_REFRESH_MS})",
).replace(
    "J1 automático · análise pré-jogo · RESEARCH ONLY",
    "J1 automático · análise pré-jogo · atualização do painel a cada 5 min · RESEARCH ONLY",
).replace(
    _NEWS_OLD,
    _NEWS_NEW,
)


def _business_today() -> date:
    return datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()


def _date_navigation() -> dict[str, str]:
    today = _business_today()
    tomorrow = today + timedelta(days=1)
    return {
        "today": today.isoformat(),
        "tomorrow": tomorrow.isoformat(),
    }


def _page_html(target_date: date | None) -> str:
    dates = _date_navigation()
    effective_date = target_date or date.fromisoformat(dates["today"])
    query = f"?target_date={effective_date.isoformat()}"
    phase_label = "PRÉ-J1 · D+1" if effective_date.isoformat() == dates["tomorrow"] else "DIA ATUAL"

    controls = (
        '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0 14px">'
        f'<a href="/dashboard/match-center-v3?target_date={dates["today"]}" '
        'style="color:#f6f8fb;text-decoration:none;border:1px solid #263746;border-radius:9px;padding:7px 10px">Hoje</a>'
        f'<a href="/dashboard/match-center-v3?target_date={dates["tomorrow"]}" '
        'style="color:#65b8ff;text-decoration:none;border:1px solid #263746;border-radius:9px;padding:7px 10px">Amanhã</a>'
        f'<span style="color:#91a2b2;font-size:12px">{phase_label} · {effective_date.isoformat()}</span>'
        '</div>'
    )

    html = DASHBOARD_MATCH_CENTER_V3_5M_HTML.replace(
        "fetch('/dashboard/api/match-center-v3'",
        f"fetch('/dashboard/api/match-center-v3{query}'",
    )
    html = html.replace(
        '<div id="app" class="grid"></div>',
        controls + '<div id="app" class="grid"></div>',
    )
    return html


@router.get("/dashboard/api/match-center-v3")
def dashboard_match_center_v3_api(target_date: date | None = Query(default=None)):
    payload = build_dashboard_match_center_v3(target_date=target_date)
    policy = dict(payload.get("policy") or {})
    policy["auto_refresh_seconds"] = DASHBOARD_MATCH_CENTER_V3_REFRESH_SECONDS
    policy["d1_preload_available"] = True
    policy["j1_predictions_remain_official_only_inside_j1_window"] = True
    policy["xg_xga_are_informational_only"] = True
    payload["policy"] = policy
    payload["date_navigation"] = _date_navigation()
    return payload


@router.get("/dashboard/match-center-v3", response_class=HTMLResponse, include_in_schema=False)
def dashboard_match_center_v3_page(
    target_date: date | None = Query(default=None),
) -> HTMLResponse:
    return HTMLResponse(_page_html(target_date))
