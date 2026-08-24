from app.daily_operations import router as daily_operations_router
from app.main_v015 import app

app.version = "0.29.0"
app.include_router(daily_operations_router)
