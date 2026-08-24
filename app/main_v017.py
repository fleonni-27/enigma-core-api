from app.daily_prediction_runner import router as daily_prediction_runner_router
from app.main_v016 import app

app.version = "0.30.0"
app.include_router(daily_prediction_runner_router)
