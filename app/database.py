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

# Supabase session-mode pooling has a hard project-wide client ceiling. Enigma
# runs the web service, the J1 cron and multiple background workers, so the
# SQLAlchemy defaults (pool_size=5, max_overflow=10 *per process*) can exhaust
# the upstream pool even at modest traffic. Keep one durable connection per
# process and queue short bursts locally instead of opening overflow sessions.
engine = create_engine(
    database_url,
    pool_pre_ping=True,
    pool_size=1,
    max_overflow=0,
    pool_timeout=15,
    pool_recycle=120,
    pool_use_lifo=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
