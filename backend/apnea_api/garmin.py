from __future__ import annotations

import getpass
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from garminconnect import Garmin
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import SignalPoint, SleepSession

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
    db.commit()
    return {"imported": imported, "counts": counts, "dates": dates, "warnings": warnings}


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
