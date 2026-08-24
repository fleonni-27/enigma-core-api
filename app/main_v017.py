from app.main_v015 import app
from app.enigma_rating import router as enigma_rating_router

# Current production wrapper. Routers are registered in main_v015 for backward
# compatibility with Render services whose start command has not been updated.
app.version = "0.41.0"
app.include_router(enigma_rating_router)
