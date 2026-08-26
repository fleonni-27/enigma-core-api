from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo("America/Sao_Paulo")
J1_MAX_LATENESS_MINUTES = 20


def annotate_watchlist_j1_window(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate monitoring-only fixtures with the real J1 operational window.

    This does not create or mutate Prediction/Decision/ledger rows. It only makes
    the dashboard show whether the normal J1 target/tolerance window is open while
    the provider fixture is still missing.
    """
    now = datetime.now(BUSINESS_TIMEZONE)
    output: list[dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        due = datetime.fromisoformat(str(item["j1_due_at"]))
        if due.tzinfo is None:
            due = due.replace(tzinfo=BUSINESS_TIMEZONE)
        due_local = due.astimezone(BUSINESS_TIMEZONE)
        close_local = due_local + timedelta(minutes=J1_MAX_LATENESS_MINUTES)
        kickoff = datetime.fromisoformat(str(item["starts_at"]))
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=BUSINESS_TIMEZONE)
        kickoff_local = kickoff.astimezone(BUSINESS_TIMEZONE)

        if now < due_local:
            status = "J1_AGUARDANDO"
            label = "JANELA 1 AGUARDANDO"
        elif now <= close_local:
            status = "J1_ATIVA_TOLERANCIA"
            label = "JANELA 1 ATIVA · TOLERÂNCIA"
        elif now < kickoff_local:
            status = "J1_OFICIAL_EXPIRADA"
            label = "J1 OFICIAL EXPIRADA · MONITORAMENTO"
        else:
            status = "KICKOFF_INICIADO"
            label = "KICKOFF INICIADO · SEM J1 OFICIAL"

        item.update(
            {
                "j1_operational_status": status,
                "j1_operational_label": label,
                "j1_window_opened_at": due_local.isoformat(),
                "j1_window_closes_at": close_local.isoformat(),
                "j1_official_blocked_by_missing_provider_fixture": True,
            }
        )
        output.append(item)
    return output
