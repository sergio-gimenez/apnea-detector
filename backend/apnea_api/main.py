from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .analysis import (
    ALGORITHM_VERSION,
    analyze_session,
    chunk_timeline_sample_offset,
    session_elapsed_seconds,
)
from .garmin import import_for_session
from .models import AudioChunk, Base, RespiratoryEvent, SignalPoint, SleepSession, utc_now
from .schemas import GarminImportRequest, ReviewUpdate, SessionCreate, SignalBatch


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _session_json(row: SleepSession) -> dict:
    duration = session_elapsed_seconds(row, row.chunks)
    return {
        "id": row.id,
        "device_id": row.device_id,
        "status": row.status,
        "started_at_utc": _as_utc(row.started_at_utc).isoformat(),
        "started_at_monotonic_ns": row.started_at_monotonic_ns,
        "sample_rate": row.sample_rate,
        "total_samples": row.total_samples,
        "recorded_seconds": row.total_samples / row.sample_rate if row.sample_rate else 0,
        "duration_seconds": duration,
        "created_at": _as_utc(row.created_at).isoformat(),
        "completed_at": _as_utc(row.completed_at).isoformat() if row.completed_at else None,
    }


def _event_json(row: RespiratoryEvent) -> dict:
    return {
        "id": row.id,
        "start_offset_seconds": row.start_offset_seconds,
        "duration_seconds": row.duration_seconds,
        "confidence": row.confidence,
        "evidence": json.loads(row.evidence_json),
        "algorithm_version": row.algorithm_version,
        "review_status": row.review_status,
    }


def create_app(data_dir: Path | None = None, database_url: str | None = None) -> FastAPI:
    root = (data_dir or Path(os.getenv("APNEA_DATA_DIR", "./data"))).resolve()
    chunks_root = root / "audio"
    chunks_root.mkdir(parents=True, exist_ok=True)
    database_url = database_url or os.getenv("DATABASE_URL", f"sqlite:///{root / 'apnea.db'}")
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    database = sessionmaker(engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    app = FastAPI(title="Apnea screening prototype", version="0.1.0")
    api_token = os.getenv("APNEA_API_TOKEN")
    insecure_development = os.getenv("APNEA_ALLOW_INSECURE_DEV") == "1"
    if not api_token and not insecure_development:
        raise RuntimeError(
            "APNEA_API_TOKEN is required; set APNEA_ALLOW_INSECURE_DEV=1 only for local development"
        )
    if api_token and len(api_token) < 32:
        raise RuntimeError("APNEA_API_TOKEN must contain at least 32 characters")

    @app.middleware("http")
    async def authenticate_api(request: Request, call_next):
        if api_token and request.url.path.startswith("/api/") and request.url.path != "/api/health":
            authorization = request.headers.get("Authorization", "")
            supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            if not hmac.compare_digest(supplied, api_token):
                return JSONResponse({"detail": "Valid bearer token required"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; img-src 'self' data:"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_db():
        with database() as db:
            yield db

    def get_session_or_404(session_id: str, db: Session) -> SleepSession:
        row = db.get(SleepSession, session_id)
        if not row:
            raise HTTPException(404, "Session not found")
        return row

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "algorithm_version": ALGORITHM_VERSION}

    @app.post("/api/sessions")
    def create_session(payload: SessionCreate, db: Session = Depends(get_db)) -> dict:
        try:
            uuid.UUID(payload.id)
        except ValueError as exc:
            raise HTTPException(422, "id must be a UUID") from exc
        existing = db.get(SleepSession, payload.id)
        if existing:
            return _session_json(existing)
        row = SleepSession(
            id=payload.id,
            device_id=payload.device_id,
            started_at_utc=_as_utc(payload.started_at_utc),
            started_at_monotonic_ns=payload.started_at_monotonic_ns,
            sample_rate=payload.sample_rate,
        )
        db.add(row)
        db.commit()
        return _session_json(row)

    @app.get("/api/sessions")
    def list_sessions(db: Session = Depends(get_db)) -> list[dict]:
        rows = db.scalars(select(SleepSession).order_by(SleepSession.started_at_utc.desc()))
        return [_session_json(row) for row in rows]

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, db: Session = Depends(get_db)) -> dict:
        return _session_json(get_session_or_404(session_id, db))

    @app.post("/api/sessions/{session_id}/audio-chunks")
    async def upload_audio_chunk(
        session_id: str,
        sequence: int = Form(ge=0, le=5000),
        sample_offset: int = Form(ge=0, le=2_764_800_000),
        sample_count: int = Form(gt=0, le=14_400_000),
        started_at_utc: datetime = Form(),
        started_at_monotonic_ns: int = Form(ge=0),
        file: UploadFile = File(),
        db: Session = Depends(get_db),
    ) -> dict:
        session = get_session_or_404(session_id, db)
        content = await file.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "Chunk exceeds 10 MiB")
        digest = hashlib.sha256(content).hexdigest()
        existing = db.scalar(
            select(AudioChunk).where(
                AudioChunk.session_id == session_id, AudioChunk.sequence == sequence
            )
        )
        if existing:
            same_metadata = (
                existing.sha256 == digest
                and existing.sample_offset == sample_offset
                and existing.sample_count == sample_count
                and existing.started_at_monotonic_ns == started_at_monotonic_ns
                and _as_utc(existing.started_at_utc) == _as_utc(started_at_utc)
            )
            if not same_metadata:
                raise HTTPException(409, "Sequence already contains different audio")
            return {"status": "already_uploaded", "sequence": sequence}
        if session.status == "complete":
            raise HTTPException(409, "Cannot append audio to a completed session")
        try:
            with wave.open(io.BytesIO(content), "rb") as wav_file:
                actual_count = wav_file.getnframes()
                valid = (
                    wav_file.getnchannels() == 1
                    and wav_file.getsampwidth() == 2
                    and wav_file.getframerate() == session.sample_rate
                )
                decoded = wav_file.readframes(actual_count + 1)
        except wave.Error as exc:
            raise HTTPException(422, f"Invalid WAV: {exc}") from exc
        if not valid:
            raise HTTPException(422, "Expected mono, PCM 16-bit WAV at session sample rate")
        if actual_count != sample_count:
            raise HTTPException(422, f"sample_count says {sample_count}; WAV contains {actual_count}")
        if len(decoded) != sample_count * 2:
            raise HTTPException(422, "WAV payload is truncated or contains invalid PCM data")

        overlap = db.scalar(
            select(AudioChunk).where(
                AudioChunk.session_id == session_id,
                AudioChunk.sample_offset < sample_offset + sample_count,
                AudioChunk.sample_offset + AudioChunk.sample_count > sample_offset,
            )
        )
        if overlap:
            raise HTTPException(409, f"Audio overlaps existing sequence {overlap.sequence}")

        session_dir = chunks_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        relative = Path("audio") / session_id / f"audio_{sequence:05d}.wav"
        destination = root / relative
        temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, destination)
        row = AudioChunk(
            session_id=session_id,
            sequence=sequence,
            filename=str(relative),
            sample_offset=sample_offset,
            sample_count=sample_count,
            started_at_utc=_as_utc(started_at_utc),
            started_at_monotonic_ns=started_at_monotonic_ns,
            sha256=digest,
        )
        db.add(row)
        session.total_samples = max(session.total_samples, sample_offset + sample_count)
        db.commit()
        return {"status": "uploaded", "sequence": sequence, "sha256": digest}

    @app.post("/api/sessions/{session_id}/complete")
    def complete_session(session_id: str, db: Session = Depends(get_db)) -> dict:
        session = get_session_or_404(session_id, db)
        session.status = "complete"
        session.completed_at = utc_now()
        db.commit()
        events = analyze_session(db, session, root)
        return {"status": "complete", "events": events}

    @app.post("/api/sessions/{session_id}/analyze")
    def rerun_analysis(session_id: str, db: Session = Depends(get_db)) -> dict:
        session = get_session_or_404(session_id, db)
        events = analyze_session(db, session, root)
        return {"events": events, "algorithm_version": ALGORITHM_VERSION}

    @app.post("/api/sessions/{session_id}/signals")
    def add_signals(
        session_id: str, payload: SignalBatch, db: Session = Depends(get_db)
    ) -> dict:
        session = get_session_or_404(session_id, db)
        start = _as_utc(session.started_at_utc)
        end = start + timedelta(
            seconds=session_elapsed_seconds(session, session.chunks) + 3 * 60 * 60
        )
        for point in payload.points:
            timestamp = _as_utc(point.timestamp_utc)
            if not start - timedelta(hours=1) <= timestamp <= end:
                raise HTTPException(422, "Signal timestamp is outside session correlation window")
            db.add(
                SignalPoint(
                    session_id=session_id,
                    timestamp_utc=timestamp,
                    signal_type=point.signal_type,
                    value=point.value,
                    unit=point.unit,
                    source=point.source,
                    device=point.device,
                )
            )
        db.commit()
        return {"imported": len(payload.points)}

    @app.get("/api/sessions/{session_id}/signals")
    def get_signals(session_id: str, db: Session = Depends(get_db)) -> list[dict]:
        get_session_or_404(session_id, db)
        rows = db.scalars(
            select(SignalPoint)
            .where(SignalPoint.session_id == session_id)
            .order_by(SignalPoint.timestamp_utc)
        )
        return [
            {
                "timestamp_utc": _as_utc(row.timestamp_utc).isoformat(),
                "signal_type": row.signal_type,
                "value": row.value,
                "unit": row.unit,
                "source": row.source,
                "device": row.device,
            }
            for row in rows
        ]

    @app.post("/api/sessions/{session_id}/garmin/import")
    def import_garmin(
        session_id: str,
        payload: GarminImportRequest,
        db: Session = Depends(get_db),
    ) -> dict:
        session = get_session_or_404(session_id, db)
        token_store = Path(os.getenv("GARMIN_TOKEN_STORE", root / "garmin"))
        if not token_store.exists():
            raise HTTPException(409, "Garmin is not authenticated; run apnea-garmin-login")
        try:
            return import_for_session(db, session, token_store, payload.date)
        except Exception as exc:
            raise HTTPException(502, f"Garmin import failed: {exc}") from exc

    @app.get("/api/sessions/{session_id}/events")
    def get_events(session_id: str, db: Session = Depends(get_db)) -> list[dict]:
        get_session_or_404(session_id, db)
        rows = db.scalars(
            select(RespiratoryEvent)
            .where(RespiratoryEvent.session_id == session_id)
            .order_by(RespiratoryEvent.start_offset_seconds)
        )
        return [_event_json(row) for row in rows]

    @app.patch("/api/events/{event_id}/review")
    def review_event(
        event_id: int, payload: ReviewUpdate, db: Session = Depends(get_db)
    ) -> dict:
        event = db.get(RespiratoryEvent, event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        event.review_status = payload.status
        db.commit()
        return _event_json(event)

    @app.get("/api/events/{event_id}/audio.wav")
    def event_audio(
        event_id: int,
        before: float = 30,
        after: float = 30,
        db: Session = Depends(get_db),
    ) -> StreamingResponse:
        event = db.get(RespiratoryEvent, event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        session = get_session_or_404(event.session_id, db)
        if not (0 <= before <= 120 and 0 <= after <= 120):
            raise HTTPException(422, "before and after must each be between 0 and 120 seconds")
        rate = session.sample_rate
        first_sample = max(0, int((event.start_offset_seconds - max(0, before)) * rate))
        last_sample = int(
            (event.start_offset_seconds + event.duration_seconds + max(0, after)) * rate
        )
        all_chunks = list(
            db.scalars(
                select(AudioChunk)
                .where(AudioChunk.session_id == session.id)
                .order_by(AudioChunk.started_at_monotonic_ns)
            )
        )
        chunks = sorted(
            (
                (chunk_timeline_sample_offset(session, chunk), chunk)
                for chunk in all_chunks
                if chunk_timeline_sample_offset(session, chunk) < last_sample
                and chunk_timeline_sample_offset(session, chunk) + chunk.sample_count > first_sample
            ),
            key=lambda item: item[0],
        )
        output = io.BytesIO()
        with wave.open(output, "wb") as output_wav:
            output_wav.setnchannels(1)
            output_wav.setsampwidth(2)
            output_wav.setframerate(rate)
            cursor = first_sample
            for timeline_offset, chunk in chunks:
                overlap_start = max(first_sample, timeline_offset)
                overlap_end = min(last_sample, timeline_offset + chunk.sample_count)
                if overlap_start > cursor:
                    output_wav.writeframes(b"\x00\x00" * (overlap_start - cursor))
                with wave.open(str(root / chunk.filename), "rb") as source:
                    source.setpos(overlap_start - timeline_offset)
                    output_wav.writeframes(source.readframes(overlap_end - overlap_start))
                cursor = overlap_end
            if cursor < last_sample:
                output_wav.writeframes(b"\x00\x00" * (last_sample - cursor))
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/sessions/{session_id}/summary")
    def summary(session_id: str, db: Session = Depends(get_db)) -> dict:
        session = get_session_or_404(session_id, db)
        events = list(
            db.scalars(
                select(RespiratoryEvent).where(RespiratoryEvent.session_id == session_id)
            )
        )
        spo2 = list(
            db.scalars(
                select(SignalPoint).where(
                    SignalPoint.session_id == session_id,
                    SignalPoint.signal_type == "spo2",
                    SignalPoint.timestamp_utc >= _as_utc(session.started_at_utc),
                    SignalPoint.timestamp_utc
                    <= _as_utc(session.started_at_utc)
                    + timedelta(
                        seconds=session_elapsed_seconds(session, session.chunks)
                    ),
                )
            )
        )
        hours = session.total_samples / session.sample_rate / 3600 if session.sample_rate else 0
        values = [point.value for point in spo2]
        return {
            "hours_analyzed": round(hours, 3),
            "suspected_events": len(events),
            "srei": round(len(events) / hours, 2) if hours else None,
            "events_over_20s": sum(event.duration_seconds >= 20 for event in events),
            "events_over_30s": sum(event.duration_seconds >= 30 for event in events),
            "minimum_spo2": min(values) if values else None,
            "mean_spo2": round(sum(values) / len(values), 2) if values else None,
            "disclaimer": "Screening metric only. SREI is not AHI and this is not a diagnosis.",
        }

    static = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static, html=True), name="dashboard")
    return app


app = create_app()
