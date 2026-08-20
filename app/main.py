from datetime import date

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from app.database import engine
from app.sportmonks import SportmonksClient

app = FastAPI(title="Enigma Core API", version="0.1.0")


@app.get("/health")
def health() -> dict:
    database = "ok"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        database = "unavailable"

    return {"status": "ok", "database": database, "service": "enigma-core-api"}


@app.get("/fixtures/today")
async def fixtures_today() -> dict:
    try:
        return await SportmonksClient().fixtures_by_date(date.today())
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Sportmonks request failed") from exc


@app.get("/fixtures/{fixture_id}")
def fixture_by_id(fixture_id: int) -> dict:
    return {"fixture_id": fixture_id, "status": "database lookup pending"}
