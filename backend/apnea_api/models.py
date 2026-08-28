from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SleepSession(Base):
    __tablename__ = "sleep_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="recording")
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at_monotonic_ns: Mapped[int] = mapped_column(BigInteger)
    sample_rate: Mapped[int] = mapped_column(Integer, default=16_000)
    total_samples: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    chunks: Mapped[list[AudioChunk]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    signals: Mapped[list[SignalPoint]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    events: Mapped[list[RespiratoryEvent]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class AudioChunk(Base):
    __tablename__ = "audio_chunks"
    __table_args__ = (UniqueConstraint("session_id", "sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sleep_sessions.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(300))
    sample_offset: Mapped[int] = mapped_column(BigInteger)
    sample_count: Mapped[int] = mapped_column(BigInteger)
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at_monotonic_ns: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped[SleepSession] = relationship(back_populates="chunks")


class SignalPoint(Base):
    __tablename__ = "signal_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sleep_sessions.id", ondelete="CASCADE"))
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    signal_type: Mapped[str] = mapped_column(String(50), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(80))
    device: Mapped[str | None] = mapped_column(String(120), nullable=True)

    session: Mapped[SleepSession] = relationship(back_populates="signals")


class RespiratoryEvent(Base):
    __tablename__ = "respiratory_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sleep_sessions.id", ondelete="CASCADE"))
    start_offset_seconds: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_json: Mapped[str] = mapped_column(Text)
    algorithm_version: Mapped[str] = mapped_column(String(80))
    review_status: Mapped[str] = mapped_column(String(30), default="unreviewed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped[SleepSession] = relationship(back_populates="events")
