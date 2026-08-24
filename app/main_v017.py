from app.main_v015 import app

# Current production wrapper. Routers are registered in main_v015 for backward
# compatibility with Render services whose start command has not been updated.
app.version = "0.30.0"
