from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["Dashboard Match Center V3"])


@router.get("/dashboard/dashboard/match-center-v3", include_in_schema=False)
def dashboard_match_center_v3_duplicate_path_redirect() -> RedirectResponse:
    return RedirectResponse(url="/dashboard/match-center-v3", status_code=307)


@router.get("/match-center-v3", include_in_schema=False)
def dashboard_match_center_v3_short_path_redirect() -> RedirectResponse:
    return RedirectResponse(url="/dashboard/match-center-v3", status_code=307)
