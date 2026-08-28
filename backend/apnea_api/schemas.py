from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SessionCreate(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    device_id: str = Field(min_length=1, max_length=200)
    started_at_utc: datetime
    started_at_monotonic_ns: int = Field(ge=0)
    sample_rate: int = Field(default=16_000, ge=8_000, le=48_000)


class SignalIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    timestamp_utc: datetime
    signal_type: Literal[
        "spo2",
        "heart_rate",
        "respiration_rate",
        "sleep_stage",
        "movement",
    ]
    value: float
    unit: str = Field(min_length=1, max_length=30)
    source: str = Field(default="manual", min_length=1, max_length=80)
    device: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_signal(self):
        constraints = {
            "spo2": (50, 100, "%"),
            "heart_rate": (20, 250, "bpm"),
            "respiration_rate": (1, 80, "breaths/min"),
            "sleep_stage": (0, 4, "stage"),
            "movement": (0, 100, "relative"),
        }
        minimum, maximum, unit = constraints[self.signal_type]
        if not minimum <= self.value <= maximum:
            raise ValueError(f"{self.signal_type} value outside plausible prototype range")
        if self.unit != unit:
            raise ValueError(f"{self.signal_type} unit must be {unit}")
        return self


class SignalBatch(BaseModel):
    points: list[SignalIn] = Field(max_length=100_000)


class ReviewUpdate(BaseModel):
    status: Literal["unreviewed", "confirmed", "rejected", "uncertain"]


class GarminImportRequest(BaseModel):
    date: str | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is not None:
            datetime.strptime(value, "%Y-%m-%d")
        return value
