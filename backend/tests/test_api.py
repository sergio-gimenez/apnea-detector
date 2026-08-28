import io
import uuid
import wave
from datetime import datetime, timezone

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
