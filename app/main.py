from datetime import date

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.database import engine
from app.fixture_data_ingestion import ingest_fixture_data_payload
from app.ingestion import ingest_fixtures_payload
from app.odds_ingestion import ingest_prematch_odds_payload
from app.sportmonks import SportmonksClient

app = FastAPI(title="Enigma Core API", version="0.3.0")


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


@app.post("/ingest/fixtures/date/{target_date}")
async def ingest_fixtures_by_date(target_date: date) -> dict:
    try:
        payload = await SportmonksClient().fixtures_by_date(target_date)
        result = ingest_fixtures_payload(payload)
        if result.get("status") != "ok":
            raise HTTPException(status_code=500, detail=result)
        return {"date": target_date.isoformat(), **result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Fixture ingestion failed") from exc


@app.post("/ingest/odds/fixture/{sportmonks_fixture_id}")
async def ingest_prematch_odds(
    sportmonks_fixture_id: int,
    snapshot_window: str | None = Query(default=None, max_length=30),
) -> dict:
    try:
        payload = await SportmonksClient().prematch_odds_by_fixture(sportmonks_fixture_id)
        result = ingest_prematch_odds_payload(
            sportmonks_fixture_id=sportmonks_fixture_id,
            payload=payload,
            snapshot_window=snapshot_window,
        )
        if result.get("status") == "fixture_not_found":
            raise HTTPException(status_code=404, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Odds ingestion failed") from exc


@app.post("/ingest/data/fixture/{sportmonks_fixture_id}")
async def ingest_fixture_data(sportmonks_fixture_id: int) -> dict:
    try:
        payload = await SportmonksClient().enriched_fixture(sportmonks_fixture_id)
        result = ingest_fixture_data_payload(sportmonks_fixture_id, payload)
        if result.get("status") == "fixture_not_found":
            raise HTTPException(status_code=404, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Fixture data ingestion failed") from exc


@app.get("/fixtures/{fixture_id}")
def fixture_by_id(fixture_id: int) -> dict:
    return {"fixture_id": fixture_id, "status": "database lookup pending"}
