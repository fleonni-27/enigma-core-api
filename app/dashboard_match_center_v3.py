from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.dashboard_operations_v2 import BUSINESS_TIMEZONE, build_dashboard_operations_v2
from app.database import SessionLocal
from app.enigma_rating_v2_context import build_enigma_rating_v2_context
from app.fixture_results import fixture_results_by_sportmonks_ids
from app.forward_test_ledger import DecisionRecord
from app.models import Fixture, FixtureDataSnapshot, OddsSnapshot
from app.training_dataset import STAT_NAMES, _as_list, _stat_value

DASHBOARD_MATCH_CENTER_V3_VERSION = "dashboard_match_center_v3"
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


def _latest_snapshot(session, fixture_id: int) -> FixtureDataSnapshot | None:
    return session.scalar(
        select(FixtureDataSnapshot)
        .where(FixtureDataSnapshot.fixture_id == fixture_id)
        .order_by(FixtureDataSnapshot.fetched_at.desc(), FixtureDataSnapshot.id.desc())
        .limit(1)
    )


def _last_five_results(*, team_name: str, league_name: str | None, before: datetime) -> list[str]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(Fixture)
            .where(
                Fixture.starts_at < before,
                ((Fixture.home_team == team_name) | (Fixture.away_team == team_name)),
            )
            .order_by(Fixture.starts_at.desc(), Fixture.id.desc())
            .limit(20)
        ).all()
        result: list[str] = []
        for fixture in rows:
            if league_name and fixture.league_name != league_name:
                continue
            snapshot = _latest_snapshot(session, int(fixture.id))
            if snapshot is None:
                continue
            stats = _as_list(snapshot.statistics)
            home_goals = _stat_value(stats, STAT_NAMES["goals"], "home")
            away_goals = _stat_value(stats, STAT_NAMES["goals"], "away")
            if home_goals is None or away_goals is None:
                continue
            if fixture.home_team == team_name:
                gf, ga = float(home_goals), float(away_goals)
            else:
                gf, ga = float(away_goals), float(home_goals)
            result.append("V" if gf > ga else "E" if gf == ga else "D")
            if len(result) == 5:
                break
        return result


def _normalize_side(selection: str | None, *, home_team: str, away_team: str) -> str | None:
    value = str(selection or "").strip().lower()
    if value in {"1", "home", "mandante", home_team.lower()}:
        return "1"
    if value in {"x", "draw", "empate"}:
        return "X"
    if value in {"2", "away", "visitante", away_team.lower()}:
        return "2"
    return None


def _latest_1x2_odds(
    *,
    fixture_id: int,
    snapshot_window: str,
    home_team: str,
    away_team: str,
    bookmaker: str | None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(OddsSnapshot)
            .where(
                OddsSnapshot.fixture_id == fixture_id,
                OddsSnapshot.snapshot_window == snapshot_window,
            )
            .order_by(OddsSnapshot.fetched_at.desc(), OddsSnapshot.id.desc())
        ).all()
    result: dict[str, Any] = {"1": None, "X": None, "2": None, "bookmaker": bookmaker}
    ordered = sorted(rows, key=lambda row: 0 if bookmaker and row.bookmaker == bookmaker else 1)
    for row in ordered:
        side = _normalize_side(row.selection, home_team=home_team, away_team=away_team)
        if side and result[side] is None:
            result[side] = _f(row.odd)
            if result["bookmaker"] is None:
                result["bookmaker"] = row.bookmaker
        if all(result[side] is not None for side in ("1", "X", "2")):
            break
    return result


def _relative_strength(home_value: float | None, away_value: float | None, *, inverse: bool = False) -> tuple[float | None, float | None]:
    if home_value is None or away_value is None:
        return None, None
    if inverse:
        home_value = 1.0 / max(home_value, 0.01)
        away_value = 1.0 / max(away_value, 0.01)
    peak = max(home_value, away_value, 0.01)
    return round(home_value / peak * 100.0, 1), round(away_value / peak * 100.0, 1)


def _enrich_fixture(item: dict[str, Any]) -> dict[str, Any]:
    sid = int(item["sportmonks_fixture_id"])
    starts_at = datetime.fromisoformat(item["starts_at"])
    context = build_enigma_rating_v2_context(sid, form_lookback=10)
    rating_inputs = context.get("rating_inputs") or {} if context.get("status") == "ok" else {}
    history = context.get("history") or {}

    decision = item.get("decision") or {}
    probabilities = item.get("probabilities") or {}
    confidence = _f(decision.get("calibrated_confidence"))
    if confidence is None:
        values = [_f(probabilities.get(key)) for key in ("home", "draw", "away")]
        values = [value for value in values if value is not None]
        confidence = max(values) if values else None

    odds = _latest_1x2_odds(
        fixture_id=int(item["fixture_id"]),
        snapshot_window=str(item["snapshot_window"]),
        home_team=str(item["home_team"]),
        away_team=str(item["away_team"]),
        bookmaker=decision.get("bookmaker"),
    )

    home_xg = _f(rating_inputs.get("home_xg_for_avg"))
    away_xg = _f(rating_inputs.get("away_xg_for_avg"))
    home_xga = _f(rating_inputs.get("home_xg_against_avg"))
    away_xga = _f(rating_inputs.get("away_xg_against_avg"))
    home_attack, away_attack = _relative_strength(home_xg, away_xg)
    home_defense, away_defense = _relative_strength(home_xga, away_xga, inverse=True)

    with SessionLocal() as session:
        fixture = session.get(Fixture, int(item["fixture_id"]))
        league_name = fixture.league_name if fixture else None

    results = fixture_results_by_sportmonks_ids([sid])
    final_row = results.get(sid)
    final_score = None
    if final_row:
        final_score = f"{final_row.get('home_goals')} x {final_row.get('away_goals')}"

    reason_codes = list(decision.get("reason_codes") or [])
    return {
        **item,
        "confidence": confidence,
        "confidence_pct": _pct(confidence),
        "confidence_band": _confidence_band(confidence),
        "odds_1x2": odds,
        "decision_explanation": _decision_reason_labels(reason_codes),
        "team_metrics": {
            "home": {
                "xg": home_xg,
                "xga": home_xga,
                "goals_for_avg": _f(rating_inputs.get("home_goals_for_avg")),
                "goals_against_avg": _f(rating_inputs.get("home_goals_against_avg")),
                "attack_strength": home_attack,
                "defense_strength": home_defense,
                "form_5": _last_five_results(team_name=str(item["home_team"]), league_name=league_name, before=starts_at),
                "form_10_ppm": _f(rating_inputs.get("home_points_per_match_10")),
                "elo": _f(rating_inputs.get("home_elo")),
            },
            "away": {
                "xg": away_xg,
                "xga": away_xga,
                "goals_for_avg": _f(rating_inputs.get("away_goals_for_avg")),
                "goals_against_avg": _f(rating_inputs.get("away_goals_against_avg")),
                "attack_strength": away_attack,
                "defense_strength": away_defense,
                "form_5": _last_five_results(team_name=str(item["away_team"]), league_name=league_name, before=starts_at),
                "form_10_ppm": _f(rating_inputs.get("away_points_per_match_10")),
                "elo": _f(rating_inputs.get("away_elo")),
            },
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
            "reason": "editorial/injury news feed is not connected; confirmed lineup context remains available separately",
        },
        "data_quality": {
            "rating_context_status": context.get("status"),
            "rating_context_version": context.get("version"),
            "history": history,
            "lineup_context": context.get("lineup_context"),
        },
    }


def build_dashboard_match_center_v3(*, target_date: date | None = None) -> dict[str, Any]:
    base = build_dashboard_operations_v2(target_date=target_date)
    fixtures = [_enrich_fixture(item) for item in base.get("fixtures") or []]
    return {
        **base,
        "version": DASHBOARD_MATCH_CENTER_V3_VERSION,
        "fixtures": fixtures,
        "policy": {
            **(base.get("policy") or {}),
            "dashboard_self_feeds_from_database": True,
            "j1_pipeline_is_primary_live_prematch_source": True,
            "unsupported_sources_are_never_fabricated": True,
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


DASHBOARD_MATCH_CENTER_V3_HTML = r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enigma Core · Match Center V3</title>
<style>
:root{--bg:#05090d;--panel:#0d141c;--panel2:#101b26;--line:#263746;--text:#f6f8fb;--muted:#91a2b2;--green:#56d39a;--red:#ff666d;--blueStrong:#164d80;--blueEffective:#65b8ff;--white:#f6f8fb;--amber:#f0c36a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}.wrap{max-width:1540px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:16px}h1{margin:0;font-size:28px}.muted{color:var(--muted)}.grid{display:grid;gap:18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}.head{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:14px;padding:18px 22px;background:#102337}.team{font-size:21px;font-weight:800}.team.right{text-align:right}.meta{text-align:center;color:var(--muted);font-size:12px}.score{font-size:22px;color:var(--white);font-weight:850}.body{padding:18px 22px}.main{display:grid;grid-template-columns:1.15fr .85fr 1.15fr;gap:16px}.side,.center{background:var(--panel2);border:1px solid var(--line);border-radius:13px;padding:14px}.side.right{text-align:right}.metricrow{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:10px}.metric{border:1px solid var(--line);border-radius:9px;padding:9px}.label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}.value{font-size:18px;font-weight:800;margin-top:4px}.ring{--p:0;--c:var(--blueEffective);width:126px;height:126px;border-radius:50%;display:grid;place-items:center;margin:0 auto;background:conic-gradient(var(--c) calc(var(--p)*1%),#1b2834 0)}.ring:after{content:"";width:94px;height:94px;background:var(--panel2);border-radius:50%;position:absolute}.ringwrap{position:relative}.ringtxt{position:absolute;inset:0;display:grid;place-items:center;font-size:25px;font-weight:900;z-index:2}.prob{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:12px}.prob div{border:1px solid var(--line);border-radius:9px;text-align:center;padding:8px}.prob b{display:block;font-size:18px}.decision{margin-top:12px;border:1px solid var(--line);border-radius:10px;padding:11px}.decision.bet{border-color:#275f49}.decision.nobet{border-color:#74343b}.good{color:var(--green)}.bad{color:var(--red)}.info{color:var(--white)}.strong{color:#5fa9e8}.effective{color:var(--blueEffective)}.form{display:flex;gap:5px;margin-top:8px}.side.right .form{justify-content:flex-end}.pill{width:27px;height:27px;border-radius:6px;display:grid;place-items:center;font-weight:850}.pill.V{background:#194b38;color:var(--green)}.pill.E{background:#4a401f;color:var(--amber)}.pill.D{background:#5b242a;color:var(--red)}.bars{margin-top:10px}.barline{display:grid;grid-template-columns:88px 1fr 42px;gap:8px;align-items:center;margin:7px 0}.bar{height:8px;background:#1b2834;border-radius:99px;overflow:hidden}.fill{height:100%;background:var(--green)}.market{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:14px}.box{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px}.reason{font-size:12px;margin-top:5px}.extras{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:12px}.warning{color:var(--amber)}@media(max-width:950px){.main{grid-template-columns:1fr}.market{grid-template-columns:repeat(2,1fr)}.extras{grid-template-columns:1fr}.head{grid-template-columns:1fr}.team.right,.meta{text-align:left}.side.right{text-align:left}.side.right .form{justify-content:flex-start}}
</style></head><body><div class="wrap"><div class="top"><div><h1>Enigma Core · Match Center V3</h1><div class="muted">J1 automático · análise pré-jogo · RESEARCH ONLY</div></div><div id="updated" class="muted">carregando…</div></div><div id="app" class="grid"></div></div>
<script>
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const pct=v=>v==null?'—':(Number(v)*100).toFixed(1)+'%';const num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
function form(xs){return (xs||[]).map(x=>`<span class="pill ${x}">${x}</span>`).join('')||'<span class="muted">sem amostra</span>'}
function strength(label,v){return `<div class="barline"><span class="muted">${label}</span><div class="bar"><div class="fill" style="width:${Math.max(0,Math.min(100,v||0))}%"></div></div><b>${v==null?'—':v.toFixed(0)}</b></div>`}
function side(t,m,right=false){return `<div class="side ${right?'right':''}"><div class="team">${esc(t)}</div><div class="form">${form(m.form_5)}</div><div class="metricrow"><div class="metric"><div class="label">xG produção</div><div class="value">${num(m.xg)}</div></div><div class="metric"><div class="label">xGA cedido</div><div class="value">${num(m.xga)}</div></div><div class="metric"><div class="label">Gols pró/jogo</div><div class="value">${num(m.goals_for_avg)}</div></div><div class="metric"><div class="label">Gols contra/jogo</div><div class="value">${num(m.goals_against_avg)}</div></div></div><div class="bars">${strength('Ataque',m.attack_strength)}${strength('Defesa',m.defense_strength)}</div><div class="muted" style="margin-top:8px">Elo ${num(m.elo,0)} · PPM10 ${num(m.form_10_ppm)}</div></div>`}
function card(f){const p=f.probabilities||{},d=f.decision||{},m=f.team_metrics||{}, band=f.confidence_band;const ringColor=band==='STRONG_FAVORITE'?'var(--blueStrong)':band==='EFFECTIVE_FAVORITE'?'var(--blueEffective)':'#667789';const action=d.decision||'AGUARDANDO';const cls=action==='BET'?'bet good':action==='NO_BET'?'nobet bad':'info';const reasons=(f.decision_explanation||[]).map(x=>`<div class="reason">• ${esc(x)}</div>`).join('')||'<div class="reason muted">sem decisão registrada ainda</div>';const o=f.odds_1x2||{};return `<article class="card"><div class="head"><div><div class="muted">${esc(f.league)}</div><div class="team">${esc(f.home_team)}</div></div><div class="meta"><div>${new Date(f.starts_at).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',hour:'2-digit',minute:'2-digit'})}</div><div class="score">${esc(f.final_score||'x')}</div><div>J1 ${new Date(f.j1_due_at).toLocaleTimeString('pt-BR',{timeZone:'America/Sao_Paulo',hour:'2-digit',minute:'2-digit'})}</div></div><div><div class="muted" style="text-align:right">ID ${f.sportmonks_fixture_id}</div><div class="team right">${esc(f.away_team)}</div></div></div><div class="body"><div class="main">${side(f.home_team,m.home||{})}<div class="center"><div class="label" style="text-align:center">Confiança Enigma Core</div><div class="ringwrap"><div class="ring" style="--p:${f.confidence_pct||0};--c:${ringColor}"></div><div class="ringtxt">${f.confidence_pct==null?'—':f.confidence_pct.toFixed(1)+'%'}</div></div><div class="muted" style="text-align:center;margin-top:5px">${esc(band.replaceAll('_',' '))}</div><div class="prob"><div>1<b>${pct(p.home)}</b></div><div>X<b>${pct(p.draw)}</b></div><div>2<b>${pct(p.away)}</b></div></div><div class="decision ${cls}"><div class="label">Decisão Enigma Core</div><div class="value">${esc(action)} ${d.selection?'· '+esc(d.selection):''}</div>${reasons}</div></div>${side(f.away_team,m.away||{},true)}</div><div class="market"><div class="box"><div class="label">Odd 1</div><div class="value">${num(o['1'])}</div></div><div class="box"><div class="label">Odd X</div><div class="value">${num(o['X'])}</div></div><div class="box"><div class="label">Odd 2</div><div class="value">${num(o['2'])}</div></div><div class="box"><div class="label">Edge</div><div class="value ${Number(d.edge_pct||0)>=0?'good':'bad'}">${d.edge_pct==null?'—':num(d.edge_pct)+' pp'}</div></div><div class="box"><div class="label">EV</div><div class="value ${Number(d.expected_value_pct||0)>=0?'good':'bad'}">${d.expected_value_pct==null?'—':num(d.expected_value_pct)+'%'}</div></div></div><div class="extras"><div class="box"><div class="label">Tabela / competição</div><div class="warning">${esc(f.competition_context?.status||'—')}</div><div class="muted reason">${esc(f.competition_context?.reason||'')}</div></div><div class="box"><div class="label">Jogo de ida</div><div class="value">${esc(f.competition_context?.first_leg_score||'—')}</div><div class="muted reason">exibido somente quando houver metadado formal de confronto</div></div><div class="box"><div class="label">Notícias / alertas</div><div class="warning">${esc(f.news?.status||'—')}</div><div class="muted reason">${esc(f.news?.reason||'')}</div></div></div></div></article>`}
async function load(){try{const r=await fetch('/dashboard/api/match-center-v3',{cache:'no-store'});const x=await r.json();document.getElementById('updated').textContent='Atualizado '+new Date(x.generated_at).toLocaleTimeString('pt-BR',{timeZone:'America/Sao_Paulo',hour:'2-digit',minute:'2-digit'});document.getElementById('app').innerHTML=(x.fixtures||[]).map(card).join('')||'<div class="muted">Nenhum jogo-alvo hoje.</div>'}catch(e){document.getElementById('app').innerHTML='<div class="bad">Falha ao carregar dashboard.</div>'}}load();setInterval(load,60000);
</script></body></html>'''
