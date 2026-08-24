from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from app.daily_prediction_runner import (
    BUSINESS_TIMEZONE,
    DAILY_PREDICTION_RUNNER_VERSION,
    J1_PREDICTION_WINDOW,
    J1_TARGET_LEAD_MINUTES,
    PrematchContextSnapshot,
    ensure_prematch_context_schema,
)
from app.database import SessionLocal
from app.forward_test_ledger import DecisionRecord, ensure_forward_test_schema
from app.league_registry import canonical_league
from app.models import Fixture, OddsSnapshot, Prediction

DASHBOARD_OPERATIONS_V2_VERSION = "dashboard_operations_v2"
DEFAULT_MAX_LATENESS_MINUTES = 20

router = APIRouter(tags=["Dashboard Operations V2"])


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _business_today() -> date:
    return datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()


def _utc_bounds(target_date: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(BUSINESS_TIMEZONE)
    local_start = datetime.combine(target_date, time.min, tzinfo=tz)
    local_end = datetime.combine(target_date, time.max, tzinfo=tz)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _snapshot_window(target_date: date) -> str:
    return f"j1_45m_{target_date.strftime('%Y%m%d')}"


def _f(value: Any) -> float | None:
    return float(value) if value is not None else None


def _stage(
    *,
    now: datetime,
    starts_at: datetime,
    j1_due_at: datetime,
    has_decision: bool,
    has_prediction: bool,
    has_j1_odds: bool,
    has_context_snapshot: bool,
) -> str:
    if has_decision:
        return "J1_COMPLETE"
    if now < j1_due_at:
        return "WAITING_J1"
    if now >= starts_at:
        return "J1_NOT_RECORDED_BEFORE_KICKOFF"

    grace_end = j1_due_at + timedelta(minutes=DEFAULT_MAX_LATENESS_MINUTES)
    if now > grace_end:
        return "J1_WINDOW_MISSED"
    if has_prediction:
        return "PROCESSING_DECISION"
    if has_j1_odds:
        return "PROCESSING_PREDICTION"
    if has_context_snapshot:
        return "PROCESSING_J1"
    return "J1_DUE"


def build_dashboard_operations_v2(*, target_date: date | None = None) -> dict[str, Any]:
    ensure_forward_test_schema()
    ensure_prematch_context_schema()

    effective_date = target_date or _business_today()
    start_dt, end_dt = _utc_bounds(effective_date)
    now = datetime.now(timezone.utc)
    window = _snapshot_window(effective_date)

    with SessionLocal() as session:
        all_fixtures = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

        fixtures: list[Fixture] = []
        for fixture in all_fixtures:
            canonical = canonical_league(fixture.league_name)
            if canonical.get("target") and canonical.get("key"):
                fixtures.append(fixture)

        items: list[dict[str, Any]] = []
        stages: Counter[str] = Counter()

        for fixture in fixtures:
            starts_at = _aware_utc(fixture.starts_at)
            j1_due_at = starts_at - timedelta(minutes=J1_TARGET_LEAD_MINUTES)

            daily_odds_rows = int(
                session.scalar(
                    select(func.count(OddsSnapshot.id)).where(
                        OddsSnapshot.fixture_id == fixture.id
                    )
                )
                or 0
            )
            latest_daily_odds_at = session.scalar(
                select(func.max(OddsSnapshot.fetched_at)).where(
                    OddsSnapshot.fixture_id == fixture.id
                )
            )

            j1_odds_rows = int(
                session.scalar(
                    select(func.count(OddsSnapshot.id)).where(
                        OddsSnapshot.fixture_id == fixture.id,
                        OddsSnapshot.snapshot_window == window,
                    )
                )
                or 0
            )
            latest_j1_odds_at = session.scalar(
                select(func.max(OddsSnapshot.fetched_at)).where(
                    OddsSnapshot.fixture_id == fixture.id,
                    OddsSnapshot.snapshot_window == window,
                )
            )

            context = session.scalar(
                select(PrematchContextSnapshot)
                .where(
                    PrematchContextSnapshot.fixture_id == fixture.id,
                    PrematchContextSnapshot.snapshot_window == window,
                )
                .order_by(
                    PrematchContextSnapshot.fetched_at.desc(),
                    PrematchContextSnapshot.id.desc(),
                )
                .limit(1)
            )

            prediction = session.scalar(
                select(Prediction)
                .where(
                    Prediction.fixture_id == fixture.id,
                    Prediction.prediction_window == J1_PREDICTION_WINDOW,
                )
                .order_by(Prediction.generated_at.desc(), Prediction.id.desc())
                .limit(1)
            )

            decision = session.scalar(
                select(DecisionRecord)
                .where(
                    DecisionRecord.fixture_id == fixture.id,
                    DecisionRecord.snapshot_window == window,
                    DecisionRecord.source == DAILY_PREDICTION_RUNNER_VERSION,
                )
                .order_by(DecisionRecord.recorded_at.desc(), DecisionRecord.id.desc())
                .limit(1)
            )

            stage = _stage(
                now=now,
                starts_at=starts_at,
                j1_due_at=j1_due_at,
                has_decision=decision is not None,
                has_prediction=prediction is not None,
                has_j1_odds=j1_odds_rows > 0,
                has_context_snapshot=context is not None,
            )
            stages[stage] += 1

            canonical = canonical_league(fixture.league_name)
            probabilities = None
            if prediction is not None:
                probabilities = {
                    "home": _f(prediction.p_home),
                    "draw": _f(prediction.p_draw),
                    "away": _f(prediction.p_away),
                }
            elif decision is not None:
                raw = decision.raw_probabilities or {}
                probabilities = {
                    "home": _f(raw.get("1")),
                    "draw": _f(raw.get("X")),
                    "away": _f(raw.get("2")),
                }

            decision_payload = None
            if decision is not None:
                decision_payload = {
                    "record_id": int(decision.id),
                    "decision": decision.decision,
                    "selection": decision.selection,
                    "reason_codes": list(decision.reason_codes or []),
                    "bookmaker": decision.bookmaker,
                    "selected_odd": _f(decision.selected_odd),
                    "selected_no_vig_probability": _f(
                        decision.selected_no_vig_probability
                    ),
                    "calibrated_confidence": _f(
                        decision.calibrated_favorite_confidence
                    ),
                    "edge_pct": _f(decision.edge_percentage_points),
                    "expected_value_pct": _f(decision.expected_value_pct),
                    "recorded_at": decision.recorded_at.isoformat()
                    if decision.recorded_at
                    else None,
                    "settlement_status": decision.settlement_status,
                }

            items.append(
                {
                    "fixture_id": int(fixture.id),
                    "sportmonks_fixture_id": int(fixture.sportmonks_id),
                    "league": canonical.get("canonical_name") or fixture.league_name,
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "starts_at": starts_at.isoformat(),
                    "starts_at_local": starts_at.astimezone(
                        ZoneInfo(BUSINESS_TIMEZONE)
                    ).isoformat(),
                    "j1_due_at": j1_due_at.isoformat(),
                    "j1_due_at_local": j1_due_at.astimezone(
                        ZoneInfo(BUSINESS_TIMEZONE)
                    ).isoformat(),
                    "minutes_to_kickoff": round(
                        (starts_at - now).total_seconds() / 60.0, 2
                    ),
                    "minutes_to_j1": round(
                        (j1_due_at - now).total_seconds() / 60.0, 2
                    ),
                    "stage": stage,
                    "snapshot_window": window,
                    "steps": {
                        "fixture": {"status": "READY"},
                        "daily_odds": {
                            "status": "READY" if daily_odds_rows > 0 else "MISSING",
                            "rows": daily_odds_rows,
                            "latest_fetched_at": latest_daily_odds_at.isoformat()
                            if latest_daily_odds_at
                            else None,
                        },
                        "lineups": {
                            "status": (
                                "READY"
                                if context is not None and context.lineup_count > 0
                                else "NOT_AVAILABLE"
                                if context is not None
                                else "WAITING"
                            ),
                            "count": int(context.lineup_count) if context else 0,
                            "latest_fetched_at": context.fetched_at.isoformat()
                            if context and context.fetched_at
                            else None,
                        },
                        "j1_odds": {
                            "status": "READY" if j1_odds_rows > 0 else "WAITING",
                            "rows": j1_odds_rows,
                            "latest_fetched_at": latest_j1_odds_at.isoformat()
                            if latest_j1_odds_at
                            else None,
                        },
                        "prediction": {
                            "status": "READY" if prediction is not None else "WAITING",
                            "prediction_id": int(prediction.id) if prediction else None,
                            "prediction_window": prediction.prediction_window
                            if prediction
                            else J1_PREDICTION_WINDOW,
                            "generated_at": prediction.generated_at.isoformat()
                            if prediction and prediction.generated_at
                            else None,
                        },
                        "decision": {
                            "status": "READY" if decision is not None else "WAITING"
                        },
                        "ledger": {
                            "status": "READY" if decision is not None else "WAITING",
                            "record_id": int(decision.id) if decision else None,
                        },
                    },
                    "probabilities": probabilities,
                    "decision": decision_payload,
                }
            )

    future_due = [
        item for item in items if datetime.fromisoformat(item["j1_due_at"]) >= now
    ]
    next_j1 = min(future_due, key=lambda item: item["j1_due_at"]) if future_due else None

    return {
        "status": "ok",
        "version": DASHBOARD_OPERATIONS_V2_VERSION,
        "generated_at": now.isoformat(),
        "timezone": BUSINESS_TIMEZONE,
        "target_date": effective_date.isoformat(),
        "snapshot_window": window,
        "overview": {
            "target_fixtures": len(items),
            "j1_complete": stages["J1_COMPLETE"],
            "waiting_j1": stages["WAITING_J1"],
            "j1_due_or_processing": sum(
                stages[key]
                for key in (
                    "J1_DUE",
                    "PROCESSING_J1",
                    "PROCESSING_PREDICTION",
                    "PROCESSING_DECISION",
                )
            ),
            "attention": stages["J1_WINDOW_MISSED"]
            + stages["J1_NOT_RECORDED_BEFORE_KICKOFF"],
            "next_j1": {
                "sportmonks_fixture_id": next_j1["sportmonks_fixture_id"],
                "match": f"{next_j1['home_team']} x {next_j1['away_team']}",
                "due_at_local": next_j1["j1_due_at_local"],
            }
            if next_j1
            else None,
        },
        "stage_counts": dict(stages),
        "fixtures": items,
        "policy": {
            "read_only": True,
            "auto_refresh_seconds": 60,
            "j1_target_lead_minutes": J1_TARGET_LEAD_MINUTES,
            "j1_max_lateness_minutes": DEFAULT_MAX_LATENESS_MINUTES,
            "lineups_used_by_current_model": False,
            "research_only": True,
            "real_money_execution_enabled": False,
        },
    }


@router.get(
    "/dashboard/operations-v2",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def dashboard_operations_v2_page() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_OPERATIONS_V2_HTML)


@router.get("/dashboard/api/operations-v2/today")
def dashboard_operations_v2_api(
    target_date: date | None = Query(default=None),
) -> dict[str, Any]:
    return build_dashboard_operations_v2(target_date=target_date)


DASHBOARD_OPERATIONS_V2_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Enigma Core — Dashboard Operations V2</title>
<style>
:root{--bg:#081018;--panel:#101b26;--panel2:#0d1721;--text:#e9f0f6;--muted:#8fa2b5;--line:#223446;--good:#54d39a;--warn:#f0c36a;--bad:#ff7b7b;--blue:#6fb6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}.wrap{max-width:1500px;margin:auto;padding:26px}.top{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:18px}h1{margin:0;font-size:27px}.muted{color:var(--muted)}.controls{display:flex;gap:9px;align-items:center}.badge{border:1px solid var(--line);background:#0f1b26;border-radius:10px;padding:8px 11px;font-size:12px}.overview{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:11px;margin-bottom:16px}.metric,.match{background:var(--panel);border:1px solid var(--line);border-radius:14px}.metric{padding:14px}.metric .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.metric .value{font-size:24px;font-weight:750;margin-top:6px}.matches{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.match{padding:16px}.matchhead{display:flex;justify-content:space-between;gap:12px}.league{font-size:12px;color:var(--muted)}.teams{font-size:18px;font-weight:720;margin-top:4px}.time{font-size:13px;text-align:right}.stage{display:inline-flex;margin-top:9px;border:1px solid var(--line);border-radius:8px;padding:4px 8px;font-size:11px;font-weight:700}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.blue{color:var(--blue)}.steps{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px;margin-top:14px}.step{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:8px 6px;text-align:center}.step b{display:block;font-size:11px}.step span{display:block;font-size:10px;color:var(--muted);margin-top:4px}.detail{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.box{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px;font-size:12px}.prob{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-top:7px}.prob div{border:1px solid var(--line);border-radius:7px;padding:6px;text-align:center}.decision{font-weight:800;font-size:17px}.reasons{font-size:11px;color:var(--muted);margin-top:5px}.empty,.loading{padding:40px;text-align:center;color:var(--muted)}@media(max-width:1100px){.overview{grid-template-columns:repeat(2,1fr)}.matches{grid-template-columns:1fr}.steps{grid-template-columns:repeat(4,1fr)}}@media(max-width:650px){.wrap{padding:14px}.top{align-items:flex-start;flex-direction:column}.overview{grid-template-columns:1fr 1fr}.steps{grid-template-columns:repeat(2,1fr)}.detail{grid-template-columns:1fr}}
</style></head>
<body><div class="wrap">
<div class="top"><div><h1>Enigma Core · Operations V2</h1><div class="muted">Jogos de hoje · J1 a 45 minutos · atualização automática</div></div><div class="controls"><span class="badge">RESEARCH ONLY</span><span id="updated" class="badge">Carregando...</span></div></div>
<div id="content"><div class="loading">Carregando operação do dia...</div></div>
</div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=v=>v==null?'—':(Number(v)*100).toLocaleString('pt-BR',{minimumFractionDigits:1,maximumFractionDigits:1})+'%';
const num=(v,d=1)=>v==null?'—':Number(v).toLocaleString('pt-BR',{minimumFractionDigits:d,maximumFractionDigits:d});
const localTime=iso=>iso?new Date(iso).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit',timeZone:'America/Sao_Paulo'}):'—';
function stageClass(s){return s==='J1_COMPLETE'?'good':s==='WAITING_J1'?'blue':s.includes('MISSED')||s.includes('NOT_RECORDED')?'bad':'warn'}
function stageLabel(s){return ({J1_COMPLETE:'J1 PROCESSADA',WAITING_J1:'AGUARDANDO J1',J1_DUE:'J1 DISPONÍVEL',PROCESSING_J1:'PROCESSANDO J1',PROCESSING_PREDICTION:'GERANDO PREVISÃO',PROCESSING_DECISION:'GERANDO DECISÃO',J1_WINDOW_MISSED:'JANELA J1 PERDIDA',J1_NOT_RECORDED_BEFORE_KICKOFF:'J1 NÃO REGISTRADA'})[s]||s}
function step(label,data,extra=''){const ready=data?.status==='READY';const cls=ready?'good':data?.status==='MISSING'?'bad':'warn';const icon=ready?'✓':data?.status==='MISSING'?'✕':'…';return `<div class="step"><b class="${cls}">${icon} ${esc(label)}</b><span>${esc(extra||data?.status||'')}</span></div>`}
function card(x){const d=x.decision,p=x.probabilities,s=x.steps;const reasons=(d?.reason_codes||[]).join(' · ');return `<div class="match">
<div class="matchhead"><div><div class="league">${esc(x.league)} · ID ${x.sportmonks_fixture_id}</div><div class="teams">${esc(x.home_team)} × ${esc(x.away_team)}</div><span class="stage ${stageClass(x.stage)}">${esc(stageLabel(x.stage))}</span></div><div class="time"><div>Kickoff <b>${localTime(x.starts_at_local)}</b></div><div class="muted">J1 ${localTime(x.j1_due_at_local)}</div></div></div>
<div class="steps">${step('Fixture',s.fixture)}${step('Odds dia',s.daily_odds,String(s.daily_odds.rows||0))}${step('Escalações',s.lineups,String(s.lineups.count||0))}${step('Odds J1',s.j1_odds,String(s.j1_odds.rows||0))}${step('Prediction',s.prediction)}${step('Decision',s.decision)}${step('Ledger',s.ledger)}</div>
<div class="detail"><div class="box"><b>Probabilidade Enigma</b><div class="prob"><div>1<br><b>${pct(p?.home)}</b></div><div>X<br><b>${pct(p?.draw)}</b></div><div>2<br><b>${pct(p?.away)}</b></div></div></div><div class="box"><b>Decision Engine</b><div class="decision ${d?.decision==='BET'?'good':d?.decision==='NO_BET'?'warn':'muted'}">${esc(d?.decision||'AGUARDANDO')}</div><div>Confiança: ${pct(d?.calibrated_confidence)} · Odd: ${num(d?.selected_odd,2)}</div><div>Edge: ${d?.edge_pct==null?'—':num(d.edge_pct,1)+' pp'} · EV: ${d?.expected_value_pct==null?'—':num(d.expected_value_pct,1)+'%'}</div><div class="reasons">${esc(reasons||'Sem decisão registrada ainda')}</div></div></div>
</div>`}
async function load(){try{const res=await fetch('/dashboard/api/operations-v2/today',{cache:'no-store'});if(!res.ok)throw new Error('HTTP '+res.status);const x=await res.json(),o=x.overview;document.getElementById('updated').textContent='Atualizado '+localTime(x.generated_at);const next=o.next_j1?`${esc(o.next_j1.match)} · ${localTime(o.next_j1.due_at_local)}`:'—';document.getElementById('content').innerHTML=`<div class="overview"><div class="metric"><div class="label">Jogos-alvo</div><div class="value">${o.target_fixtures}</div></div><div class="metric"><div class="label">J1 concluídas</div><div class="value good">${o.j1_complete}</div></div><div class="metric"><div class="label">Aguardando J1</div><div class="value blue">${o.waiting_j1}</div></div><div class="metric"><div class="label">Atenção</div><div class="value ${o.attention?'bad':''}">${o.attention}</div></div><div class="metric"><div class="label">Próxima J1</div><div style="margin-top:7px;font-weight:700">${next}</div></div></div><div class="matches">${x.fixtures.length?x.fixtures.map(card).join(''):'<div class="empty">Nenhum jogo-alvo hoje.</div>'}</div>`}catch(e){document.getElementById('content').innerHTML=`<div class="empty bad">Falha ao carregar Dashboard Operations V2: ${esc(e.message)}</div>`}}
load();setInterval(load,60000);
</script></body></html>"""
