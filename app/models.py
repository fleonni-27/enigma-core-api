from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Fixture(Base):
    __tablename__ = "fixtures"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sportmonks_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    league_name: Mapped[str | None] = mapped_column(String(160))
    home_team: Mapped[str] = mapped_column(String(160))
    away_team: Mapped[str] = mapped_column(String(160))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (UniqueConstraint("fixture_id", "window", "model_version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    window: Mapped[str] = mapped_column(String(30))  # J0, J1, PRE_CLOSE
    model_version: Mapped[str] = mapped_column(String(30))
    p_home: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    p_draw: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    p_away: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    bookmaker: Mapped[str] = mapped_column(String(120), index=True)
    market: Mapped[str] = mapped_column(String(80), index=True)
    selection: Mapped[str] = mapped_column(String(120))
    odd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    source: Mapped[str] = mapped_column(String(80), default="sportmonks")
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    window: Mapped[str | None] = mapped_column(String(30))
