from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

database_url = settings.database_url
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)

# Supabase session-mode pool currently exposes a hard 15-client ceiling. The
# Enigma deployment has multiple long-lived processes (web + J1 workers) plus
# short-lived cron jobs; SQLAlchemy's defaults (pool_size=5, max_overflow=10)
# allow a single process to consume the entire provider allowance. Keep each
# process deliberately small so aggregate concurrency stays below the upstream
# limit while still allowing one temporary burst connection when needed.
engine = create_engine(
    database_url,
    pool_pre_ping=True,
    pool_size=1,
    max_overflow=1,
    pool_timeout=10,
    pool_recycle=300,
    pool_use_lifo=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
