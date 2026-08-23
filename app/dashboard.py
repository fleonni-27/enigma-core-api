from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, case, func, select

from app.database import SessionLocal
from app.forward_test_ledger import (
    DecisionRecord,
    ensure_forward_test_schema,
    router as forward_test_router,
)

DASHBOARD_VERSION = "dashboard_v1_1"
DEFAULT_WINDOW_DAYS = 90
MAX_WINDOW_DAYS = 3650
MAX_RECENT_RECORDS = 100

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


def _count(session, filters: list[Any], *extra: Any) -> int:
    return int(
        session.scalar(select(func.count(DecisionRecord.id)).where(*filters, *extra))
        or 0
    )


def _sum(session, column: Any, filters: list[Any], *extra: Any) -> float:
    return _as_float(
        session.scalar(
            select(func.coalesce(func.sum(column), Decimal("0"))).where(
                *filters, *extra
            )
        )
    )


def _avg(session, column: Any, filters: list[Any], *extra: Any) -> float:
    return _as_float(
        session.scalar(select(func.avg(column)).where(*filters, *extra))
    )


def _summary_payload(days: int) -> dict[str, Any]:
    ensure_forward_test_schema()
    filters = _window_filters(days)

    with SessionLocal() as session:
        total_records = _count(session, filters)
        bet_records = _count(session, filters, DecisionRecord.decision == "BET")
        no_bet_records = _count(session, filters, DecisionRecord.decision == "NO_BET")
        pending_records = _count(
            session, filters, DecisionRecord.settlement_status == "UNSETTLED"
        )

        settled_bets = _count(
            session,
            filters,
            DecisionRecord.decision == "BET",
            DecisionRecord.settlement_status == "SETTLED",
        )
        wins = _count(
            session,
            filters,
            DecisionRecord.decision == "BET",
            DecisionRecord.settlement_status == "SETTLED",
            DecisionRecord.selection_won == "true",
        )
        losses = _count(
            session,
            filters,
            DecisionRecord.decision == "BET",
            DecisionRecord.settlement_status == "SETTLED",
            DecisionRecord.selection_won == "false",
        )
        pnl_units = _sum(
            session,
            DecisionRecord.hypothetical_pnl_units,
            filters,
            DecisionRecord.decision == "BET",
            DecisionRecord.settlement_status == "SETTLED",
        )
        avg_odd = _avg(
            session,
            DecisionRecord.selected_odd,
            filters,
            DecisionRecord.decision == "BET",
        )
        avg_edge_pct = _avg(
            session,
            DecisionRecord.edge_percentage_points,
            filters,
            DecisionRecord.decision == "BET",
        )
        avg_ev_pct = _avg(
            session,
            DecisionRecord.expected_value_pct,
            filters,
            DecisionRecord.decision == "BET",
        )

        settled_no_bets = _count(
            session,
            filters,
            DecisionRecord.decision == "NO_BET",
            DecisionRecord.settlement_status == "SETTLED",
        )
        avoided_losses = _count(
            session,
            filters,
            DecisionRecord.decision == "NO_BET",
            DecisionRecord.settlement_status == "SETTLED",
            DecisionRecord.counterfactual_pnl_units < 0,
        )
        missed_wins = _count(
            session,
            filters,
            DecisionRecord.decision == "NO_BET",
            DecisionRecord.settlement_status == "SETTLED",
            DecisionRecord.counterfactual_pnl_units > 0,
        )
        neutral_no_bets = _count(
            session,
            filters,
            DecisionRecord.decision == "NO_BET",
            DecisionRecord.settlement_status == "SETTLED",
            DecisionRecord.counterfactual_pnl_units == 0,
        )
        no_bet_counterfactual_pnl = _sum(
            session,
            DecisionRecord.counterfactual_pnl_units,
            filters,
            DecisionRecord.decision == "NO_BET",
            DecisionRecord.settlement_status == "SETTLED",
        )
        policy_pnl = _sum(
            session,
            DecisionRecord.hypothetical_pnl_units,
            filters,
            DecisionRecord.settlement_status == "SETTLED",
        )
        all_counterfactual_pnl = _sum(
            session,
            DecisionRecord.counterfactual_pnl_units,
            filters,
            DecisionRecord.settlement_status == "SETTLED",
        )
        policy_advantage = policy_pnl - all_counterfactual_pnl

        league_rows = session.execute(
            select(
                DecisionRecord.league,
                func.count(DecisionRecord.id).label("bets"),
                func.sum(
                    case(
                        (DecisionRecord.settlement_status == "SETTLED", 1),
                        else_=0,
                    )
                ).label("settled_bets"),
                func.sum(
                    case(
                        (
                            and_(
                                DecisionRecord.settlement_status == "SETTLED",
                                DecisionRecord.selection_won == "true",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("wins"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                DecisionRecord.settlement_status == "SETTLED",
                                DecisionRecord.hypothetical_pnl_units,
                            ),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ).label("pnl_units"),
            )
            .where(*filters, DecisionRecord.decision == "BET")
            .group_by(DecisionRecord.league)
            .order_by(func.count(DecisionRecord.id).desc())
        ).all()

        curve_rows = session.execute(
            select(
                DecisionRecord.settled_at,
                DecisionRecord.hypothetical_pnl_units,
                DecisionRecord.home_team,
                DecisionRecord.away_team,
            )
            .where(
                *filters,
                DecisionRecord.decision == "BET",
                DecisionRecord.settlement_status == "SETTLED",
                DecisionRecord.settled_at.is_not(None),
            )
            .order_by(DecisionRecord.settled_at.desc())
            .limit(250)
        ).all()

    leagues: list[dict[str, Any]] = []
    for league, bets, settled, league_wins, league_pnl in league_rows:
        settled_int = int(settled or 0)
        pnl_float = _as_float(league_pnl)
        leagues.append(
            {
                "league": league or "Unknown",
                "bets": int(bets or 0),
                "settled_bets": settled_int,
                "wins": int(league_wins or 0),
                "hit_rate_pct": _percentage(int(league_wins or 0), settled_int),
                "pnl_units": round(pnl_float, 4),
                "roi_pct": _percentage(pnl_float, settled_int),
            }
        )

    cumulative = 0.0
    curve: list[dict[str, Any]] = []
    for settled_at, pnl, home_team, away_team in reversed(curve_rows):
        cumulative += _as_float(pnl)
        curve.append(
            {
                "settled_at": settled_at.isoformat() if settled_at else None,
                "match": f"{home_team} x {away_team}",
                "pnl_units": round(_as_float(pnl), 4),
                "cumulative_pnl_units": round(cumulative, 4),
            }
        )

    decision_quality = {
        "settled_no_bets": settled_no_bets,
        "avoided_losses": avoided_losses,
        "missed_wins": missed_wins,
        "neutral_no_bets": neutral_no_bets,
        "avoided_loss_rate_pct": _percentage(avoided_losses, settled_no_bets),
        "missed_win_rate_pct": _percentage(missed_wins, settled_no_bets),
        "no_bet_counterfactual_pnl_units": round(no_bet_counterfactual_pnl, 4),
        "policy_pnl_units": round(policy_pnl, 4),
        "all_selections_counterfactual_pnl_units": round(all_counterfactual_pnl, 4),
        "policy_advantage_units": round(policy_advantage, 4),
    }

    return {
        "status": "ok",
        "version": DASHBOARD_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "execution_mode": "RESEARCH_ONLY",
        "kpis": {
            "total_records": total_records,
            "bet_records": bet_records,
            "no_bet_records": no_bet_records,
            "settled_bets": settled_bets,
            "pending_records": pending_records,
            "wins": wins,
            "losses": losses,
            "hit_rate_pct": _percentage(wins, settled_bets),
            "pnl_units": round(pnl_units, 4),
            "roi_pct": _percentage(pnl_units, settled_bets),
            "avg_odd": round(avg_odd, 4),
            "avg_edge_pct": round(avg_edge_pct, 3),
            "avg_ev_pct": round(avg_ev_pct, 3),
        },
        "decision_quality": decision_quality,
        "league_breakdown": leagues,
        "pnl_curve": curve,
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
                "recorded_at": (
                    record.recorded_at.isoformat() if record.recorded_at else None
                ),
                "fixture_starts_at": (
                    record.fixture_starts_at.isoformat()
                    if record.fixture_starts_at
                    else None
                ),
                "sportmonks_fixture_id": int(record.sportmonks_fixture_id),
                "league": record.league,
                "home_team": record.home_team,
                "away_team": record.away_team,
                "decision": record.decision,
                "selection": record.selection,
                "bookmaker": record.bookmaker,
                "selected_odd": (
                    _as_float(record.selected_odd)
                    if record.selected_odd is not None
                    else None
                ),
                "edge_pct": (
                    _as_float(record.edge_percentage_points)
                    if record.edge_percentage_points is not None
                    else None
                ),
                "expected_value_pct": (
                    _as_float(record.expected_value_pct)
                    if record.expected_value_pct is not None
                    else None
                ),
                "calibrated_confidence": (
                    _as_float(record.calibrated_favorite_confidence)
                    if record.calibrated_favorite_confidence is not None
                    else None
                ),
                "settlement_status": record.settlement_status,
                "actual_result": record.actual_result,
                "selection_won": selection_won,
                "pnl_units": (
                    _as_float(record.hypothetical_pnl_units)
                    if record.hypothetical_pnl_units is not None
                    else None
                ),
                "counterfactual_pnl_units": (
                    _as_float(record.counterfactual_pnl_units)
                    if record.counterfactual_pnl_units is not None
                    else None
                ),
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
:root{--bg:#081018;--panel:#101b26;--text:#e9f0f6;--muted:#8fa2b5;--line:#223446;--good:#54d39a;--bad:#ff7b7b;--accent:#79a8ff}
*{box-sizing:border-box}body{margin:0;background:#081018;color:var(--text);font-family:Inter,system-ui,sans-serif}.wrap{max-width:1440px;margin:auto;padding:28px}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:24px}.brand h1{margin:0;font-size:30px}.brand p{margin:7px 0 0;color:var(--muted)}
.controls{display:flex;gap:10px;align-items:center}.badge{border:1px solid #34506a;background:#0f1e2a;color:#9bc5ff;padding:8px 11px;border-radius:999px;font-size:12px;font-weight:700}.select{background:#0f1b26;color:var(--text);border:1px solid var(--line);padding:9px 12px;border-radius:10px}
.grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:16px}
.card,.panel{background:#101b26;border:1px solid var(--line);border-radius:14px;padding:16px}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.value{font-size:28px;font-weight:760;margin-top:10px}.sub{font-size:12px;color:var(--muted);margin-top:6px}
.section{margin-top:16px;display:grid;grid-template-columns:1.4fr 1fr;gap:16px}.panel h2{font-size:15px;margin:0 0 14px}.records{margin-top:16px}.scroll{overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}th{color:var(--muted);font-weight:600;text-align:left;padding:10px 8px;border-bottom:1px solid var(--line)}td{padding:11px 8px;border-bottom:1px solid rgba(34,52,70,.65)}.right{text-align:right}.good{color:var(--good)}.bad{color:var(--bad)}.muted{color:var(--muted)}.pill{display:inline-flex;padding:4px 7px;border-radius:7px;background:#162737;border:1px solid #294158;font-size:11px}
.chart{height:260px;width:100%}.line{fill:none;stroke:var(--accent);stroke-width:3;vector-effect:non-scaling-stroke}.zero{stroke:#506579;stroke-dasharray:5 5}.empty,.loading{padding:40px;color:var(--muted);text-align:center}.error{padding:18px;border:1px solid #683b3b;background:#261616;border-radius:12px;color:#ffb4b4}
@media(max-width:1050px){.grid{grid-template-columns:repeat(3,1fr)}.grid4{grid-template-columns:repeat(2,1fr)}.section{grid-template-columns:1fr}}@media(max-width:650px){.wrap{padding:18px}.top{align-items:flex-start;flex-direction:column}.grid,.grid4{grid-template-columns:repeat(2,1fr)}.value{font-size:23px}}
</style>
</head>
<body>
<div class="wrap">
<div class="top"><div class="brand"><h1>Enigma Core</h1><p>Decision Intelligence Dashboard · Forward Test</p></div><div class="controls"><span class="badge">RESEARCH ONLY</span><select id="days" class="select"><option value="7">7 dias</option><option value="30">30 dias</option><option value="90" selected>90 dias</option><option value="365">1 ano</option><option value="0">Todo período</option></select></div></div>
<div id="content"><div class="loading">Carregando métricas...</div></div>
</div>
<script>
const fmt=(v,d=2)=>Number(v??0).toLocaleString('pt-BR',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=(s)=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cls=(v)=>Number(v)>0?'good':Number(v)<0?'bad':'';
function chart(curve){if(!curve?.length)return '<div class="empty">Sem BETs liquidadas no período.</div>';const vals=curve.map(x=>Number(x.cumulative_pnl_units||0)),w=800,h=240,p=24,min=Math.min(0,...vals),max=Math.max(0,...vals),span=(max-min)||1;const pts=vals.map((v,i)=>{const x=p+(i*(w-2*p)/Math.max(1,vals.length-1)),y=p+((max-v)*(h-2*p)/span);return `${x.toFixed(1)},${y.toFixed(1)}`}).join(' '),zy=p+((max-0)*(h-2*p)/span);return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="zero" x1="${p}" x2="${w-p}" y1="${zy}" y2="${zy}"/><polyline class="line" points="${pts}"/></svg>`}
function leagueRows(rows){if(!rows?.length)return '<tr><td colspan="6" class="muted">Sem BETs no período.</td></tr>';return rows.map(r=>`<tr><td>${esc(r.league)}</td><td class="right">${r.bets}</td><td class="right">${r.settled_bets}</td><td class="right">${fmt(r.hit_rate_pct,1)}%</td><td class="right ${cls(r.pnl_units)}">${fmt(r.pnl_units)}u</td><td class="right ${cls(r.roi_pct)}">${fmt(r.roi_pct,1)}%</td></tr>`).join('')}
function recordRows(rows){if(!rows?.length)return '<tr><td colspan="11" class="muted">Nenhum registro encontrado.</td></tr>';return rows.map(r=>{const result=r.selection_won===true?'WIN':r.selection_won===false?'LOSS':r.settlement_status;return `<tr><td>${esc((r.recorded_at||'').slice(0,16).replace('T',' '))}</td><td>${esc(r.league||'—')}</td><td><strong>${esc(r.home_team)}</strong><span class="muted"> x </span><strong>${esc(r.away_team)}</strong></td><td>${esc(r.decision)}</td><td>${esc(r.selection||'—')}</td><td class="right">${r.selected_odd==null?'—':fmt(r.selected_odd)}</td><td class="right">${r.edge_pct==null?'—':fmt(r.edge_pct,1)+'pp'}</td><td class="right">${r.expected_value_pct==null?'—':fmt(r.expected_value_pct,1)+'%'}</td><td><span class="pill">${esc(result)}</span></td><td class="right ${cls(r.pnl_units)}">${r.pnl_units==null?'—':fmt(r.pnl_units)+'u'}</td><td class="right ${cls(r.counterfactual_pnl_units)}">${r.counterfactual_pnl_units==null?'—':fmt(r.counterfactual_pnl_units)+'u'}</td></tr>`}).join('')}
async function load(){const days=document.getElementById('days').value;document.getElementById('content').innerHTML='<div class="loading">Atualizando...</div>';try{const [sres,rres]=await Promise.all([fetch(`/dashboard/api/summary?days=${days}`),fetch(`/dashboard/api/records?days=${days}&limit=30`)]);if(!sres.ok||!rres.ok)throw new Error(`HTTP ${sres.status}/${rres.status}`);const s=await sres.json(),r=await rres.json(),k=s.kpis,q=s.decision_quality;document.getElementById('content').innerHTML=`
<div class="grid">
<div class="card"><div class="label">Registros</div><div class="value">${k.total_records}</div><div class="sub">BET + NO_BET</div></div>
<div class="card"><div class="label">BETs</div><div class="value">${k.bet_records}</div><div class="sub">${k.no_bet_records} NO_BET</div></div>
<div class="card"><div class="label">BETs liquidadas</div><div class="value">${k.settled_bets}</div><div class="sub">${k.pending_records} pendentes</div></div>
<div class="card"><div class="label">Assertividade</div><div class="value">${fmt(k.hit_rate_pct,1)}%</div><div class="sub">${k.wins}W · ${k.losses}L</div></div>
<div class="card"><div class="label">P&L BET</div><div class="value ${cls(k.pnl_units)}">${fmt(k.pnl_units)}u</div><div class="sub">1 unidade por BET</div></div>
<div class="card"><div class="label">ROI BET</div><div class="value ${cls(k.roi_pct)}">${fmt(k.roi_pct,1)}%</div><div class="sub">BETs liquidadas</div></div>
</div>
<div class="panel records"><h2>Decision Quality · NO_BET</h2><div class="grid4">
<div class="card"><div class="label">NO_BET liquidados</div><div class="value">${q.settled_no_bets}</div><div class="sub">${fmt(q.avoided_loss_rate_pct,1)}% evitaram perdas</div></div>
<div class="card"><div class="label">Perdas evitadas</div><div class="value good">${q.avoided_losses}</div><div class="sub">decisões rejeitadas que perderiam</div></div>
<div class="card"><div class="label">Ganhos perdidos</div><div class="value bad">${q.missed_wins}</div><div class="sub">NO_BET que teria vencido</div></div>
<div class="card"><div class="label">Vantagem da política</div><div class="value ${cls(q.policy_advantage_units)}">${fmt(q.policy_advantage_units)}u</div><div class="sub">vs apostar em toda seleção</div></div>
</div><div class="sub" style="margin-top:12px">Contrafactual NO_BET: <strong class="${cls(q.no_bet_counterfactual_pnl_units)}">${fmt(q.no_bet_counterfactual_pnl_units)}u</strong> · P&L da política: ${fmt(q.policy_pnl_units)}u · Contrafactual total: ${fmt(q.all_selections_counterfactual_pnl_units)}u</div></div>
<div class="grid" style="margin-top:12px;grid-template-columns:repeat(3,minmax(0,1fr))">
<div class="card"><div class="label">Odd média</div><div class="value">${fmt(k.avg_odd)}</div></div>
<div class="card"><div class="label">Edge médio</div><div class="value">${fmt(k.avg_edge_pct,1)}pp</div></div>
<div class="card"><div class="label">EV médio</div><div class="value">${fmt(k.avg_ev_pct,1)}%</div></div>
</div>
<div class="section"><div class="panel"><h2>Curva de P&L acumulado</h2>${chart(s.pnl_curve)}</div><div class="panel"><h2>Desempenho por liga</h2><div class="scroll"><table><thead><tr><th>Liga</th><th class="right">BETs</th><th class="right">Settled</th><th class="right">Hit rate</th><th class="right">P&L</th><th class="right">ROI</th></tr></thead><tbody>${leagueRows(s.league_breakdown)}</tbody></table></div></div></div>
<div class="panel records"><h2>Registros recentes</h2><div class="scroll"><table><thead><tr><th>Registrado</th><th>Liga</th><th>Jogo</th><th>Decisão</th><th>Sel.</th><th class="right">Odd</th><th class="right">Edge</th><th class="right">EV</th><th>Status</th><th class="right">P&L política</th><th class="right">P&L contraf.</th></tr></thead><tbody>${recordRows(r.items)}</tbody></table></div></div>`}catch(e){document.getElementById('content').innerHTML=`<div class="error">Falha ao carregar o dashboard: ${esc(e.message)}</div>`}}
document.getElementById('days').addEventListener('change',load);load();
</script>
</body>
</html>"""
