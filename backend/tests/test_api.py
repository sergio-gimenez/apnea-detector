import io
import uuid
import wave
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from apnea_api.main import create_app


def wav_bytes(samples: int = 16_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * samples)
    return output.getvalue()


def test_session_chunk_upload_is_idempotent(tmp_path):
    client = TestClient(create_app(tmp_path, f"sqlite:///{tmp_path / 'test.db'}"))
    session_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()
    response = client.post(
        "/api/sessions",
        json={
            "id": session_id,
            "device_id": "test phone",
            "started_at_utc": started,
            "started_at_monotonic_ns": 123,
            "sample_rate": 16_000,
        },
    )
    assert response.status_code == 200
    form = {
        "sequence": "0",
        "sample_offset": "0",
        "sample_count": "16000",
        "started_at_utc": started,
        "started_at_monotonic_ns": "123",
    }
    files = {"file": ("audio_00000.wav", wav_bytes(), "audio/wav")}
    first = client.post(f"/api/sessions/{session_id}/audio-chunks", data=form, files=files)
    second = client.post(f"/api/sessions/{session_id}/audio-chunks", data=form, files=files)

    assert first.json()["status"] == "uploaded"
    assert second.json()["status"] == "already_uploaded"
    assert client.get(f"/api/sessions/{session_id}").json()["total_samples"] == 16_000


def test_bearer_token_protects_session_data(tmp_path, monkeypatch):
    monkeypatch.setenv("APNEA_API_TOKEN", "s" * 32)
    client = TestClient(create_app(tmp_path, f"sqlite:///{tmp_path / 'auth.db'}"))

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sessions").status_code == 401
    assert client.get(
        "/api/sessions", headers={"Authorization": f"Bearer {'s' * 32}"}
    ).status_code == 200


def test_summary_reports_odi_from_stored_spo2(tmp_path):
    client = TestClient(create_app(tmp_path, f"sqlite:///{tmp_path / 'odi.db'}"))
    session_id = str(uuid.uuid4())
    started = datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc)
    client.post(
        "/api/sessions",
        json={
            "id": session_id,
            "device_id": "test phone",
            "started_at_utc": started.isoformat(),
            "started_at_monotonic_ns": 0,
            "sample_rate": 16_000,
        },
    )
    chunk_seconds = 300
    chunks = 2
    for sequence in range(chunks):
        offset_seconds = sequence * chunk_seconds
        assert client.post(
            f"/api/sessions/{session_id}/audio-chunks",
            data={
                "sequence": str(sequence),
                "sample_offset": str(16_000 * offset_seconds),
                "sample_count": str(16_000 * chunk_seconds),
                "started_at_utc": (started + timedelta(seconds=offset_seconds)).isoformat(),
                "started_at_monotonic_ns": str(offset_seconds * 1_000_000_000),
            },
            files={
                "file": (
                    f"audio_{sequence:05d}.wav",
                    wav_bytes(16_000 * chunk_seconds),
                    "audio/wav",
                )
            },
        ).status_code == 200

    samples = chunk_seconds * chunks // 20
    points = [
        {
            "timestamp_utc": (started + timedelta(seconds=index * 20)).isoformat(),
            "signal_type": "spo2",
            "value": 91.0 if index and index % 3 == 0 else 96.0,
            "unit": "%",
            "source": "garmin",
        }
        for index in range(samples)
    ]
    assert client.post(
        f"/api/sessions/{session_id}/signals", json={"points": points}
    ).json()["imported"] == samples

    oximetry = client.get(f"/api/sessions/{session_id}/oximetry").json()
    assert oximetry["samples"] == samples
    assert oximetry["odi3"] > 10
    assert oximetry["minimum_spo2"] == 91.0
    assert oximetry["baseline_spo2"] == 96.0
    assert oximetry["events"]

    summary = client.get(f"/api/sessions/{session_id}/summary").json()
    assert summary["odi3"] == oximetry["odi3"]
    assert summary["minimum_spo2"] == 91.0
    assert summary["spo2_coverage_hours"] > 0.1
