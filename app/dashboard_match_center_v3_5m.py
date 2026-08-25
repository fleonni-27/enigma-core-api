from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.dashboard_match_center_v3 import DASHBOARD_MATCH_CENTER_V3_HTML
from app.dashboard_match_center_v3_light import build_dashboard_match_center_v3

DASHBOARD_MATCH_CENTER_V3_REFRESH_MS = 300_000
router = APIRouter(tags=["Dashboard Match Center V3"])

DASHBOARD_MATCH_CENTER_V3_5M_HTML = DASHBOARD_MATCH_CENTER_V3_HTML.replace(
    "setInterval(load,60000)",
    f"setInterval(load,{DASHBOARD_MATCH_CENTER_V3_REFRESH_MS})",
).replace(
    "J1 automático · análise pré-jogo · RESEARCH ONLY",
    "J1 automático · análise pré-jogo · atualização do painel a cada 5 min · RESEARCH ONLY",
)


@router.get("/dashboard/api/match-center-v3")
def dashboard_match_center_v3_api(target_date: date | None = Query(default=None)):
    return build_dashboard_match_center_v3(target_date=target_date)


@router.get("/dashboard/match-center-v3", response_class=HTMLResponse, include_in_schema=False)
def dashboard_match_center_v3_page() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_MATCH_CENTER_V3_5M_HTML)
