from __future__ import annotations

import hashlib
import math
import io
import json
import os
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, delete, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .analysis import (
    ALGORITHM_VERSION,
    AVAILABLE_ALGORITHMS,
    analyze_session,
    chunk_timeline_sample_offset,
    session_elapsed_seconds,
)
from .auth import (
    build_auth_router,
    check_origin,
    resolve_principal,
    session_cookie_name,
)
from .garmin import import_for_session
from .oximetry import OximetryResult, analyze_oximetry
from .review import plan_batch, score_batch
from .snoring import (
    BURST_THRESHOLD_DB,
    ENVELOPE_HZ,
    EPOCH_SECONDS,
    band_envelope,
    detect_bursts,
    keep_loud_bursts,
    rolling_floor,
    to_dbfs,
)
from .models import (
    AudioChunk,
    Base,
    RespiratoryEvent,
    SignalPoint,
    ReviewItem,
    SleepArchitecture,
    SleepSession,
    utc_now,
)
from .schemas import (
    GarminImportRequest,
    LabelUpdate,
    ReviewBatchRequest,
    ReviewUpdate,
    SessionCreate,
    SignalBatch,
)


def add_missing_columns(engine) -> None:
    """Add columns introduced after a database was first created.

    create_all() adds new tables but never new columns, and this prototype has no
    migration tool, so nightly data would otherwise have to be thrown away to pick
    up a new field. Only additive, nullable/defaulted columns belong here.
    """
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable and column.default is None:
                raise RuntimeError(
                    f"Cannot add required column {table.name}.{column.name} automatically"
                )
            kind = column.type.compile(engine.dialect)
            default = column.default.arg if column.default is not None else None
            clause = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {kind}'
            if default is not None and not callable(default):
                clause += f" DEFAULT {default!r}" if isinstance(default, str) else f" DEFAULT {default}"
            with engine.begin() as connection:
                connection.execute(text(clause))


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
        "chunk_count": len(row.chunks),
        "snoring_burden_percent": round(row.snoring_burden_percent or 0.0, 1),
        "snore_bursts": row.snore_bursts or 0,
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
    add_missing_columns(engine)

    app = FastAPI(title="Apnea screening prototype", version="0.1.0")
    insecure_development = os.getenv("APNEA_ALLOW_INSECURE_DEV") == "1"
    secure_cookies = not insecure_development
    session_cookie = session_cookie_name(secure_cookies)
    trust_forwarded_for = os.getenv("APNEA_TRUST_FORWARDED_FOR") == "1"
    trusted_origins = {
        origin.strip().rstrip("/")
        for origin in os.getenv("APNEA_TRUSTED_ORIGINS", "").split(",")
        if origin.strip()
    }
    unsafe_methods = {"POST", "PUT", "PATCH", "DELETE"}
    open_paths = {"/api/health"}

    @app.middleware("http")
    async def authenticate_api(request: Request, call_next):
        path = request.url.path
        guarded = path.startswith("/api/") and path not in open_paths
        # /api/auth/* is public at the edge and enforces its own rules per route.
        is_auth_route = path == "/api/auth" or path.startswith("/api/auth/")

        if guarded and not insecure_development:
            bearer = request.headers.get("authorization", "").startswith("Bearer ")
            if (
                request.method in unsafe_methods
                and not bearer
                and not check_origin(request, trusted_origins)
            ):
                return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
            if not is_auth_route:
                with database() as db:
                    principal = resolve_principal(request, db, session_cookie)
                if principal is None:
                    return JSONResponse({"detail": "Authentication required"}, status_code=401)
                if not principal.fully_authorized:
                    return JSONResponse(
                        {"detail": "MFA required", "needs_mfa": True}, status_code=403
                    )

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        if not insecure_development:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if path.startswith("/api/"):
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
    def rerun_analysis(
        session_id: str, algorithm: str = ALGORITHM_VERSION, db: Session = Depends(get_db)
    ) -> dict:
        if algorithm not in AVAILABLE_ALGORITHMS:
            raise HTTPException(422, f"algorithm must be one of {list(AVAILABLE_ALGORITHMS)}")
        session = get_session_or_404(session_id, db)
        events = analyze_session(db, session, root, algorithm=algorithm)
        return {"events": events, "algorithm_version": algorithm}

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
        return clip_response(
            session,
            event.start_offset_seconds - max(0, before),
            event.start_offset_seconds + event.duration_seconds + max(0, after),
            db,
        )

    @app.get("/api/events/{event_id}/waveform")
    def event_waveform(
        event_id: int, before: float = 30, after: float = 30, db: Session = Depends(get_db)
    ) -> dict:
        event = db.get(RespiratoryEvent, event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        session = get_session_or_404(event.session_id, db)
        if not (0 <= before <= 120 and 0 <= after <= 120):
            raise HTTPException(422, "before and after must each be between 0 and 120 seconds")
        return waveform_for(
            session,
            event.start_offset_seconds - max(0, before),
            event.start_offset_seconds + event.duration_seconds + max(0, after),
            event.start_offset_seconds,
            event.start_offset_seconds + event.duration_seconds,
            db,
        )

    def clip_response(
        session: SleepSession, start_seconds: float, end_seconds: float, db: Session
    ) -> StreamingResponse:
        rate = session.sample_rate
        first_sample = max(0, int(start_seconds * rate))
        last_sample = int(end_seconds * rate)
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

    def session_oximetry(session: SleepSession, db: Session) -> OximetryResult:
        started_at = _as_utc(session.started_at_utc)
        rows = db.scalars(
            select(SignalPoint)
            .where(
                SignalPoint.session_id == session.id,
                SignalPoint.signal_type == "spo2",
                SignalPoint.timestamp_utc >= started_at,
                SignalPoint.timestamp_utc
                <= started_at
                + timedelta(seconds=session_elapsed_seconds(session, session.chunks)),
            )
            .order_by(SignalPoint.timestamp_utc)
        )
        return analyze_oximetry(
            [((_as_utc(row.timestamp_utc) - started_at).total_seconds(), row.value) for row in rows]
        )

    @app.get("/api/sessions/{session_id}/oximetry")
    def get_oximetry(session_id: str, db: Session = Depends(get_db)) -> dict:
        session = get_session_or_404(session_id, db)
        return session_oximetry(session, db).as_json()

    def snoring_mask_for(session: SleepSession, db: Session) -> np.ndarray:
        """Rebuild the per-epoch snoring flag from the stored snore_rate series.

        Analysis writes one snore_rate point per snoring epoch, so the mask can be
        recovered without re-reading a whole night of audio.
        """
        duration = session_elapsed_seconds(session, session.chunks)
        epochs = max(1, math.ceil(duration / EPOCH_SECONDS))
        mask = np.zeros(epochs, dtype=bool)
        started_at = _as_utc(session.started_at_utc)
        rows = db.scalars(
            select(SignalPoint).where(
                SignalPoint.session_id == session.id,
                SignalPoint.signal_type == "snore_rate",
            )
        )
        for row in rows:
            index = int((_as_utc(row.timestamp_utc) - started_at).total_seconds() // EPOCH_SECONDS)
            if 0 <= index < epochs:
                mask[index] = True
        return mask

    def read_window(session: SleepSession, start_seconds: float, end_seconds: float, db: Session):
        rate = session.sample_rate
        first_sample = max(0, int(start_seconds * rate))
        last_sample = int(end_seconds * rate)
        samples = np.zeros(max(0, last_sample - first_sample), dtype=np.int16)
        for chunk in db.scalars(
            select(AudioChunk).where(AudioChunk.session_id == session.id)
        ):
            offset = chunk_timeline_sample_offset(session, chunk)
            if offset >= last_sample or offset + chunk.sample_count <= first_sample:
                continue
            overlap_start = max(first_sample, offset)
            overlap_end = min(last_sample, offset + chunk.sample_count)
            with wave.open(str(root / chunk.filename), "rb") as source:
                source.setpos(overlap_start - offset)
                data = np.frombuffer(
                    source.readframes(overlap_end - overlap_start), dtype="<i2"
                )
            samples[overlap_start - first_sample : overlap_start - first_sample + data.size] = data
        return samples

    def waveform_for(
        session: SleepSession,
        start_seconds: float,
        end_seconds: float,
        window_start: float,
        window_end: float,
        db: Session,
    ) -> dict:
        start_seconds = max(0.0, start_seconds)
        samples = read_window(session, start_seconds, end_seconds, db)
        envelope_db = to_dbfs(band_envelope(samples, session.sample_rate))
        floor_db = rolling_floor(envelope_db)
        bursts = keep_loud_bursts(detect_bursts(envelope_db, floor_db))
        started_at = _as_utc(session.started_at_utc)
        spo2 = [
            {
                "offset": (_as_utc(row.timestamp_utc) - started_at).total_seconds(),
                "value": row.value,
            }
            for row in db.scalars(
                select(SignalPoint)
                .where(
                    SignalPoint.session_id == session.id,
                    SignalPoint.signal_type == "spo2",
                    SignalPoint.timestamp_utc
                    >= started_at + timedelta(seconds=start_seconds - 120),
                    SignalPoint.timestamp_utc
                    <= started_at + timedelta(seconds=end_seconds + 180),
                )
                .order_by(SignalPoint.timestamp_utc)
            )
        ]
        return {
            "start_offset_seconds": round(start_seconds, 2),
            "end_offset_seconds": round(end_seconds, 2),
            "window": [round(window_start, 2), round(window_end, 2)],
            "sample_rate_hz": ENVELOPE_HZ,
            "envelope_dbfs": [round(float(value), 1) for value in envelope_db],
            "floor_dbfs": [round(float(value), 1) for value in floor_db],
            "burst_threshold_db": BURST_THRESHOLD_DB,
            "bursts": [
                {
                    "start": round(start_seconds + burst.start, 2),
                    "duration": round(burst.duration, 2),
                    "peak_dbfs": round(burst.peak_dbfs, 1),
                }
                for burst in bursts
            ],
            "spo2": spo2,
        }

    def review_item_or_404(item_id: int, db: Session) -> ReviewItem:
        item = db.get(ReviewItem, item_id)
        if not item:
            raise HTTPException(404, "Review item not found")
        return item

    def item_json(item: ReviewItem, reveal: bool, db: Session) -> dict:
        payload = {
            "id": item.id,
            "position": item.position,
            "batch": item.batch,
            "session_id": item.session_id,
            "start_offset_seconds": item.start_offset_seconds,
            "duration_seconds": item.duration_seconds,
            "label": item.label,
            "labeled": item.label is not None,
        }
        # `kind` is what makes the batch blinded, so it is withheld until a label exists
        if reveal or item.label is not None:
            payload["kind"] = item.kind
            event = db.get(RespiratoryEvent, item.event_id) if item.event_id else None
            payload["event"] = _event_json(event) if event else None
        return payload

    @app.post("/api/sessions/{session_id}/review-batch")
    def create_review_batch(
        session_id: str,
        payload: ReviewBatchRequest | None = None,
        db: Session = Depends(get_db),
    ) -> dict:
        session = get_session_or_404(session_id, db)
        request = payload or ReviewBatchRequest()
        events = list(
            db.scalars(
                select(RespiratoryEvent)
                .where(RespiratoryEvent.session_id == session_id)
                .order_by(RespiratoryEvent.start_offset_seconds)
            )
        )
        if not events:
            raise HTTPException(422, "Run analysis before building a review batch")
        mask = snoring_mask_for(session, db)
        batch, planned = plan_batch(
            [(e.id, e.start_offset_seconds, e.duration_seconds) for e in events],
            mask,
            EPOCH_SECONDS,
            control_ratio=request.control_ratio,
            seed=request.seed,
        )
        db.execute(delete(ReviewItem).where(ReviewItem.session_id == session_id))
        for position, item in enumerate(planned):
            db.add(
                ReviewItem(
                    session_id=session_id,
                    batch=batch,
                    position=position,
                    kind=item.kind,
                    event_id=item.event_id,
                    start_offset_seconds=item.start_offset_seconds,
                    duration_seconds=item.duration_seconds,
                )
            )
        db.commit()
        counts = {"candidate": 0, "control": 0}
        for item in planned:
            counts[item.kind] += 1
        return {"batch": batch, "items": len(planned), **counts}

    @app.get("/api/sessions/{session_id}/review-batch")
    def get_review_batch(session_id: str, db: Session = Depends(get_db)) -> list[dict]:
        get_session_or_404(session_id, db)
        items = db.scalars(
            select(ReviewItem)
            .where(ReviewItem.session_id == session_id)
            .order_by(ReviewItem.position)
        )
        return [item_json(item, reveal=False, db=db) for item in items]

    @app.patch("/api/review-items/{item_id}")
    def label_review_item(
        item_id: int, payload: LabelUpdate, db: Session = Depends(get_db)
    ) -> dict:
        item = review_item_or_404(item_id, db)
        item.label = payload.label
        item.labeled_at = utc_now()
        db.commit()
        return item_json(item, reveal=True, db=db)

    @app.get("/api/review-items/{item_id}/audio.wav")
    def review_item_audio(
        item_id: int, before: float = 30, after: float = 30, db: Session = Depends(get_db)
    ) -> StreamingResponse:
        item = review_item_or_404(item_id, db)
        session = get_session_or_404(item.session_id, db)
        if not (0 <= before <= 120 and 0 <= after <= 120):
            raise HTTPException(422, "before and after must each be between 0 and 120 seconds")
        return clip_response(
            session,
            item.start_offset_seconds - max(0, before),
            item.start_offset_seconds + item.duration_seconds + max(0, after),
            db,
        )

    @app.get("/api/review-items/{item_id}/waveform")
    def review_item_waveform(
        item_id: int, before: float = 30, after: float = 30, db: Session = Depends(get_db)
    ) -> dict:
        item = review_item_or_404(item_id, db)
        session = get_session_or_404(item.session_id, db)
        return waveform_for(
            session,
            item.start_offset_seconds - max(0, before),
            item.start_offset_seconds + item.duration_seconds + max(0, after),
            item.start_offset_seconds,
            item.start_offset_seconds + item.duration_seconds,
            db,
        )

    @app.get("/api/sessions/{session_id}/review-stats")
    def review_stats(session_id: str, db: Session = Depends(get_db)) -> dict:
        get_session_or_404(session_id, db)
        rows = db.execute(
            select(ReviewItem.kind, ReviewItem.label).where(ReviewItem.session_id == session_id)
        ).all()
        return score_batch([(kind, label) for kind, label in rows])

    @app.get("/api/sessions/{session_id}/summary")
    def summary(session_id: str, db: Session = Depends(get_db)) -> dict:
        session = get_session_or_404(session_id, db)
        events = list(
            db.scalars(
                select(RespiratoryEvent).where(RespiratoryEvent.session_id == session_id)
            )
        )
        oximetry = session_oximetry(session, db).as_json()
        hours = session.total_samples / session.sample_rate / 3600 if session.sample_rate else 0
        correlated = sum(
            1
            for event in events
            if (json.loads(event.evidence_json).get("spo2_drop") or 0) >= 3.0
        )
        architecture = db.scalar(
            select(SleepArchitecture).where(SleepArchitecture.session_id == session_id)
        )
        sleep_seconds = (architecture.sleep_seconds or 0) if architecture else 0

        def stage_percent(value: int | None) -> float | None:
            if not architecture or not sleep_seconds or value is None:
                return None
            return round(100.0 * value / sleep_seconds, 1)

        return {
            "hours_analyzed": round(hours, 3),
            "suspected_events": len(events),
            "algorithm_version": events[0].algorithm_version if events else ALGORITHM_VERSION,
            "snoring_burden_percent": round(session.snoring_burden_percent or 0.0, 1),
            "snore_bursts": session.snore_bursts or 0,
            "sleep_architecture": None
            if architecture is None
            else {
                "calendar_date": architecture.calendar_date,
                "sleep_score": architecture.sleep_score,
                "sleep_hours": round(sleep_seconds / 3600, 2) if sleep_seconds else None,
                "deep_percent": stage_percent(architecture.deep_seconds),
                "light_percent": stage_percent(architecture.light_seconds),
                "rem_percent": stage_percent(architecture.rem_seconds),
                "awake_count": architecture.awake_count,
                "restless_moments": architecture.restless_moments,
                "average_respiration": architecture.average_respiration,
                "lowest_respiration": architecture.lowest_respiration,
                "note": (
                    "Vendor sleep staging and score are proprietary context, not screening "
                    "metrics. Light-heavy architecture with many restless moments is "
                    "consistent with fragmented sleep but is not specific to apnea."
                ),
            },
            "srei": round(len(events) / hours, 2) if hours else None,
            "events_over_20s": sum(event.duration_seconds >= 20 for event in events),
            "events_over_30s": sum(event.duration_seconds >= 30 for event in events),
            "correlated_events": correlated,
            "minimum_spo2": oximetry["minimum_spo2"],
            "mean_spo2": oximetry["mean_spo2"],
            "odi3": oximetry["odi3"],
            "odi4": oximetry["odi4"],
            "t90_seconds": oximetry["t90_seconds"],
            "spo2_coverage_hours": oximetry["coverage_hours"],
            "oximetry": oximetry,
            "disclaimer": "Screening metric only. SREI is not AHI and this is not a diagnosis.",
        }

    app.include_router(
        build_auth_router(
            database,
            secure_cookies=secure_cookies,
            trusted_origins=trusted_origins,
            insecure_dev=insecure_development,
            trust_forwarded_for=trust_forwarded_for,
        )
    )

    static = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static, html=True), name="dashboard")
    return app


app = create_app()
