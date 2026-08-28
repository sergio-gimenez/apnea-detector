from __future__ import annotations

import json
import math
import wave
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import AudioChunk, RespiratoryEvent, SignalPoint, SleepSession

ALGORITHM_VERSION = "dsp-v0.1.0"


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def chunk_timeline_sample_offset(session: SleepSession, chunk: AudioChunk) -> int:
    monotonic_delta = chunk.started_at_monotonic_ns - session.started_at_monotonic_ns
    maximum_delta = 48 * 60 * 60 * 1_000_000_000
    monotonic_seconds = monotonic_delta / 1_000_000_000
    utc_seconds = (
        _as_utc(chunk.started_at_utc) - _as_utc(session.started_at_utc)
    ).total_seconds()
    monotonic_valid = 0 <= monotonic_delta <= maximum_delta
    utc_valid = 0 <= utc_seconds <= maximum_delta / 1_000_000_000
    if monotonic_valid and utc_valid and abs(monotonic_seconds - utc_seconds) <= 5:
        elapsed_seconds = monotonic_seconds
    elif utc_valid:
        elapsed_seconds = utc_seconds
    else:
        elapsed_seconds = monotonic_seconds
    return max(0, round(elapsed_seconds * session.sample_rate))


def session_elapsed_seconds(session: SleepSession, chunks: list[AudioChunk]) -> float:
    if not chunks:
        return session.total_samples / session.sample_rate if session.sample_rate else 0.0
    return max(
        (chunk_timeline_sample_offset(session, chunk) + chunk.sample_count) / session.sample_rate
        for chunk in chunks
    )


def _physiology_points(
    db: Session, session: SleepSession, signal_type: str
) -> list[tuple[float, float]]:
    start = _as_utc(session.started_at_utc)
    rows = db.scalars(
        select(SignalPoint)
        .where(
            SignalPoint.session_id == session.id,
            SignalPoint.signal_type == signal_type,
        )
        .order_by(SignalPoint.timestamp_utc)
    )
    return [((_as_utc(row.timestamp_utc) - start).total_seconds(), row.value) for row in rows]


def detect_candidates(energy_dbfs: np.ndarray) -> tuple[list[dict], float]:
    valid = energy_dbfs[np.isfinite(energy_dbfs)]
    if valid.size < 20:
        return [], -90.0

    floor = float(np.min(valid))
    high = float(np.percentile(valid, 75))
    dynamic_range = high - floor
    if dynamic_range < 6.0:
        return [], high - 6.0
    threshold = high - max(6.0, dynamic_range * 0.4)
    low_mask = np.isfinite(energy_dbfs) & (energy_dbfs <= threshold)

    candidates: list[dict] = []
    index = 0
    while index < len(low_mask):
        if not low_mask[index]:
            index += 1
            continue
        end = index
        while end < len(low_mask) and low_mask[end]:
            end += 1
        duration = end - index
        if 10 <= duration <= 120:
            before = energy_dbfs[max(0, index - 10) : index]
            after = energy_dbfs[end : min(len(energy_dbfs), end + 8)]
            before = before[np.isfinite(before)]
            after = after[np.isfinite(after)]
            run = energy_dbfs[index:end]
            pre_peak = float(np.max(before)) if before.size else -90.0
            recovery_peak = float(np.max(after)) if after.size else -90.0
            run_median = float(np.median(run))
            recovery_gasp = bool(recovery_peak >= high + 3.0)
            confidence = 0.2
            confidence += min(0.25, 0.1 + (duration - 10) * 0.015)
            confidence += min(0.25, max(0.0, (threshold - run_median) / 15.0))
            confidence += 0.15 if recovery_gasp else 0.0
            confidence += 0.1 if pre_peak > threshold + 3.0 else 0.0
            candidates.append(
                {
                    "start": float(index),
                    "duration": float(duration),
                    "confidence": confidence,
                    "evidence": {
                        "reduced_respiratory_audio": True,
                        "pause_gt_10s": True,
                        "recovery_gasp": recovery_gasp,
                        "snoring_before": False,
                        "audio_floor_dbfs": round(run_median, 2),
                        "adaptive_threshold_dbfs": round(threshold, 2),
                        "recovery_peak_dbfs": round(recovery_peak, 2),
                    },
                }
            )
        index = end
    return candidates, threshold


def analyze_session(db: Session, session: SleepSession, data_root: Path) -> int:
    chunks = list(
        db.scalars(
            select(AudioChunk)
            .where(AudioChunk.session_id == session.id)
            .order_by(AudioChunk.sample_offset)
        )
    )
    if not chunks:
        return 0

    sample_rate = session.sample_rate
    timeline_offsets = {
        chunk.id: chunk_timeline_sample_offset(session, chunk) for chunk in chunks
    }
    total_samples = max(timeline_offsets[chunk.id] + chunk.sample_count for chunk in chunks)
    seconds = math.ceil(total_samples / sample_rate)
    sum_squares = np.zeros(seconds, dtype=np.float64)
    counts = np.zeros(seconds, dtype=np.int64)

    for chunk in chunks:
        path = data_root / chunk.filename
        with wave.open(str(path), "rb") as wav_file:
            samples = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype="<i2")
        cursor = 0
        while cursor < samples.size:
            absolute = timeline_offsets[chunk.id] + cursor
            second = absolute // sample_rate
            take = min(samples.size - cursor, sample_rate - (absolute % sample_rate))
            frame = samples[cursor : cursor + take].astype(np.float64)
            sum_squares[second] += np.dot(frame, frame)
            counts[second] += take
            cursor += take

    energy = np.full(seconds, np.nan, dtype=np.float64)
    populated = counts > 0
    rms = np.sqrt(sum_squares[populated] / counts[populated])
    energy[populated] = 20.0 * np.log10(np.maximum(rms, 1.0) / 32768.0)
    candidates, _ = detect_candidates(energy)

    db.execute(
        delete(SignalPoint).where(
            SignalPoint.session_id == session.id,
            SignalPoint.signal_type == "audio_energy",
        )
    )
    previous_reviews = [
        event
        for event in db.scalars(
            select(RespiratoryEvent).where(RespiratoryEvent.session_id == session.id)
        )
        if event.review_status != "unreviewed"
    ]
    db.execute(delete(RespiratoryEvent).where(RespiratoryEvent.session_id == session.id))

    started_at = _as_utc(session.started_at_utc)
    for offset in range(0, len(energy), 5):
        window = energy[offset : offset + 5]
        window = window[np.isfinite(window)]
        if window.size:
            db.add(
                SignalPoint(
                    session_id=session.id,
                    timestamp_utc=started_at + timedelta(seconds=offset),
                    signal_type="audio_energy",
                    value=float(np.mean(window)),
                    unit="dBFS",
                    source=ALGORITHM_VERSION,
                    device="phone microphone",
                )
            )

    spo2 = _physiology_points(db, session, "spo2")
    heart_rate = _physiology_points(db, session, "heart_rate")
    for candidate in candidates:
        start = candidate["start"]
        end = start + candidate["duration"]
        evidence = candidate["evidence"]
        confidence = candidate["confidence"]

        spo2_before = [value for offset, value in spo2 if start - 120 <= offset < start]
        spo2_after = [value for offset, value in spo2 if end <= offset <= end + 180]
        if spo2_before and spo2_after:
            drop = float(np.median(spo2_before) - min(spo2_after))
            evidence["spo2_drop"] = round(drop, 2)
            confidence += 0.15 if drop >= 3.0 else 0.0
        else:
            evidence["spo2_drop"] = None

        hr_before = [value for offset, value in heart_rate if start - 120 <= offset < start]
        hr_after = [value for offset, value in heart_rate if end <= offset <= end + 120]
        if hr_before and hr_after:
            change = float(max(hr_after) - np.median(hr_before))
            evidence["heart_rate_change"] = round(change, 2)
            confidence += 0.1 if change >= 10.0 else 0.0
        else:
            evidence["heart_rate_change"] = None

        matching_review = next(
            (
                old
                for old in previous_reviews
                if abs(old.start_offset_seconds - start) <= 2
                and abs(old.duration_seconds - candidate["duration"]) <= 2
            ),
            None,
        )
        db.add(
            RespiratoryEvent(
                session_id=session.id,
                start_offset_seconds=start,
                duration_seconds=candidate["duration"],
                confidence=min(0.99, round(confidence, 3)),
                evidence_json=json.dumps(evidence),
                algorithm_version=ALGORITHM_VERSION,
                review_status=matching_review.review_status if matching_review else "unreviewed",
            )
        )

    session.total_samples = sum(chunk.sample_count for chunk in chunks)
    db.commit()
    return len(candidates)
