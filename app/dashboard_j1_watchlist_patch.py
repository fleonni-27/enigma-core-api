from __future__ import annotations

from app import dashboard_confirmation_holdout_v1 as dashboard_module
from app.dashboard_j1_watchlist_window import annotate_watchlist_j1_window

_ORIGINAL = dashboard_module._monitoring_watchlist
_INSTALLED = False


def install_dashboard_j1_watchlist_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    def patched(target_date, fixtures):
        return annotate_watchlist_j1_window(_ORIGINAL(target_date, fixtures))

    dashboard_module._monitoring_watchlist = patched
    _INSTALLED = True
