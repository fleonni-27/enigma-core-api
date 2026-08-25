from app.main_v015 import app
from app.enigma_rating import router as enigma_rating_router
from app.forward_test_report_v2 import router as forward_test_report_v2_router
from app.forward_test_report_v3 import router as forward_test_report_v3_router
from app.performance_observatory import router as performance_observatory_router
from app.odds_window_clv import router as odds_window_clv_router

# Current production wrapper. Routers are registered in main_v015 for backward
# compatibility with Render services whose start command has not been updated.
app.version = "0.48.0"
app.include_router(enigma_rating_router)
app.include_router(performance_observatory_router)
app.include_router(odds_window_clv_router)
app.include_router(forward_test_report_v2_router)
app.include_router(forward_test_report_v3_router)
