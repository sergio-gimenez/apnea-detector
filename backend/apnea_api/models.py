from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
    snoring_burden_percent: Mapped[float] = mapped_column(Float, default=0.0)
    snore_bursts: Mapped[int] = mapped_column(Integer, default=0)
    # operator's own context for the night: free text + a JSON array of tag strings
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    architecture: Mapped[SleepArchitecture | None] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False
    )
    review_items: Mapped[list[ReviewItem]] = relationship(
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


class SleepArchitecture(Base):
    """Nightly sleep-stage breakdown as reported by the wearable.

    Kept separate from SignalPoint because these are one-per-night summaries from
    the vendor's own scoring, not samples on the session timeline. The score is a
    proprietary composite, so it is stored as context, never as a screening metric.
    """

    __tablename__ = "sleep_architecture"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sleep_sessions.id", ondelete="CASCADE"), unique=True
    )
    calendar_date: Mapped[str] = mapped_column(String(10))
    sleep_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deep_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    light_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rem_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awake_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sleep_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awake_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restless_moments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    average_respiration: Mapped[float | None] = mapped_column(Float, nullable=True)
    lowest_respiration: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_stress: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(80), default="garmin")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped[SleepSession] = relationship(back_populates="architecture")


class User(Base):
    """The single operator account.

    ``totp_secret`` is the active MFA key; ``pending_totp_secret`` holds a key
    that has been shown for enrolment but not yet confirmed with a code. Both are
    plain base32, protected by the data directory's filesystem permissions, the
    same posture as the stored Garmin OAuth tokens.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # highest TOTP time step already accepted, so a captured code cannot be replayed
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    api_tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    recovery_codes: Mapped[list[MfaRecoveryCode]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    """A browser login. The cookie carries ``id.secret``; only the hash is stored,
    so a database leak cannot be replayed as a session."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    secret_hash: Mapped[str] = mapped_column(String(64))
    mfa_satisfied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class ApiToken(Base):
    """A long-lived bearer credential for a non-browser device (the recorder).

    Minted only by a fully authenticated session or the ``apnea-admin`` CLI, and
    revocable independently of the password.
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_tokens")


class MfaRecoveryCode(Base):
    """One single-use code that stands in for the authenticator when it is lost."""

    __tablename__ = "mfa_recovery_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship(back_populates="recovery_codes")


class ReviewItem(Base):
    """One blinded clip in a labelling batch.

    Labelling only the detector's own candidates measures precision but can never
    reveal missed events, and showing which clips are candidates invites the
    listener to agree with the detector. So a batch mixes candidates with control
    windows drawn from the same snoring periods, stores them in a shuffled order,
    and withholds `kind` until a label has been recorded.
    """

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sleep_sessions.id", ondelete="CASCADE"))
    batch: Mapped[str] = mapped_column(String(36), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))  # candidate | control
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("respiratory_events.id", ondelete="SET NULL"), nullable=True
    )
    start_offset_seconds: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[float] = mapped_column(Float)
    label: Mapped[str | None] = mapped_column(String(20), nullable=True)
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped[SleepSession] = relationship(back_populates="review_items")
