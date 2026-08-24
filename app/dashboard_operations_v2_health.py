from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app import dashboard_operations_v2 as dashboard_module
from app.j1_scheduler import J1_HEARTBEAT_STALE_MINUTES, latest_j1_run

DASHBOARD_OPERATIONS_V2_HEALTH_VERSION = "dashboard_operations_v2_3"
_installed = False
_original_builder: Callable[..., dict[str, Any]] | None = None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _scheduler_payload(now: datetime) -> dict[str, Any]:
    run = latest_j1_run()
    if run is None:
        return {
            "status": "NO_HEARTBEAT",
            "source": None,
            "run_id": None,
            "started_at": None,
            "finished_at": None,
            "age_minutes": None,
            "selected_fixtures": 0,
            "counts": {},
            "error": None,
            "expected_cadence_minutes": 1,
            "stale_after_minutes": J1_HEARTBEAT_STALE_MINUTES,
        }

    started_at = _aware_utc(run.started_at)
    age_minutes = round(max(0.0, (now - started_at).total_seconds() / 60.0), 2)
    if run.status == "FAILED":
        health = "FAILED"
    elif age_minutes > J1_HEARTBEAT_STALE_MINUTES:
        health = "STALE"
    elif run.status == "DEGRADED":
        health = "DEGRADED"
    elif run.status == "REVIEW":
        health = "REVIEW"
    elif run.status == "SKIPPED_LOCKED":
        health = "HEALTHY_LOCKED"
    else:
        health = "HEALTHY"

    return {
        "status": health,
        "last_run_status": run.status,
        "source": run.source,
        "run_id": int(run.id),
        "started_at": started_at.isoformat(),
        "finished_at": _aware_utc(run.finished_at).isoformat() if run.finished_at else None,
        "age_minutes": age_minutes,
        "selected_fixtures": int(run.selected_fixtures or 0),
        "counts": dict(run.counts or {}),
        "error": run.error,
        "expected_cadence_minutes": 1,
        "stale_after_minutes": J1_HEARTBEAT_STALE_MINUTES,
    }


def _build_with_scheduler_health(*, target_date=None) -> dict[str, Any]:
    if _original_builder is None:
        raise RuntimeError("Dashboard Operations V2 health wrapper is not installed")
    payload = _original_builder(target_date=target_date)
    now = datetime.now(timezone.utc)
    scheduler = _scheduler_payload(now)
    payload["version"] = DASHBOARD_OPERATIONS_V2_HEALTH_VERSION
    payload["scheduler"] = scheduler
    payload.setdefault("overview", {})["scheduler_health"] = scheduler["status"]
    payload["overview"]["scheduler_source"] = scheduler.get("source")
    payload["overview"]["scheduler_last_run_at"] = scheduler.get("started_at")
    payload.setdefault("policy", {})["dashboard_refresh_does_not_trigger_j1"] = True
    payload["policy"]["j1_scheduler_heartbeat_persisted"] = True
    payload["policy"]["degraded_runner_state_is_visible"] = True
    payload["policy"]["j1_auto_publish_after_window_opens"] = True
    payload["policy"]["primary_scheduler_cadence_minutes"] = 1
    payload["policy"]["dashboard_auto_refresh_seconds"] = 60
    return payload


def _patch_html(html: str) -> str:
    html = html.replace(
        "grid-template-columns:repeat(5,minmax(0,1fr))",
        "grid-template-columns:repeat(6,minmax(0,1fr))",
    )
    html = html.replace("J1 DISPONÍVEL", "J1 AGUARDANDO PROCESSAMENTO")
    old = "<div class=\"metric\"><div class=\"label\">Próxima J1</div><div style=\"margin-top:7px;font-weight:700\">${next}</div></div></div><div class=\"matches\">"
    new = (
        "<div class=\"metric\"><div class=\"label\">Próxima J1</div><div style=\"margin-top:7px;font-weight:700\">${next}</div></div>"
        "<div class=\"metric\"><div class=\"label\">Runner J1</div>"
        "<div class=\"value ${o.scheduler_health==='HEALTHY'||o.scheduler_health==='HEALTHY_LOCKED'?'good':o.scheduler_health==='NO_HEARTBEAT'?'warn':'bad'}\" style=\"font-size:17px\">${esc(o.scheduler_health||'—')}</div>"
        "<div class=\"muted\" style=\"font-size:11px;margin-top:4px\">${x.scheduler?.started_at?'última '+localTime(x.scheduler.started_at):'sem heartbeat'} · ${esc(x.scheduler?.source||'')}</div></div></div><div class=\"matches\">"
    )
    html = html.replace(old, new)
    return html


def install_dashboard_operations_v2_health() -> None:
    global _installed, _original_builder
    if _installed:
        return

    # Capture the builder at install time, not import time. This preserves any
    # preceding read-plan optimization (for example Bulk Reads V1) and wraps it
    # with scheduler health instead of accidentally restoring the legacy N+1 path.
    _original_builder = dashboard_module.build_dashboard_operations_v2
    dashboard_module.build_dashboard_operations_v2 = _build_with_scheduler_health
    dashboard_module.DASHBOARD_OPERATIONS_V2_VERSION = DASHBOARD_OPERATIONS_V2_HEALTH_VERSION
    dashboard_module.DASHBOARD_OPERATIONS_V2_HTML = _patch_html(
        dashboard_module.DASHBOARD_OPERATIONS_V2_HTML
    )
    _installed = True
