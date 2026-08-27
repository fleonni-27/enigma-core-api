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

# Supabase session-mode pool currently exposes a hard 15-client ceiling. Enigma
# processes sometimes need two concurrent DB sessions internally (for example a
# long-lived advisory-lock connection plus a scoped ORM session in the same J1
# cycle). Keep exactly two fixed connections per process and disallow overflow,
# which preserves that internal concurrency without allowing unbounded bursts.
# SQLite is used by CI and does not accept QueuePool-only arguments.
if database_url.startswith("sqlite"):
    engine = create_engine(database_url, pool_pre_ping=True)
else:
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=15,
        pool_recycle=120,
        pool_use_lifo=True,
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
