"""Relative-desaturation analysis for wearable pulse oximetry.

Garmin Pulse Ox is minute-level and noisy, so the rules here follow the shape of
AASM desaturation scoring (relative drop from a rolling pre-event baseline) while
staying honest about the sampling limits: a minute-level series cannot resolve a
10-second event, so desaturations are reported as an independent evidence stream
rather than as apnea/hypopnea scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

OXIMETRY_VERSION = "spo2-v0.1.0"

BASELINE_WINDOW_SECONDS = 300.0
MAX_SAMPLE_GAP_SECONDS = 300.0
MERGE_GAP_SECONDS = 30.0
ARTIFACT_JUMP = 15.0
ARTIFACT_WINDOW_SECONDS = 90.0
MIN_PLAUSIBLE_SPO2 = 60.0


@dataclass
class Desaturation:
    start: float
    duration: float
    baseline: float
    nadir: float

    @property
    def depth(self) -> float:
        return self.baseline - self.nadir

    @property
    def end(self) -> float:
        return self.start + self.duration

    def as_json(self) -> dict:
        return {
            "start_offset_seconds": round(self.start, 1),
            "duration_seconds": round(self.duration, 1),
            "baseline_spo2": round(self.baseline, 1),
            "nadir_spo2": round(self.nadir, 1),
            "depth_percent": round(self.depth, 1),
        }


@dataclass
class OximetryResult:
    coverage_seconds: float = 0.0
    samples: int = 0
    rejected_samples: int = 0
    desaturations_3: list[Desaturation] = field(default_factory=list)
    desaturations_4: list[Desaturation] = field(default_factory=list)
    minimum_spo2: float | None = None
    mean_spo2: float | None = None
    baseline_spo2: float | None = None
    seconds_below_90: float = 0.0
    seconds_below_88: float = 0.0
    desaturation_burden: float = 0.0

    @property
    def coverage_hours(self) -> float:
        return self.coverage_seconds / 3600.0

    def _per_hour(self, events: list[Desaturation]) -> float | None:
        hours = self.coverage_hours
        return round(len(events) / hours, 2) if hours >= 0.05 else None

    def as_json(self) -> dict:
        hours = self.coverage_hours
        return {
            "algorithm_version": OXIMETRY_VERSION,
            "coverage_hours": round(hours, 3),
            "samples": self.samples,
            "rejected_samples": self.rejected_samples,
            "odi3": self._per_hour(self.desaturations_3),
            "odi4": self._per_hour(self.desaturations_4),
            "desaturations_3": len(self.desaturations_3),
            "desaturations_4": len(self.desaturations_4),
            "minimum_spo2": self.minimum_spo2,
            "mean_spo2": round(self.mean_spo2, 2) if self.mean_spo2 is not None else None,
            "baseline_spo2": round(self.baseline_spo2, 1)
            if self.baseline_spo2 is not None
            else None,
            "t90_seconds": round(self.seconds_below_90, 1),
            "t88_seconds": round(self.seconds_below_88, 1),
            "t90_percent": round(100.0 * self.seconds_below_90 / self.coverage_seconds, 2)
            if self.coverage_seconds
            else None,
            "desaturation_burden_pct_min_per_hour": round(self.desaturation_burden, 2)
            if hours >= 0.05
            else None,
            "events": [event.as_json() for event in self.desaturations_3],
            "sampling_caveat": (
                "Wearable Pulse Ox is minute-level. These desaturations are supporting "
                "evidence, not scored respiratory events, and ODI is an estimate."
            ),
        }


def _reject_artifacts(times: np.ndarray, values: np.ndarray) -> np.ndarray:
    keep = values >= MIN_PLAUSIBLE_SPO2
    previous = None
    for index in range(len(values)):
        if not keep[index]:
            continue
        if previous is not None:
            gap = times[index] - times[previous]
            if (
                gap <= ARTIFACT_WINDOW_SECONDS
                and abs(values[index] - values[previous]) > ARTIFACT_JUMP
            ):
                keep[index] = False
                continue
        previous = index
    return keep


def _rolling_baseline(times: np.ndarray, values: np.ndarray) -> np.ndarray:
    baseline = np.empty(len(values), dtype=np.float64)
    fallback = float(np.median(values))
    for index in range(len(values)):
        window = values[
            (times >= times[index] - BASELINE_WINDOW_SECONDS) & (times < times[index])
        ]
        baseline[index] = float(np.median(window)) if window.size >= 2 else fallback
    return baseline


def _group(
    times: np.ndarray,
    values: np.ndarray,
    baseline: np.ndarray,
    intervals: np.ndarray,
    drop: float,
) -> list[Desaturation]:
    flagged = values <= baseline - drop
    events: list[Desaturation] = []
    index = 0
    while index < len(flagged):
        if not flagged[index]:
            index += 1
            continue
        end = index
        while end < len(flagged) and flagged[end]:
            end += 1
        start_time = float(times[index])
        end_time = float(times[end - 1] + intervals[end - 1])
        events.append(
            Desaturation(
                start=start_time,
                duration=max(end_time - start_time, float(intervals[index])),
                baseline=float(np.max(baseline[index:end])),
                nadir=float(np.min(values[index:end])),
            )
        )
        index = end

    merged: list[Desaturation] = []
    for event in events:
        if merged and event.start - merged[-1].end <= MERGE_GAP_SECONDS:
            previous = merged[-1]
            previous.duration = event.end - previous.start
            previous.baseline = max(previous.baseline, event.baseline)
            previous.nadir = min(previous.nadir, event.nadir)
        else:
            merged.append(event)
    return merged


def analyze_oximetry(points: list[tuple[float, float]]) -> OximetryResult:
    """`points` are (offset seconds from session start, SpO2 percent) pairs."""
    result = OximetryResult()
    if len(points) < 5:
        return result

    ordered = sorted(points)
    times = np.array([offset for offset, _ in ordered], dtype=np.float64)
    values = np.array([value for _, value in ordered], dtype=np.float64)

    keep = _reject_artifacts(times, values)
    result.rejected_samples = int(np.count_nonzero(~keep))
    times, values = times[keep], values[keep]
    result.samples = int(values.size)
    if values.size < 5:
        return result

    gaps = np.diff(times)
    intervals = np.append(gaps, np.median(gaps) if gaps.size else 60.0)
    intervals = np.clip(intervals, 0.0, MAX_SAMPLE_GAP_SECONDS)

    result.coverage_seconds = float(np.sum(intervals))
    result.minimum_spo2 = float(np.min(values))
    result.mean_spo2 = float(np.mean(values))
    result.seconds_below_90 = float(np.sum(intervals[values < 90.0]))
    result.seconds_below_88 = float(np.sum(intervals[values < 88.0]))

    baseline = _rolling_baseline(times, values)
    result.baseline_spo2 = float(np.median(baseline))
    result.desaturations_3 = _group(times, values, baseline, intervals, 3.0)
    result.desaturations_4 = _group(times, values, baseline, intervals, 4.0)

    deficit = np.maximum(baseline - values, 0.0)
    burden_percent_seconds = float(np.sum(deficit * intervals))
    if result.coverage_hours >= 0.05:
        result.desaturation_burden = burden_percent_seconds / 60.0 / result.coverage_hours
    return result


def match_desaturation(
    events: list[Desaturation], start: float, end: float, lag_seconds: float = 60.0
) -> Desaturation | None:
    """Nearest desaturation whose nadir plausibly follows an audio candidate."""
    window_start = start - 30.0
    window_end = end + lag_seconds
    overlapping = [
        event for event in events if event.end >= window_start and event.start <= window_end
    ]
    return max(overlapping, key=lambda event: event.depth) if overlapping else None
