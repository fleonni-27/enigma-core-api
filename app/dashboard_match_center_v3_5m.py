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

_PROVIDER_HELPER = r'''
function providerLabel(v){if(Array.isArray(v))v=v[0];if(!v||typeof v!=='object')return '';return v.name||v.short_name||v.code||v.developer_name||v.description||''}
function providerNews(items){return (Array.isArray(items)?items:[]).slice(0,3).map(n=>{const t=n?.title||n?.name||n?.headline||n?.description||n?.data?.title||'';return t?`<div class="reason">• ${esc(t)}</div>`:''}).join('')}
function providerContext(f){const p=f.prematch_provider;if(!p)return `<div class="box" style="margin-top:12px"><div class="label">Contexto Sportmonks · J1</div><div class="muted reason">será materializado quando a janela J1 deste evento for processada</div></div>`;const c=p.counts||{},s=p.sections||{},avail=p.available_sections||[];const xg=(Array.isArray(s.xg_fixture)?s.xg_fixture:[]).slice(0,4).map(r=>{const v=r?.data?.value??r?.value;return v==null?'':`${esc(r.location||r.participant_id||'xG')} ${num(v,2)}`}).filter(Boolean).join(' · ');const comp=[providerLabel(s.season),providerLabel(s.stage),providerLabel(s.round),providerLabel(s.venue)].filter(Boolean).join(' · ');const summary=[`Escalações ${c.lineups||0}`,`Prováveis ${c.expected_lineups||0}`,`Desfalques ${c.sidelined||0}`,`Formações ${c.formations||0}`,`Notícias ${c.prematch_news||0}`,`Previsões API ${c.provider_predictions||0}`,`Stats ${c.statistics||0}`,`xG ${c.xg_fixture||0}`].join(' · ');return `<div class="box" style="margin-top:12px"><div class="label">Contexto Sportmonks · J1</div><div class="good reason">${esc(p.include_profile||'prematch')} · ${avail.length} seções disponíveis</div>${comp?`<div class="reason">${esc(comp)}</div>`:''}<div class="muted reason">${esc(summary)}</div>${xg?`<div class="reason"><b>xG fixture:</b> ${xg}</div>`:''}${providerNews(s.prematch_news)}<div class="muted reason" style="margin-top:7px">Dados capturados na J1 e lidos do cache. Informativos; não alteram o modelo STANDARD.</div></div>`}
'''

DASHBOARD_MATCH_CENTER_V3_5M_HTML = DASHBOARD_MATCH_CENTER_V3_HTML.replace(
    "setInterval(load,60000)",
    f"setInterval(load,{DASHBOARD_MATCH_CENTER_V3_REFRESH_MS})",
).replace(
    "J1 automático · análise pré-jogo · RESEARCH ONLY",
    "J1 automático · análise pré-jogo · atualização do painel a cada 5 min · RESEARCH ONLY",
).replace(
    _NEWS_OLD,
    _NEWS_NEW,
).replace(
    "function card(f){",
    _PROVIDER_HELPER + "\nfunction card(f){",
    1,
).replace(
    '</div><div class="extras">',
    '</div>${providerContext(f)}<div class="extras">',
    1,
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
    policy["rich_prematch_provider_context_is_cache_only"] = True
    payload["policy"] = policy
    payload["date_navigation"] = _date_navigation()
    return payload


@router.get("/dashboard/match-center-v3", response_class=HTMLResponse, include_in_schema=False)
def dashboard_match_center_v3_page(
    target_date: date | None = Query(default=None),
) -> HTMLResponse:
    return HTMLResponse(_page_html(target_date))
