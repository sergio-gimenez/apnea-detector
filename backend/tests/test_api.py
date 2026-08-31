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


def test_session_json_reports_chunk_count(tmp_path):
    client = TestClient(create_app(tmp_path, f"sqlite:///{tmp_path / 'chunks.db'}"))
    session_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
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
    assert client.get(f"/api/sessions/{session_id}").json()["chunk_count"] == 0
    for sequence in range(2):
        client.post(
            f"/api/sessions/{session_id}/audio-chunks",
            data={
                "sequence": str(sequence),
                "sample_offset": str(16_000 * 60 * sequence),
                "sample_count": str(16_000 * 60),
                "started_at_utc": (started + timedelta(seconds=60 * sequence)).isoformat(),
                "started_at_monotonic_ns": str(sequence * 60_000_000_000),
            },
            files={"file": (f"audio_{sequence:05d}.wav", wav_bytes(16_000 * 60), "audio/wav")},
        )
    remote = client.get(f"/api/sessions/{session_id}").json()
    assert remote["chunk_count"] == 2
    assert remote["total_samples"] == 16_000 * 120


def _session_with_audio(client, minutes=10):
    session_id = str(uuid.uuid4())
    started = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)
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
    for sequence in range(minutes // 5):
        offset = sequence * 300
        client.post(
            f"/api/sessions/{session_id}/audio-chunks",
            data={
                "sequence": str(sequence),
                "sample_offset": str(16_000 * offset),
                "sample_count": str(16_000 * 300),
                "started_at_utc": (started + timedelta(seconds=offset)).isoformat(),
                "started_at_monotonic_ns": str(offset * 1_000_000_000),
            },
            files={"file": (f"a_{sequence:05d}.wav", wav_bytes(16_000 * 300), "audio/wav")},
        )
    return session_id, started


def test_review_batch_is_blinded_until_labelled(tmp_path):
    client = TestClient(create_app(tmp_path, f"sqlite:///{tmp_path / 'review.db'}"))
    session_id, _ = _session_with_audio(client)
    client.post(f"/api/sessions/{session_id}/analyze")
    # silent synthetic audio yields no candidates, so a batch cannot be built
    assert client.post(f"/api/sessions/{session_id}/review-batch").status_code == 422


def test_review_labelling_reveals_kind_and_scores(tmp_path):
    from apnea_api.main import create_app as _create
    from apnea_api.models import RespiratoryEvent, SignalPoint, SleepSession

    app = _create(tmp_path, f"sqlite:///{tmp_path / 'review2.db'}")
    client = TestClient(app)
    session_id, started = _session_with_audio(client)

    # plant candidates and a snoring mask directly, independent of the detector
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'review2.db'}")
    with sessionmaker(engine)() as db:
        for index in range(6):
            db.add(
                RespiratoryEvent(
                    session_id=session_id,
                    start_offset_seconds=60.0 + index * 60,
                    duration_seconds=20.0,
                    confidence=0.8,
                    evidence_json="{}",
                    algorithm_version="dsp-v0.2.0",
                )
            )
        for epoch in range(0, 20):
            db.add(
                SignalPoint(
                    session_id=session_id,
                    timestamp_utc=started + timedelta(seconds=epoch * 30),
                    signal_type="snore_rate",
                    value=20.0,
                    unit="bursts/min",
                    source="dsp-v0.2.0",
                )
            )
        db.query(SleepSession).filter_by(id=session_id).update({"total_samples": 16_000 * 600})
        db.commit()

    created = client.post(
        f"/api/sessions/{session_id}/review-batch", json={"control_ratio": 1.0, "seed": 4}
    ).json()
    assert created["candidate"] == 6
    assert created["control"] > 0

    batch = client.get(f"/api/sessions/{session_id}/review-batch").json()
    assert len(batch) == created["items"]
    assert all("kind" not in item for item in batch), "unlabelled items must stay blinded"

    revealed = client.patch(
        f"/api/review-items/{batch[0]['id']}", json={"label": "pause"}
    ).json()
    assert revealed["kind"] in {"candidate", "control"}
    assert revealed["label"] == "pause"

    after = client.get(f"/api/sessions/{session_id}/review-batch").json()
    assert "kind" in after[0], "a labelled item is no longer blinded"

    stats = client.get(f"/api/sessions/{session_id}/review-stats").json()
    assert stats["candidates"]["total"] == 6
    assert stats["controls"]["total"] == created["control"]

    wave_data = client.get(f"/api/review-items/{batch[0]['id']}/waveform").json()
    assert wave_data["sample_rate_hz"] == 20
    assert len(wave_data["envelope_dbfs"]) > 100
    assert len(wave_data["floor_dbfs"]) == len(wave_data["envelope_dbfs"])
    assert client.get(f"/api/review-items/{batch[0]['id']}/audio.wav").status_code == 200
