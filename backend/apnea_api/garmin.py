from __future__ import annotations

import getpass
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from garminconnect import Garmin
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import SignalPoint, SleepArchitecture, SleepSession

UNITS = {"heart_rate": "bpm", "spo2": "%", "respiration_rate": "breaths/min"}


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _signal_for_path(path: str) -> str | None:
    lowered = path.lower()
    if "spo2" in lowered or "pulseox" in lowered:
        return "spo2"
    if "heartrate" in lowered or "heart_rate" in lowered:
        return "heart_rate"
    if "respiration" in lowered or "breath" in lowered:
        return "respiration_rate"
    return None


def _point_from_item(item: Any, signal_type: str) -> tuple[datetime, float] | None:
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        timestamp = _timestamp(item[0])
        value = item[1]
    elif isinstance(item, dict):
        timestamp = None
        for key in (
            "timestampGMT",
            "timestamp",
            "epochTimestamp",
            "readingStartTimeGMT",
            "startTimeGMT",
            "startGMT",
        ):
            if key in item:
                timestamp = _timestamp(item[key])
                if timestamp:
                    break
        value_keys = {
            "heart_rate": ("heartRate", "heartRateValue", "value"),
            "spo2": ("spo2Reading", "spo2Value", "readingValue", "spO2", "spo2", "value"),
            "respiration_rate": ("respirationValue", "respiration", "value"),
        }[signal_type]
        value = next((item[key] for key in value_keys if key in item), None)
    else:
        return None
    if timestamp is None or value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    minimum, maximum = {
        "heart_rate": (20, 250),
        "spo2": (50, 100),
        "respiration_rate": (1, 80),
    }[signal_type]
    if not minimum <= numeric <= maximum:
        return None
    return timestamp, numeric


def normalize_payload(payload: Any) -> list[tuple[datetime, str, float]]:
    points: list[tuple[datetime, str, float]] = []

    def walk(value: Any, path: str) -> None:
        signal_type = _signal_for_path(path)
        if isinstance(value, list):
            for item in value:
                point = _point_from_item(item, signal_type) if signal_type else None
                if point:
                    points.append((point[0], signal_type, point[1]))
                else:
                    walk(item, path)
        elif isinstance(value, dict):
            point = _point_from_item(value, signal_type) if signal_type else None
            if point:
                points.append((point[0], signal_type, point[1]))
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else key)

    walk(payload, "")
    return list(dict.fromkeys(points))


def _sleep_architecture(payload: Any) -> dict | None:
    """Pull the nightly stage breakdown out of a get_sleep_data payload."""
    if not isinstance(payload, dict):
        return None
    daily = payload.get("dailySleepDTO")
    if not isinstance(daily, dict) or not daily.get("calendarDate"):
        return None
    scores = daily.get("sleepScores") or {}
    overall = scores.get("overall") if isinstance(scores, dict) else None
    score = overall.get("value") if isinstance(overall, dict) else None

    def number(*keys):
        for source in (daily, payload):
            for key in keys:
                value = source.get(key) if isinstance(source, dict) else None
                if isinstance(value, (int, float)):
                    return value
        return None

    return {
        "calendar_date": str(daily["calendarDate"]),
        "sleep_score": int(score) if isinstance(score, (int, float)) else None,
        "deep_seconds": number("deepSleepSeconds"),
        "light_seconds": number("lightSleepSeconds"),
        "rem_seconds": number("remSleepSeconds"),
        "awake_seconds": number("awakeSleepSeconds"),
        "sleep_seconds": number("sleepTimeSeconds"),
        "awake_count": number("awakeCount"),
        "restless_moments": number("restlessMomentsCount"),
        "average_respiration": number("averageRespirationValue", "avgRespirationValue"),
        "lowest_respiration": number("lowestRespirationValue"),
        "average_stress": number("avgSleepStress", "averageSleepStress"),
    }


def _store_architecture(db: Session, session: SleepSession, payloads: list[Any]) -> dict | None:
    """Keep the stage breakdown whose date best matches the recorded night."""
    found = [record for record in (_sleep_architecture(p) for p in payloads) if record]
    if not found:
        return None
    target = _as_local_date(session)
    best = min(found, key=lambda record: abs_days(record["calendar_date"], target))
    existing = db.scalar(
        select(SleepArchitecture).where(SleepArchitecture.session_id == session.id)
    )
    if existing is None:
        existing = SleepArchitecture(session_id=session.id)
        db.add(existing)
    for key, value in best.items():
        if key in {"calendar_date"}:
            setattr(existing, key, value)
        elif value is not None:
            setattr(existing, key, int(value) if key.endswith(("_seconds", "_count", "_moments", "_score")) else float(value))
    existing.updated_at = datetime.now(timezone.utc)
    existing.source = "garmin-connect"
    return best


def _as_local_date(session: SleepSession) -> str:
    start = session.started_at_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    # a night starting before midnight is reported by Garmin under the next day
    return (start + timedelta(hours=6)).date().isoformat()


def abs_days(left: str, right: str) -> int:
    fmt = "%Y-%m-%d"
    return abs((datetime.strptime(left, fmt) - datetime.strptime(right, fmt)).days)


def import_for_session(
    db: Session,
    session: SleepSession,
    token_store: Path,
    requested_date: str | None = None,
) -> dict:
    client = Garmin()
    client.login(str(token_store))

    start = session.started_at_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    duration_seconds = session.total_samples / session.sample_rate if session.sample_rate else 0
    end = start + timedelta(seconds=duration_seconds) + timedelta(hours=3)
    if requested_date:
        dates = [requested_date]
    else:
        dates = sorted({start.date().isoformat(), end.date().isoformat()})

    payloads: list[Any] = []
    warnings: list[str] = []
    methods = (
        client.get_sleep_data,
        client.get_heart_rates,
        client.get_spo2_data,
        client.get_respiration_data,
    )
    for date in dates:
        for method in methods:
            try:
                payloads.append(method(date))
            except Exception as exc:  # Garmin has several endpoint-specific failure classes.
                warnings.append(f"{method.__name__}({date}): {exc}")

    normalized: list[tuple[datetime, str, float]] = []
    for payload in payloads:
        normalized.extend(normalize_payload(payload))
    normalized = list(dict.fromkeys(normalized))

    window_start = start - timedelta(hours=1)
    window_end = end
    staged = [
        point for point in normalized if window_start <= point[0] <= window_end
    ]
    if not staged:
        raise RuntimeError("Garmin returned no timestamped signals; existing import was preserved")
    existing_count = db.scalar(
        select(func.count()).select_from(SignalPoint).where(
            SignalPoint.session_id == session.id,
            SignalPoint.source == "garmin-connect",
        )
    )
    if warnings and existing_count:
        raise RuntimeError(
            "Garmin import was partial; existing signals were preserved. " + "; ".join(warnings)
        )
    signal_types = {point[1] for point in staged}
    db.execute(
        delete(SignalPoint).where(
            SignalPoint.session_id == session.id,
            SignalPoint.source == "garmin-connect",
            SignalPoint.signal_type.in_(signal_types),
        )
    )
    imported = 0
    counts: dict[str, int] = {}
    for timestamp, signal_type, value in staged:
        db.add(
            SignalPoint(
                session_id=session.id,
                timestamp_utc=timestamp,
                signal_type=signal_type,
                value=value,
                unit=UNITS[signal_type],
                source="garmin-connect",
                device="Garmin",
            )
        )
        imported += 1
        counts[signal_type] = counts.get(signal_type, 0) + 1
    architecture = _store_architecture(db, session, payloads)
    db.commit()
    return {
        "imported": imported,
        "counts": counts,
        "dates": dates,
        "warnings": warnings,
        "architecture": architecture,
    }


def login_cli() -> None:
    data_dir = Path(os.getenv("APNEA_DATA_DIR", "./data")).expanduser().resolve()
    token_store = Path(os.getenv("GARMIN_TOKEN_STORE", data_dir / "garmin"))
    token_store.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        client = Garmin()
        client.login(str(token_store))
        print(f"Existing Garmin token valid: {token_store}")
        return
    except Exception:
        pass

    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    client = Garmin(email=email, password=password, prompt_mfa=lambda: input("MFA code: ").strip())
    client.login(str(token_store))
    print(f"Garmin token stored in {token_store}")


if __name__ == "__main__":
    login_cli()
