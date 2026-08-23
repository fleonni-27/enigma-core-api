from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.database import SessionLocal
from app.forward_test_ledger import (
    DecisionRecord,
    ensure_forward_test_schema,
    router as forward_test_router,
)

DASHBOARD_VERSION = "dashboard_v1_2"
DEFAULT_WINDOW_DAYS = 90
MAX_WINDOW_DAYS = 3650
MAX_RECENT_RECORDS = 100
SAMPLE_MIN_SETTLED_RECORDS = 30
SAMPLE_MIN_SETTLED_BETS = 10

router = APIRouter(tags=["Dashboard"])
_routes_installed = False


def _window_filters(days: int) -> list[Any]:
    if days <= 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [DecisionRecord.recorded_at >= cutoff]


def _as_float(value: Any) -> float:
    return 0.0 if value is None else float(value)


def _percentage(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _counter_entries(counter: Counter[str], limit: int = 12) -> list[dict[str, Any]]:
    return [
        {"key": key, "count": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _sample_health(settled_records: int, settled_bets: int) -> dict[str, Any]:
    if settled_records < SAMPLE_MIN_SETTLED_RECORDS:
        status = "INSUFFICIENT"
        message = (
            "Amostra insuficiente para avaliar desempenho da politica. "
            "Os resultados atuais sao apenas observacionais."
        )
    elif settled_bets < SAMPLE_MIN_SETTLED_BETS:
        status = "LIMITED_BET_SAMPLE"
        message = (
            "Ha volume de decisoes liquidadas, mas ainda poucas BETs para avaliar "
            "assertividade, ROI e P&L da politica de entrada."
        )
    else:
        status = "OBSERVATIONAL_READY"
        message = (
            "Amostra minima operacional atingida para revisao descritiva. "
            "Isso nao equivale a significancia estatistica nem validacao financeira."
        )

    return {
        "status": status,
        "message": message,
        "settled_records": settled_records,
        "settled_bets": settled_bets,
        "minimum_settled_records": SAMPLE_MIN_SETTLED_RECORDS,
        "minimum_settled_bets": SAMPLE_MIN_SETTLED_BETS,
        "settled_records_progress_pct": min(
            100.0, _percentage(settled_records, SAMPLE_MIN_SETTLED_RECORDS)
        ),
        "settled_bets_progress_pct": min(
            100.0, _percentage(settled_bets, SAMPLE_MIN_SETTLED_BETS)
        ),
        "remaining_settled_records": max(
            0, SAMPLE_MIN_SETTLED_RECORDS - settled_records
        ),
        "remaining_settled_bets": max(0, SAMPLE_MIN_SETTLED_BETS - settled_bets),
        "descriptive_review_ready": status == "OBSERVATIONAL_READY",
        "statistical_validation_claimed": False,
    }


def _summary_payload(days: int) -> dict[str, Any]:
    ensure_forward_test_schema()
    filters = _window_filters(days)

    with SessionLocal() as session:
        rows = session.scalars(
            select(DecisionRecord)
            .where(*filters)
            .order_by(DecisionRecord.recorded_at.asc(), DecisionRecord.id.asc())
        ).all()

    total_records = len(rows)
    bet_rows = [row for row in rows if row.decision == "BET"]
    no_bet_rows = [row for row in rows if row.decision == "NO_BET"]
    settled_rows = [row for row in rows if row.settlement_status == "SETTLED"]
    pending_rows = [row for row in rows if row.settlement_status == "UNSETTLED"]
    settled_bet_rows = [
        row for row in bet_rows if row.settlement_status == "SETTLED"
    ]
    settled_no_bet_rows = [
        row for row in no_bet_rows if row.settlement_status == "SETTLED"
    ]

    wins = sum(1 for row in settled_bet_rows if row.selection_won == "true")
    losses = sum(1 for row in settled_bet_rows if row.selection_won == "false")
    pnl_units = sum(_as_float(row.hypothetical_pnl_units) for row in settled_bet_rows)

    avg_odd = _avg(
        [_as_float(row.selected_odd) for row in bet_rows if row.selected_odd is not None]
    )
    avg_edge_pct = _avg(
        [
            _as_float(row.edge_percentage_points)
            for row in bet_rows
            if row.edge_percentage_points is not None
        ]
    )
    avg_ev_pct = _avg(
        [
            _as_float(row.expected_value_pct)
            for row in bet_rows
            if row.expected_value_pct is not None
        ]
    )

    avoided_losses = sum(
        1
        for row in settled_no_bet_rows
        if row.counterfactual_pnl_units is not None
        and _as_float(row.counterfactual_pnl_units) < 0
    )
    missed_wins = sum(
        1
        for row in settled_no_bet_rows
        if row.counterfactual_pnl_units is not None
        and _as_float(row.counterfactual_pnl_units) > 0
    )
    neutral_no_bets = sum(
        1
        for row in settled_no_bet_rows
        if row.counterfactual_pnl_units is not None
        and _as_float(row.counterfactual_pnl_units) == 0
    )
    no_bet_counterfactual_pnl = sum(
        _as_float(row.counterfactual_pnl_units)
        for row in settled_no_bet_rows
        if row.counterfactual_pnl_units is not None
    )
    policy_pnl = sum(
        _as_float(row.hypothetical_pnl_units)
        for row in settled_rows
        if row.hypothetical_pnl_units is not None
    )
    all_counterfactual_pnl = sum(
        _as_float(row.counterfactual_pnl_units)
        for row in settled_rows
        if row.counterfactual_pnl_units is not None
    )
    policy_advantage = policy_pnl - all_counterfactual_pnl

    reason_counts: Counter[str] = Counter()
    no_bet_reason_counts: Counter[str] = Counter()
    no_bet_signature_counts: Counter[str] = Counter()
    single_blocker_no_bets = 0
    multi_blocker_no_bets = 0

    for row in rows:
        reasons = [str(reason) for reason in (row.reason_codes or [])]
        for reason in reasons:
            reason_counts[reason] += 1
        if row.decision == "NO_BET":
            for reason in reasons:
                no_bet_reason_counts[reason] += 1
            signature = " + ".join(sorted(reasons)) if reasons else "NO_REASON_CODE"
            no_bet_signature_counts[signature] += 1
            if len(reasons) == 1:
                single_blocker_no_bets += 1
            elif len(reasons) > 1:
                multi_blocker_no_bets += 1

    league_groups: dict[str, list[DecisionRecord]] = defaultdict(list)
    for row in bet_rows:
        league_groups[row.league or "Unknown"].append(row)

    league_breakdown: list[dict[str, Any]] = []
    for league, league_bets in sorted(
        league_groups.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        settled = [row for row in league_bets if row.settlement_status == "SETTLED"]
        league_wins = sum(1 for row in settled if row.selection_won == "true")
        league_pnl = sum(
            _as_float(row.hypothetical_pnl_units)
            for row in settled
            if row.hypothetical_pnl_units is not None
        )
        league_breakdown.append(
            {
                "league": league,
                "bets": len(league_bets),
                "settled_bets": len(settled),
                "wins": league_wins,
                "hit_rate_pct": _percentage(league_wins, len(settled)),
                "pnl_units": round(league_pnl, 4),
                "roi_pct": _percentage(league_pnl, len(settled)),
            }
        )

    curve_source = sorted(
        [row for row in settled_bet_rows if row.settled_at is not None],
        key=lambda row: row.settled_at,
    )[-250:]
    cumulative = 0.0
    pnl_curve: list[dict[str, Any]] = []
    for row in curve_source:
        pnl = _as_float(row.hypothetical_pnl_units)
        cumulative += pnl
        pnl_curve.append(
            {
                "settled_at": row.settled_at.isoformat() if row.settled_at else None,
                "match": f"{row.home_team} x {row.away_team}",
                "pnl_units": round(pnl, 4),
                "cumulative_pnl_units": round(cumulative, 4),
            }
        )

    settled_records = len(settled_rows)
    settled_bets = len(settled_bet_rows)
    dominant_reason = None
    if no_bet_reason_counts:
        dominant_reason = sorted(
            no_bet_reason_counts.items(), key=lambda item: (-item[1], item[0])
        )[0][0]

    return {
        "status": "ok",
        "version": DASHBOARD_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "execution_mode": "RESEARCH_ONLY",
        "sample_health": _sample_health(settled_records, settled_bets),
        "coverage": {
            "total_records": total_records,
            "settled_records": settled_records,
            "pending_records": len(pending_rows),
            "settlement_rate_pct": _percentage(settled_records, total_records),
            "bet_rate_pct": _percentage(len(bet_rows), total_records),
            "no_bet_rate_pct": _percentage(len(no_bet_rows), total_records),
        },
        "kpis": {
            "total_records": total_records,
            "bet_records": len(bet_rows),
            "no_bet_records": len(no_bet_rows),
            "settled_bets": settled_bets,
            "pending_records": len(pending_rows),
            "wins": wins,
            "losses": losses,
            "hit_rate_pct": _percentage(wins, settled_bets),
            "pnl_units": round(pnl_units, 4),
            "roi_pct": _percentage(pnl_units, settled_bets),
            "avg_odd": round(avg_odd, 4),
            "avg_edge_pct": round(avg_edge_pct, 3),
            "avg_ev_pct": round(avg_ev_pct, 3),
        },
        "decision_quality": {
            "settled_no_bets": len(settled_no_bet_rows),
            "avoided_losses": avoided_losses,
            "missed_wins": missed_wins,
            "neutral_no_bets": neutral_no_bets,
            "avoided_loss_rate_pct": _percentage(
                avoided_losses, len(settled_no_bet_rows)
            ),
            "missed_win_rate_pct": _percentage(missed_wins, len(settled_no_bet_rows)),
            "no_bet_counterfactual_pnl_units": round(no_bet_counterfactual_pnl, 4),
            "policy_pnl_units": round(policy_pnl, 4),
            "all_selections_counterfactual_pnl_units": round(
                all_counterfactual_pnl, 4
            ),
            "policy_advantage_units": round(policy_advantage, 4),
        },
        "decision_diagnostics": {
            "dominant_no_bet_reason": dominant_reason,
            "single_blocker_no_bets": single_blocker_no_bets,
            "multi_blocker_no_bets": multi_blocker_no_bets,
            "reason_code_counts": _counter_entries(reason_counts),
            "no_bet_reason_code_counts": _counter_entries(no_bet_reason_counts),
            "no_bet_signatures": _counter_entries(no_bet_signature_counts),
        },
        "league_breakdown": league_breakdown,
        "pnl_curve": pnl_curve,
        "policy": {
            "read_only": True,
            "real_money_execution_enabled": False,
            "roi_denominator": "one hypothetical unit per settled BET record",
            "decision_quality_baseline": (
                "counterfactual assumes one unit on every selected 1X2 outcome, "
                "including NO_BET records"
            ),
            "policy_advantage_definition": (
                "policy P&L minus all-selections counterfactual P&L"
            ),
            "sample_health_thresholds_are_operational_guardrails": True,
            "sample_health_is_not_statistical_significance": True,
        },
    }


def _recent_records_payload(days: int, limit: int) -> dict[str, Any]:
    ensure_forward_test_schema()
    filters = _window_filters(days)

    with SessionLocal() as session:
        rows = session.scalars(
            select(DecisionRecord)
            .where(*filters)
            .order_by(DecisionRecord.recorded_at.desc(), DecisionRecord.id.desc())
            .limit(limit)
        ).all()

    items: list[dict[str, Any]] = []
    for record in rows:
        selection_won: bool | None = None
        if record.selection_won == "true":
            selection_won = True
        elif record.selection_won == "false":
            selection_won = False

        items.append(
            {
                "record_id": int(record.id),
                "recorded_at": record.recorded_at.isoformat()
                if record.recorded_at
                else None,
                "fixture_starts_at": record.fixture_starts_at.isoformat()
                if record.fixture_starts_at
                else None,
                "sportmonks_fixture_id": int(record.sportmonks_fixture_id),
                "league": record.league,
                "home_team": record.home_team,
                "away_team": record.away_team,
                "decision": record.decision,
                "selection": record.selection,
                "reason_codes": list(record.reason_codes or []),
                "bookmaker": record.bookmaker,
                "selected_odd": _as_float(record.selected_odd)
                if record.selected_odd is not None
                else None,
                "edge_pct": _as_float(record.edge_percentage_points)
                if record.edge_percentage_points is not None
                else None,
                "expected_value_pct": _as_float(record.expected_value_pct)
                if record.expected_value_pct is not None
                else None,
                "calibrated_confidence": _as_float(
                    record.calibrated_favorite_confidence
                )
                if record.calibrated_favorite_confidence is not None
                else None,
                "settlement_status": record.settlement_status,
                "actual_result": record.actual_result,
                "selection_won": selection_won,
                "pnl_units": _as_float(record.hypothetical_pnl_units)
                if record.hypothetical_pnl_units is not None
                else None,
                "counterfactual_pnl_units": _as_float(
                    record.counterfactual_pnl_units
                )
                if record.counterfactual_pnl_units is not None
                else None,
            }
        )

    return {
        "status": "ok",
        "version": DASHBOARD_VERSION,
        "window_days": days,
        "limit": limit,
        "items": items,
    }


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


@router.get("/dashboard/api/summary")
def dashboard_summary(
    days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=0, le=MAX_WINDOW_DAYS),
) -> dict[str, Any]:
    return _summary_payload(days)


@router.get("/dashboard/api/records")
def dashboard_records(
    days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=0, le=MAX_WINDOW_DAYS),
    limit: int = Query(default=25, ge=1, le=MAX_RECENT_RECORDS),
) -> dict[str, Any]:
    return _recent_records_payload(days, limit)


def install_dashboard_routes() -> None:
    global _routes_installed
    if _routes_installed:
        return
    forward_test_router.include_router(router)
    _routes_installed = True


DASHBOARD_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Enigma Core — Dashboard</title>
<style>
:root{--bg:#081018;--panel:#101b26;--panel2:#132230;--text:#e9f0f6;--muted:#8fa2b5;--line:#223446;--good:#54d39a;--bad:#ff7b7b;--warn:#f0c36a;--accent:#79a8ff}
*{box-sizing:border-box}body{margin:0;background:#081018;color:var(--text);font-family:Inter,system-ui,sans-serif}.wrap{max-width:1440px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:20px}.brand h1{margin:0;font-size:30px}.brand p{margin:7px 0 0;color:var(--muted)}.controls{display:flex;gap:10px;align-items:center}.badge{border:1px solid #34506a;background:#0f1e2a;color:#9bc5ff;padding:8px 11px;border-radius:999px;font-size:12px;font-weight:700}.select{background:#0f1b26;color:var(--text);border:1px solid var(--line);padding:9px 12px;border-radius:10px}.sample{border:1px solid #665127;background:#251f13;border-radius:14px;padding:15px 17px;margin-bottom:16px}.sample strong{color:var(--warn)}.sample p{margin:6px 0 0;color:#c9b98f;font-size:13px}.grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:16px}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.value{font-size:28px;font-weight:760;margin-top:9px}.sub{font-size:12px;color:var(--muted);margin-top:5px}.section{display:grid;grid-template-columns:1.2fr 1fr;gap:16px;margin-top:16px}.panel h2{font-size:15px;margin:0 0 13px}.good{color:var(--good)}.bad{color:var(--bad)}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:13px}th{color:var(--muted);text-align:left;font-weight:600;border-bottom:1px solid var(--line);padding:9px 7px}td{border-bottom:1px solid rgba(34,52,70,.65);padding:10px 7px;vertical-align:top}.right{text-align:right}.pill{display:inline-flex;padding:3px 7px;border:1px solid #2b4459;background:#152635;border-radius:7px;font-size:11px}.records{margin-top:16px}.scroll{overflow:auto}.loading,.empty{padding:34px;text-align:center;color:var(--muted)}.progress{height:7px;border-radius:99px;background:#172634;overflow:hidden;margin-top:8px}.progress span{display:block;height:100%;background:#79a8ff}.reason{font-size:11px;color:var(--muted);max-width:380px;white-space:normal}.chart{height:220px;width:100%;display:block}.line{fill:none;stroke:#79a8ff;stroke-width:3;vector-effect:non-scaling-stroke}.zero{stroke:#506579;stroke-dasharray:5 5}.error{padding:18px;border:1px solid #683b3b;background:#261616;border-radius:12px;color:#ffb4b4}
@media(max-width:1050px){.grid{grid-template-columns:repeat(3,1fr)}.grid4{grid-template-columns:repeat(2,1fr)}.section{grid-template-columns:1fr}}@media(max-width:650px){.wrap{padding:16px}.top{align-items:flex-start;flex-direction:column}.grid,.grid4{grid-template-columns:repeat(2,1fr)}.value{font-size:23px}}
</style>
</head>
<body>
<div class="wrap">
<div class="top"><div class="brand"><h1>Enigma Core</h1><p>Decision Intelligence Dashboard · Forward Test</p></div><div class="controls"><span class="badge">RESEARCH ONLY</span><select id="days" class="select"><option value="7">7 dias</option><option value="30">30 dias</option><option value="90" selected>90 dias</option><option value="365">1 ano</option><option value="0">Todo periodo</option></select></div></div>
<div id="content"><div class="loading">Carregando metricas...</div></div>
</div>
<script>
const fmt=(v,d=2)=>Number(v??0).toLocaleString('pt-BR',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=(s)=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cls=(v)=>Number(v)>0?'good':Number(v)<0?'bad':'';
function chart(curve){if(!curve?.length)return '<div class="empty">Sem BETs liquidadas no periodo.</div>';const vals=curve.map(x=>Number(x.cumulative_pnl_units||0)),w=800,h=200,p=20,min=Math.min(0,...vals),max=Math.max(0,...vals),span=(max-min)||1;const pts=vals.map((v,i)=>`${p+i*(w-2*p)/Math.max(1,vals.length-1)},${p+(max-v)*(h-2*p)/span}`).join(' ');const zy=p+(max*(h-2*p)/span);return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="zero" x1="${p}" x2="${w-p}" y1="${zy}" y2="${zy}"/><polyline class="line" points="${pts}"/></svg>`}
function reasonRows(rows){if(!rows?.length)return '<tr><td colspan="2" class="muted">Sem bloqueadores registrados.</td></tr>';return rows.map(r=>`<tr><td>${esc(r.key)}</td><td class="right">${r.count}</td></tr>`).join('')}
function leagueRows(rows){if(!rows?.length)return '<tr><td colspan="5" class="muted">Sem BETs no periodo.</td></tr>';return rows.map(r=>`<tr><td>${esc(r.league)}</td><td class="right">${r.settled_bets}</td><td class="right">${fmt(r.hit_rate_pct,1)}%</td><td class="right ${cls(r.pnl_units)}">${fmt(r.pnl_units)}u</td><td class="right ${cls(r.roi_pct)}">${fmt(r.roi_pct,1)}%</td></tr>`).join('')}
function recordRows(rows){if(!rows?.length)return '<tr><td colspan="10" class="muted">Nenhum registro.</td></tr>';return rows.map(r=>{const result=r.selection_won===true?'WIN':r.selection_won===false?'LOSS':r.settlement_status;const reasons=(r.reason_codes||[]).join(', ');return `<tr><td>${esc((r.recorded_at||'').slice(0,16).replace('T',' '))}</td><td>${esc(r.league||'—')}</td><td>${esc(r.home_team)} x ${esc(r.away_team)}<div class="reason">${esc(reasons)}</div></td><td><strong>${esc(r.decision)}</strong></td><td>${esc(r.selection||'—')}</td><td class="right">${r.selected_odd==null?'—':fmt(r.selected_odd)}</td><td class="right">${r.edge_pct==null?'—':fmt(r.edge_pct,1)+'pp'}</td><td><span class="pill">${esc(result)}</span></td><td class="right ${cls(r.pnl_units)}">${r.pnl_units==null?'—':fmt(r.pnl_units)+'u'}</td><td class="right ${cls(r.counterfactual_pnl_units)}">${r.counterfactual_pnl_units==null?'—':fmt(r.counterfactual_pnl_units)+'u'}</td></tr>`}).join('')}
async function load(){const days=document.getElementById('days').value;document.getElementById('content').innerHTML='<div class="loading">Atualizando...</div>';try{const [sres,rres]=await Promise.all([fetch(`/dashboard/api/summary?days=${days}`),fetch(`/dashboard/api/records?days=${days}&limit=30`)]);if(!sres.ok||!rres.ok)throw new Error(`HTTP ${sres.status}/${rres.status}`);const s=await sres.json(),r=await rres.json(),k=s.kpis,q=s.decision_quality,d=s.decision_diagnostics,h=s.sample_health,c=s.coverage;document.getElementById('content').innerHTML=`
<div class="sample"><strong>${esc(h.status)}</strong> · ${h.settled_records}/${h.minimum_settled_records} decisoes liquidadas · ${h.settled_bets}/${h.minimum_settled_bets} BETs liquidadas<p>${esc(h.message)}</p><div class="progress"><span style="width:${h.settled_records_progress_pct}%"></span></div></div>
<div class="grid">
<div class="card"><div class="label">Registros</div><div class="value">${k.total_records}</div><div class="sub">${fmt(c.settlement_rate_pct,1)}% liquidados</div></div>
<div class="card"><div class="label">BET rate</div><div class="value">${fmt(c.bet_rate_pct,1)}%</div><div class="sub">${k.bet_records} BET · ${k.no_bet_records} NO_BET</div></div>
<div class="card"><div class="label">BETs liquidadas</div><div class="value">${k.settled_bets}</div><div class="sub">${k.pending_records} pendentes</div></div>
<div class="card"><div class="label">Assertividade</div><div class="value">${fmt(k.hit_rate_pct,1)}%</div><div class="sub">${k.wins}W · ${k.losses}L</div></div>
<div class="card"><div class="label">P&L politica</div><div class="value ${cls(k.pnl_units)}">${fmt(k.pnl_units)}u</div><div class="sub">BETs executadas</div></div>
<div class="card"><div class="label">ROI</div><div class="value ${cls(k.roi_pct)}">${fmt(k.roi_pct,1)}%</div><div class="sub">Somente BETs liquidadas</div></div>
</div>
<div class="grid4">
<div class="card"><div class="label">Perdas evitadas</div><div class="value good">${q.avoided_losses}</div><div class="sub">${fmt(q.avoided_loss_rate_pct,1)}% dos NO_BET liquidados</div></div>
<div class="card"><div class="label">Ganhos perdidos</div><div class="value bad">${q.missed_wins}</div><div class="sub">${fmt(q.missed_win_rate_pct,1)}% dos NO_BET liquidados</div></div>
<div class="card"><div class="label">Contrafactual NO_BET</div><div class="value ${cls(q.no_bet_counterfactual_pnl_units)}">${fmt(q.no_bet_counterfactual_pnl_units)}u</div><div class="sub">Se todos fossem apostados</div></div>
<div class="card"><div class="label">Vantagem da politica</div><div class="value ${cls(q.policy_advantage_units)}">${fmt(q.policy_advantage_units)}u</div><div class="sub">Politica vs apostar em todas</div></div>
</div>
<div class="section"><div class="panel"><h2>Diagnostics · bloqueadores NO_BET</h2><div class="sub" style="margin-bottom:10px">Dominante: ${esc(d.dominant_no_bet_reason||'—')} · ${d.single_blocker_no_bets} simples · ${d.multi_blocker_no_bets} multiplos</div><div class="scroll"><table><thead><tr><th>Reason code</th><th class="right">Ocorrencias</th></tr></thead><tbody>${reasonRows(d.no_bet_reason_code_counts)}</tbody></table></div></div><div class="panel"><h2>Curva de P&L</h2>${chart(s.pnl_curve)}</div></div>
<div class="section"><div class="panel"><h2>Assinaturas NO_BET</h2><div class="scroll"><table><thead><tr><th>Combinacao de filtros</th><th class="right">Qtd.</th></tr></thead><tbody>${reasonRows(d.no_bet_signatures)}</tbody></table></div></div><div class="panel"><h2>Desempenho por liga</h2><div class="scroll"><table><thead><tr><th>Liga</th><th class="right">Settled</th><th class="right">Hit rate</th><th class="right">P&L</th><th class="right">ROI</th></tr></thead><tbody>${leagueRows(s.league_breakdown)}</tbody></table></div></div></div>
<div class="panel records"><h2>Registros recentes</h2><div class="scroll"><table><thead><tr><th>Registrado</th><th>Liga</th><th>Jogo / blockers</th><th>Decisao</th><th>Sel.</th><th class="right">Odd</th><th class="right">Edge</th><th>Status</th><th class="right">P&L</th><th class="right">Contraf.</th></tr></thead><tbody>${recordRows(r.items)}</tbody></table></div></div>`}catch(e){document.getElementById('content').innerHTML=`<div class="error">Falha ao carregar dashboard: ${esc(e.message)}</div>`}}
document.getElementById('days').addEventListener('change',load);load();
</script>
</body>
</html>"""
