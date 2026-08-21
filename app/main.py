from datetime import date

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.database import engine
from app.sportmonks import SportmonksClient

app = FastAPI(title="Enigma Core API", version="0.1.2")


def classify_database_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "password authentication failed" in message or "authentication failed" in message:
        return "authentication_failed"
    if "ssl" in message or "certificate" in message:
        return "ssl_failed"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "name or service not known" in message or "could not translate host name" in message or "nodename nor servname" in message:
        return "dns_failed"
    if "connection refused" in message:
        return "connection_refused"
    if isinstance(exc, OperationalError):
        return "operational_error"
    return "connection_error"


@app.get("/health")
def health() -> dict:
    database = "ok"
    database_error = None
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        database = "unavailable"
        database_error = classify_database_error(exc)

    return {
        "status": "ok",
        "database": database,
        "database_error": database_error,
        "service": "enigma-core-api",
    }


@app.get("/fixtures/today")
async def fixtures_today() -> dict:
    try:
        return await SportmonksClient().fixtures_by_date(date.today())
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Sportmonks request failed") from exc


@app.get("/fixtures/date/{target_date}")
async def fixtures_by_date(target_date: date) -> dict:
    try:
        return await SportmonksClient().fixtures_by_date(target_date)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Sportmonks request failed") from exc


@app.get("/fixtures/{fixture_id}")
def fixture_by_id(fixture_id: int) -> dict:
    return {"fixture_id": fixture_id, "status": "database lookup pending"}
