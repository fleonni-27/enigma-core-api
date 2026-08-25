from __future__ import annotations

from app.dashboard_match_center_v3_5m import (
    DASHBOARD_MATCH_CENTER_V3_5M_HTML,
    DASHBOARD_MATCH_CENTER_V3_REFRESH_MS,
)


def test_match_center_refresh_is_five_minutes() -> None:
    assert DASHBOARD_MATCH_CENTER_V3_REFRESH_MS == 300_000
    assert "setInterval(load,300000)" in DASHBOARD_MATCH_CENTER_V3_5M_HTML
    assert "setInterval(load,60000)" not in DASHBOARD_MATCH_CENTER_V3_5M_HTML
