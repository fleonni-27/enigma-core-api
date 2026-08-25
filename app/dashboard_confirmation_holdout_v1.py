from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.dashboard_match_center_v3_5m import _page_html
from app.dashboard_match_center_v3_light import build_dashboard_match_center_v3
from app.enigma_rating_v2_confirmation_holdout import confirmation_holdout_dashboard_state

DASHBOARD_CONFIRMATION_HOLDOUT_VERSION = "dashboard_confirmation_holdout_v1"
router = APIRouter(tags=["Dashboard Match Center V3"])


def build_dashboard_with_confirmation_holdout(
    *, target_date: date | None = None
) -> dict[str, Any]:
    payload = build_dashboard_match_center_v3(target_date=target_date)
    fixtures = list(payload.get("fixtures") or [])
    state = confirmation_holdout_dashboard_state(fixtures)
    by_fixture_id = state.get("by_fixture_id") or {}

    enriched: list[dict[str, Any]] = []
    for item in fixtures:
        fixture_id = int(item["fixture_id"])
        enriched.append(
            {
                **item,
                "confirmation_holdout": by_fixture_id.get(fixture_id),
            }
        )

    policy = dict(payload.get("policy") or {})
    policy.update(
        {
            "confirmation_holdout_dashboard_connected": True,
            "confirmation_holdout_performance_peeking": False,
            "confirmation_holdout_progress_counts_settled_eligible_targets": True,
        }
    )
    return {
        **payload,
        "version": DASHBOARD_CONFIRMATION_HOLDOUT_VERSION,
        "fixtures": enriched,
        "confirmation_holdout": state.get("summary"),
        "policy": policy,
    }


def _inject_holdout_ui(html: str) -> str:
    css = r'''
.holdoutPanel{background:#0d1721;border:1px solid #263746;border-radius:14px;padding:14px 16px;margin:0 0 14px}.holdoutTop{display:flex;justify-content:space-between;gap:14px;align-items:center}.holdoutTitle{font-size:14px;font-weight:850}.holdoutCounter{font-size:22px;font-weight:900}.holdoutBar{height:9px;background:#1b2834;border-radius:99px;overflow:hidden;margin:10px 0 7px}.holdoutFill{height:100%;background:#65b8ff}.holdoutMeta{font-size:11px;color:#91a2b2;display:flex;gap:12px;flex-wrap:wrap}.holdoutBadge{border:1px solid #315c80;background:#0d2234;color:#8dcbff;border-radius:8px;padding:7px 9px;margin:0 0 12px;font-size:11px;font-weight:750}.holdoutBadge.wait{border-color:#66562e;background:#241f10;color:#f0c36a}.holdoutBadge.settled{border-color:#275f49;background:#10291f;color:#56d39a}@media(max-width:700px){.holdoutTop{align-items:flex-start;flex-direction:column}}
'''
    html = html.replace("</style></head>", css + "</style></head>")
    html = html.replace(
        '<div id="app" class="grid"></div>',
        '<div id="holdout"></div><div id="app" class="grid"></div>',
        1,
    )

    helpers = r'''
function renderHoldout(h){const el=document.getElementById('holdout');if(!el)return;if(!h){el.innerHTML='';return}const ready=!!h.ready_for_confirmation;const status=ready?'READY FOR CONFIRMATION':'ACCUMULATING · NO PEEKING';el.innerHTML=`<section class="holdoutPanel"><div class="holdoutTop"><div><div class="holdoutTitle">ENIGMA RATING V2 · CONFIRMATION HOLDOUT</div><div class="muted" style="margin-top:4px">${status}</div></div><div class="holdoutCounter">${esc(h.progress_counter||'0/100')}</div></div><div class="holdoutBar"><div class="holdoutFill" style="width:${Math.max(0,Math.min(100,Number(h.progress_pct||0)))}%"></div></div><div class="holdoutMeta"><span>Início ${esc(h.start_date)}</span><span>Capturados ${esc(h.captured_targets)}</span><span>Liquidados elegíveis ${esc(h.settled_eligible_targets)}</span><span>Restam ${esc(h.remaining_to_confirmation)}</span><span>Parâmetros FROZEN</span><span>SHA ${esc(String(h.selection_sha256||'').slice(0,12))}…</span><span>Métricas bloqueadas</span></div></section>`}
function decorateHoldoutFixtures(fs){const cards=[...document.querySelectorAll('#app .card')];(fs||[]).forEach((f,i)=>{const card=cards[i],h=f.confirmation_holdout;if(!card||!h||!h.candidate)return;const body=card.querySelector('.body');if(!body)return;let text='🧪 Confirmation candidate · aguardando J1';let cls='holdoutBadge wait';if(h.registered){text=`🧪 Confirmation Target #${h.target_number} · ${h.status}`;cls=h.status==='SETTLED_TARGET'?'holdoutBadge settled':'holdoutBadge'}const badge=document.createElement('div');badge.className=cls;badge.textContent=text+' · NO PEEKING';body.prepend(badge)})}
'''
    html = html.replace("async function load(){", helpers + "\nasync function load(){", 1)

    needle = "document.getElementById('app').innerHTML=(x.fixtures||[]).map(card).join('')||'<div class=\"muted\">Nenhum jogo-alvo hoje.</div>'"
    replacement = needle + ";renderHoldout(x.confirmation_holdout);decorateHoldoutFixtures(x.fixtures||[])"
    html = html.replace(needle, replacement, 1)
    return html


@router.get("/dashboard/api/match-center-v3")
def dashboard_match_center_confirmation_api(
    target_date: date | None = Query(default=None),
) -> dict[str, Any]:
    return build_dashboard_with_confirmation_holdout(target_date=target_date)


@router.get(
    "/dashboard/match-center-v3",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def dashboard_match_center_confirmation_page(
    target_date: date | None = Query(default=None),
) -> HTMLResponse:
    return HTMLResponse(_inject_holdout_ui(_page_html(target_date)))
