from app.main_v015 import app

# Compatibility wrapper. The operations routers are registered in main_v015 so
# production remains correct even when Render is still pinned to that entrypoint.
app.version = "0.39.0"
