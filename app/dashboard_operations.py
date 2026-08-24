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

OPERATIONS_VERSION = "dashboard_operations_v1_3"
DEFAULT_WINDOW_DAYS = 90
MAX_WINDOW_DAYS = 3650
MAX_OUTLIERS = 50
SLOW_BATCH_SECONDS = 900
AUDIT_ABS_EDGE_PCT = 15.0
AUDIT_ABS_EV_PCT = 50.0
AUDIT_ODD = 4.0
BATCH_SOURCE_PREFIX = "future_batch_runner_"

router = APIRouter(tags=["Dashboard Operations"])
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


def _batch_started_at(snapshot_window: str | None) -> datetime | None:
    raw = str(snapshot_window or "")
    if not raw.startswith("batch_"):
        return None
    try:
        return datetime.strptime(raw, "batch_%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _audit_reasons(record: DecisionRecord) -> list[str]:
    reasons: list[str] = []
    if record.edge_percentage_points is not None and abs(
        _as_float(record.edge_percentage_points)
    ) >= AUDIT_ABS_EDGE_PCT:
        reasons.append("ABS_EDGE_GE_15PP")
    if record.expected_value_pct is not None and abs(
        _as_float(record.expected_value_pct)
    ) >= AUDIT_ABS_EV_PCT:
        reasons.append("ABS_EV_GE_50PCT")
    if record.selected_odd is not None and _as_float(record.selected_odd) >= AUDIT_ODD:
        reasons.append("ODD_GE_4")
    return reasons


def _operations_payload(days: int) -> dict[str, Any]:
    ensure_forward_test_schema()
    filters = _window_filters(days)

    with SessionLocal() as session:
        rows = session.scalars(
            select(DecisionRecord)
            .where(*filters)
            .order_by(DecisionRecord.recorded_at.asc(), DecisionRecord.id.asc())
        ).all()

    total_records = len(rows)
    fixture_counts: Counter[int] = Counter(int(row.sportmonks_fixture_id) for row in rows)
    duplicate_fixture_groups = [
        {
            "sportmonks_fixture_id": fixture_id,
            "records": count,
            "excess_records": count - 1,
        }
        for fixture_id, count in sorted(fixture_counts.items())
        if count > 1
    ]
    duplicate_excess = sum(item["excess_records"] for item in duplicate_fixture_groups)

    source_counts: Counter[str] = Counter(str(row.source or "unknown") for row in rows)
    batch_rows = [
        row for row in rows if str(row.source or "").startswith(BATCH_SOURCE_PREFIX)
    ]

    batch_groups: dict[str, list[DecisionRecord]] = defaultdict(list)
    for row in batch_rows:
        batch_groups[str(row.snapshot_window or "unknown")].append(row)

    batch_runs: list[dict[str, Any]] = []
    run_durations: list[float] = []
    record_latencies: list[float] = []
    slow_runs = 0

    for snapshot_window, group in batch_groups.items():
        started_at = _batch_started_at(snapshot_window)
        first_recorded = min(
            (row.recorded_at for row in group if row.recorded_at is not None),
            default=None,
        )
        last_recorded = max(
            (row.recorded_at for row in group if row.recorded_at is not None),
            default=None,
        )
        duration_seconds: float | None = None
        if started_at is not None and last_recorded is not None:
            duration_seconds = max(0.0, (last_recorded - started_at).total_seconds())
            run_durations.append(duration_seconds)
            if duration_seconds >= SLOW_BATCH_SECONDS:
                slow_runs += 1

        if started_at is not None:
            for row in group:
                if row.recorded_at is not None:
                    record_latencies.append(
                        max(0.0, (row.recorded_at - started_at).total_seconds())
                    )

        batch_runs.append(
            {
                "snapshot_window": snapshot_window,
                "source_versions": sorted({str(row.source or "unknown") for row in group}),
                "started_at": started_at.isoformat() if started_at else None,
                "first_recorded_at": first_recorded.isoformat() if first_recorded else None,
                "last_recorded_at": last_recorded.isoformat() if last_recorded else None,
                "estimated_duration_seconds": (
                    round(duration_seconds, 3) if duration_seconds is not None else None
                ),
                "records_persisted": len(group),
                "unique_fixtures": len({int(row.sportmonks_fixture_id) for row in group}),
                "bets": sum(1 for row in group if row.decision == "BET"),
                "no_bets": sum(1 for row in group if row.decision == "NO_BET"),
            }
        )

    batch_runs.sort(
        key=lambda item: item.get("started_at") or item["snapshot_window"], reverse=True
    )

    outliers: list[dict[str, Any]] = []
    for row in rows:
        audit_reasons = _audit_reasons(row)
        if not audit_reasons:
            continue
        outliers.append(
            {
                "record_id": int(row.id),
                "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
                "sportmonks_fixture_id": int(row.sportmonks_fixture_id),
                "league": row.league,
                "home_team": row.home_team,
                "away_team": row.away_team,
                "decision": row.decision,
                "selection": row.selection,
                "bookmaker": row.bookmaker,
                "selected_odd": (
                    _as_float(row.selected_odd) if row.selected_odd is not None else None
                ),
                "edge_pct": (
                    _as_float(row.edge_percentage_points)
                    if row.edge_percentage_points is not None
                    else None
                ),
                "expected_value_pct": (
                    _as_float(row.expected_value_pct)
                    if row.expected_value_pct is not None
                    else None
                ),
                "calibrated_confidence": (
                    _as_float(row.calibrated_favorite_confidence)
                    if row.calibrated_favorite_confidence is not None
                    else None
                ),
                "audit_reasons": audit_reasons,
            }
        )

    outliers.sort(
        key=lambda item: max(
            abs(float(item.get("expected_value_pct") or 0.0)),
            abs(float(item.get("edge_pct") or 0.0)),
        ),
        reverse=True,
    )
    outliers = outliers[:MAX_OUTLIERS]

    health_reasons: list[str] = []
    if duplicate_fixture_groups:
        health_reasons.append("DUPLICATE_FIXTURE_RECORDS_PRESENT")
    if slow_runs:
        health_reasons.append("SLOW_BATCH_ESTIMATES_PRESENT")
    if outliers:
        health_reasons.append("MARKET_OUTLIERS_REQUIRE_AUDIT")

    if duplicate_fixture_groups:
        health_status = "ATTENTION"
    elif slow_runs or outliers:
        health_status = "REVIEW"
    else:
        health_status = "OK"

    avg_run_duration = (
        sum(run_durations) / len(run_durations) if run_durations else 0.0
    )
    avg_record_latency = (
        sum(record_latencies) / len(record_latencies) if record_latencies else 0.0
    )

    return {
        "status": "ok",
        "version": OPERATIONS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "execution_mode": "RESEARCH_ONLY",
        "operations_health": {
            "status": health_status,
            "reason_codes": health_reasons,
            "total_records": total_records,
            "unique_fixtures": len(fixture_counts),
            "duplicate_fixture_groups": len(duplicate_fixture_groups),
            "duplicate_excess_records": duplicate_excess,
            "batch_records": len(batch_rows),
            "manual_or_other_records": total_records - len(batch_rows),
            "outlier_records": len(outliers),
        },
        "latency": {
            "estimated_batch_runs": len(batch_runs),
            "runs_with_duration_estimate": len(run_durations),
            "average_estimated_run_seconds": round(avg_run_duration, 3),
            "max_estimated_run_seconds": round(max(run_durations), 3)
            if run_durations
            else 0,
            "slow_run_threshold_seconds": SLOW_BATCH_SECONDS,
            "slow_runs": slow_runs,
            "average_record_latency_seconds": round(avg_record_latency, 3),
            "method": (
                "estimated from batch snapshot_window timestamp to persisted ledger "
                "record timestamps"
            ),
            "limitation": (
                "inference-not-ready fixtures do not create ledger rows, so true batch "
                "duration can be longer than this estimate"
            ),
        },
        "batch_runs": batch_runs[:25],
        "duplicate_fixtures": duplicate_fixture_groups[:25],
        "market_outliers": outliers,
        "source_breakdown": [
            {"source": source, "records": count, "share_pct": _percentage(count, total_records)}
            for source, count in sorted(
                source_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "audit_policy": {
            "read_only": True,
            "outlier_flags_do_not_change_decisions": True,
            "outlier_thresholds_are_operational_audit_thresholds": True,
            "audit_abs_edge_pct": AUDIT_ABS_EDGE_PCT,
            "audit_abs_ev_pct": AUDIT_ABS_EV_PCT,
            "audit_odd": AUDIT_ODD,
            "duplicate_fixture_policy": "one fixture per automated forward-test sample by default",
            "real_money_execution_enabled": False,
        },
    }


@router.get("/dashboard/operations", response_class=HTMLResponse, include_in_schema=False)
def dashboard_operations_page() -> HTMLResponse:
    return HTMLResponse(OPERATIONS_HTML)


@router.get("/dashboard/api/operations")
def dashboard_operations_api(
    days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=0, le=MAX_WINDOW_DAYS),
) -> dict[str, Any]:
    return _operations_payload(days)


def install_dashboard_operations_routes() -> None:
    global _routes_installed
    if _routes_installed:
        return
    forward_test_router.include_router(router)
    _routes_installed = True


OPERATIONS_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Enigma Core — Operations & Audit</title>
<style>
:root{--bg:#081018;--panel:#101b26;--text:#e9f0f6;--muted:#8fa2b5;--line:#223446;--good:#54d39a;--warn:#f0c36a;--bad:#ff7b7b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}.wrap{max-width:1440px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:20px}h1{margin:0}.muted{color:var(--muted)}.controls{display:flex;gap:10px}.badge,.select{border:1px solid var(--line);background:#0f1b26;color:var(--text);border-radius:10px;padding:9px 12px}.grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}.label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}.value{font-size:27px;font-weight:750;margin-top:8px}.section{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}.panel h2{font-size:15px;margin:0 0 12px}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px 7px;border-bottom:1px solid rgba(34,52,70,.7);text-align:left;vertical-align:top}th{color:var(--muted)}.right{text-align:right}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.pill{display:inline-flex;padding:3px 7px;border:1px solid #36506a;border-radius:7px;font-size:11px}.loading,.empty{padding:32px;text-align:center;color:var(--muted)}@media(max-width:1000px){.grid{grid-template-columns:repeat(2,1fr)}.section{grid-template-columns:1fr}}@media(max-width:600px){.wrap{padding:16px}.top{align-items:flex-start;flex-direction:column}.grid{grid-template-columns:1fr 1fr}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>Enigma Core · Operations & Audit</h1><div class="muted">Forward Test · observabilidade read-only</div></div><div class="controls"><span class="badge">RESEARCH ONLY</span><select id="days" class="select"><option value="7">7 dias</option><option value="30">30 dias</option><option value="90" selected>90 dias</option><option value="365">1 ano</option><option value="0">Todo periodo</option></select></div></div>
<div id="content"><div class="loading">Carregando...</div></div></div>
<script>
const fmt=(v,d=1)=>Number(v??0).toLocaleString('pt-BR',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function batchRows(rows){if(!rows?.length)return '<tr><td colspan="6" class="empty">Sem batches reconstruidos.</td></tr>';return rows.map(r=>`<tr><td>${esc(r.snapshot_window)}</td><td class="right">${r.records_persisted}</td><td class="right">${r.bets}</td><td class="right">${r.no_bets}</td><td class="right">${r.estimated_duration_seconds==null?'—':fmt(r.estimated_duration_seconds/60,1)+' min'}</td><td>${esc((r.last_recorded_at||'').slice(0,19).replace('T',' '))}</td></tr>`).join('')}
function outlierRows(rows){if(!rows?.length)return '<tr><td colspan="7" class="empty">Nenhum outlier pelos thresholds de auditoria.</td></tr>';return rows.map(r=>`<tr><td>${esc(r.home_team)} x ${esc(r.away_team)}</td><td><strong>${esc(r.decision)}</strong> ${esc(r.selection||'')}</td><td class="right">${r.selected_odd==null?'—':fmt(r.selected_odd,2)}</td><td class="right">${r.edge_pct==null?'—':fmt(r.edge_pct,1)+'pp'}</td><td class="right">${r.expected_value_pct==null?'—':fmt(r.expected_value_pct,1)+'%'}</td><td class="right">${r.calibrated_confidence==null?'—':fmt(r.calibrated_confidence*100,1)+'%'}</td><td>${(r.audit_reasons||[]).map(x=>`<span class="pill">${esc(x)}</span>`).join(' ')}</td></tr>`).join('')}
async function load(){const d=document.getElementById('days').value;const root=document.getElementById('content');root.innerHTML='<div class="loading">Atualizando...</div>';try{const res=await fetch(`/dashboard/api/operations?days=${d}`);if(!res.ok)throw new Error(`HTTP ${res.status}`);const x=await res.json(),h=x.operations_health,l=x.latency;const statusCls=h.status==='OK'?'good':h.status==='REVIEW'?'warn':'bad';root.innerHTML=`<div class="grid"><div class="card"><div class="label">Status</div><div class="value ${statusCls}">${esc(h.status)}</div><div class="muted">${esc((h.reason_codes||[]).join(' · ')||'sem alertas')}</div></div><div class="card"><div class="label">Fixtures unicos</div><div class="value">${h.unique_fixtures}</div><div class="muted">${h.total_records} registros</div></div><div class="card"><div class="label">Duplicidades</div><div class="value ${h.duplicate_fixture_groups?'bad':''}">${h.duplicate_fixture_groups}</div><div class="muted">${h.duplicate_excess_records} registros excedentes</div></div><div class="card"><div class="label">Batch latency max</div><div class="value ${l.slow_runs?'warn':''}">${fmt(l.max_estimated_run_seconds/60,1)}m</div><div class="muted">${l.slow_runs} acima de ${fmt(l.slow_run_threshold_seconds/60,0)}m</div></div><div class="card"><div class="label">Outliers</div><div class="value ${h.outlier_records?'warn':''}">${h.outlier_records}</div><div class="muted">auditoria; nao altera decisao</div></div></div><div class="section"><div class="panel"><h2>Batches reconstruidos</h2><div class="scroll"><table><thead><tr><th>Snapshot</th><th class="right">Records</th><th class="right">BET</th><th class="right">NO_BET</th><th class="right">Duracao est.</th><th>Ultimo registro</th></tr></thead><tbody>${batchRows(x.batch_runs)}</tbody></table></div><div class="muted" style="margin-top:10px">${esc(l.limitation)}</div></div><div class="panel"><h2>Source breakdown</h2><div class="scroll"><table><thead><tr><th>Source</th><th class="right">Records</th><th class="right">Share</th></tr></thead><tbody>${(x.source_breakdown||[]).map(s=>`<tr><td>${esc(s.source)}</td><td class="right">${s.records}</td><td class="right">${fmt(s.share_pct,1)}%</td></tr>`).join('')}</tbody></table></div></div></div><div class="panel" style="margin-top:16px"><h2>Market outliers · audit only</h2><div class="scroll"><table><thead><tr><th>Jogo</th><th>Decisao</th><th class="right">Odd</th><th class="right">Edge</th><th class="right">EV</th><th class="right">Conf.</th><th>Flags</th></tr></thead><tbody>${outlierRows(x.market_outliers)}</tbody></table></div></div>`}catch(e){root.innerHTML=`<div class="card bad">Falha: ${esc(e.message)}</div>`}}
document.getElementById('days').addEventListener('change',load);load();
</script></body></html>"""
