from datetime import datetime, timedelta, timezone

import numpy as np

from apnea_api.analysis import chunk_timeline_sample_offset, detect_candidates
from apnea_api.models import AudioChunk, SleepSession


def test_detects_ten_second_low_energy_run_with_recovery():
    energy = np.full(60, -32.0)
    energy[20:33] = -60.0
    energy[33:36] = -20.0

    events, threshold = detect_candidates(energy)

    assert threshold < -32
    assert len(events) == 1
    assert events[0]["start"] == 20
    assert events[0]["duration"] == 13
    assert events[0]["evidence"]["recovery_gasp"] is True


def test_flat_background_does_not_create_candidates():
    events, _ = detect_candidates(np.full(60, -48.0))
    assert events == []


def test_detects_sparse_event_in_long_stable_recording():
    energy = np.full(3_600, -32.0)
    energy[1_800:1_813] = -60.0

    events, _ = detect_candidates(energy)

    assert len(events) == 1
    assert events[0]["start"] == 1_800


def test_chunk_timeline_preserves_restart_gap():
    started = datetime(2026, 8, 28, tzinfo=timezone.utc)
    session = SleepSession(
        id="session",
        device_id="phone",
        started_at_utc=started,
        started_at_monotonic_ns=1_000_000_000,
        sample_rate=16_000,
    )
    chunk = AudioChunk(
        session_id="session",
        sequence=1,
        filename="audio.wav",
        sample_offset=60 * 16_000,
        sample_count=60 * 16_000,
        started_at_utc=started + timedelta(seconds=120),
        started_at_monotonic_ns=121_000_000_000,
        sha256="0" * 64,
    )

    assert chunk_timeline_sample_offset(session, chunk) == 120 * 16_000
