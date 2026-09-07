from datetime import datetime, timezone

from apnea_api.garmin import MAX_SESSION_SPAN, normalize_payload, session_span_seconds
from apnea_api.models import SleepSession


def test_normalizes_known_garmin_point_shapes():
    payload = {
        "heartRateValues": [[1_700_000_000_000, 58], [1_700_000_060_000, None]],
        "respirationValuesArray": [[1_700_000_000_000, 14.2]],
        "wellnessSpO2DataDTOList": [
            {"readingStartTimeGMT": "2023-11-14T22:13:20Z", "spo2Reading": 96}
        ],
        "wellnessEpochSPO2DataDTOList": [
            {"epochTimestamp": 1_700_000_120_000, "spo2Value": 95},
            {"epochTimestamp": 1_700_000_180_000, "spo2Value": -1},
        ],
    }

    points = normalize_payload(payload)

    assert {(signal, value) for _, signal, value in points} == {
        ("heart_rate", 58.0),
        ("respiration_rate", 14.2),
        ("spo2", 96.0),
        ("spo2", 95.0),
    }
    assert all(timestamp.tzinfo == timezone.utc for timestamp, _, _ in points)


def _session(**overrides):
    session = SleepSession(
        id="s1",
        device_id="Pixel",
        status=overrides.pop("status", "complete"),
        started_at_utc=datetime(2026, 9, 3, 21, 0, tzinfo=timezone.utc),
        started_at_monotonic_ns=0,
        sample_rate=16_000,
        total_samples=overrides.pop("total_samples", 0),
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def test_span_prefers_completed_at_over_a_short_sample_count():
    # chunks stopped landing after 40 min but the night ran 8 h
    session = _session(
        total_samples=16_000 * 2_400,
        completed_at=datetime(2026, 9, 4, 5, 0, tzinfo=timezone.utc),
    )

    assert session_span_seconds(session) == 8 * 3600


def test_span_of_a_still_recording_session_reaches_now():
    session = _session(status="recording", total_samples=16_000 * 2_100, completed_at=None)
    now = datetime(2026, 9, 4, 5, 0, tzinfo=timezone.utc)

    assert session_span_seconds(session, now=now) == 8 * 3600


def test_span_is_capped_so_a_stale_session_cannot_ask_for_days():
    session = _session(status="recording", completed_at=None)
    now = datetime(2026, 9, 10, 21, 0, tzinfo=timezone.utc)

    assert session_span_seconds(session, now=now) == MAX_SESSION_SPAN.total_seconds()


def test_span_falls_back_to_samples_when_the_night_closed_early():
    session = _session(
        total_samples=16_000 * 8 * 3600,
        completed_at=datetime(2026, 9, 3, 21, 30, tzinfo=timezone.utc),
    )

    assert session_span_seconds(session) == 8 * 3600
