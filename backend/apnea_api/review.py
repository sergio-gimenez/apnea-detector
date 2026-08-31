"""Blinded labelling batches for measuring the detector against a human listener.

Two design points carry the whole method:

* Controls are drawn from the *same snoring periods the detector searches*, not
  from anywhere in the night. A control taken from a silent hour would be trivially
  distinguishable by ear, blinding would collapse, and the recall estimate would be
  meaningless because the detector never looks there anyway.
* Control durations are resampled from the candidate durations, so clip length
  cannot betray which is which.

What the labels then buy: pauses heard in candidates give precision, pauses heard
in controls are events the detector missed, and together they bound recall.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

import numpy as np

CONTROL_SEPARATION_SECONDS = 30.0
DEFAULT_CONTROL_RATIO = 1.0
MAX_BATCH_ITEMS = 120


@dataclass
class PlannedItem:
    kind: str
    start_offset_seconds: float
    duration_seconds: float
    event_id: int | None = None


def _snoring_spans(mask: np.ndarray, epoch_seconds: float) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    index = 0
    while index < mask.size:
        if not mask[index]:
            index += 1
            continue
        end = index
        while end < mask.size and mask[end]:
            end += 1
        spans.append((index * epoch_seconds, end * epoch_seconds))
        index = end
    return spans


def plan_controls(
    candidates: list[tuple[float, float]],
    snoring_spans: list[tuple[float, float]],
    count: int,
    seed: int | None = None,
) -> list[PlannedItem]:
    """Windows inside snoring periods that do not overlap any candidate."""
    if count <= 0 or not snoring_spans or not candidates:
        return []
    rng = random.Random(seed)
    durations = [duration for _, duration in candidates]
    blocked = [
        (start - CONTROL_SEPARATION_SECONDS, start + duration + CONTROL_SEPARATION_SECONDS)
        for start, duration in candidates
    ]
    weights = [max(0.0, end - start) for start, end in snoring_spans]
    if not sum(weights):
        return []

    controls: list[PlannedItem] = []
    for _ in range(count * 40):
        if len(controls) >= count:
            break
        span_start, span_end = rng.choices(snoring_spans, weights=weights, k=1)[0]
        duration = rng.choice(durations)
        if span_end - span_start <= duration:
            continue
        start = rng.uniform(span_start, span_end - duration)
        window = (start - CONTROL_SEPARATION_SECONDS, start + duration + CONTROL_SEPARATION_SECONDS)
        if any(not (window[1] < low or window[0] > high) for low, high in blocked):
            continue
        if any(
            not (window[1] < item.start_offset_seconds or window[0] > item.start_offset_seconds + item.duration_seconds)
            for item in controls
        ):
            continue
        controls.append(PlannedItem("control", round(start, 1), round(duration, 1)))
    return controls


def plan_batch(
    candidates: list[tuple[int, float, float]],
    snoring_mask: np.ndarray,
    epoch_seconds: float,
    control_ratio: float = DEFAULT_CONTROL_RATIO,
    seed: int | None = None,
) -> tuple[str, list[PlannedItem]]:
    """Shuffled mix of every candidate plus matched controls."""
    items = [
        PlannedItem("candidate", start, duration, event_id)
        for event_id, start, duration in candidates
    ]
    spans = _snoring_spans(snoring_mask, epoch_seconds) if snoring_mask is not None else []
    items += plan_controls(
        [(start, duration) for _, start, duration in candidates],
        spans,
        int(round(len(candidates) * control_ratio)),
        seed=seed,
    )
    rng = random.Random(seed)
    rng.shuffle(items)
    return uuid.uuid4().hex, items[:MAX_BATCH_ITEMS]


def score_batch(rows: list[tuple[str, str | None]]) -> dict:
    """Precision, control false-positive rate and a recall estimate from labels."""
    def counts(kind: str) -> dict[str, int]:
        subset = [label for item_kind, label in rows if item_kind == kind]
        return {
            "total": len(subset),
            "labeled": sum(1 for label in subset if label),
            "pause": sum(1 for label in subset if label == "pause"),
            "no_pause": sum(1 for label in subset if label == "no_pause"),
            "unclear": sum(1 for label in subset if label == "unclear"),
        }

    candidate = counts("candidate")
    control = counts("control")
    decided_candidates = candidate["pause"] + candidate["no_pause"]
    decided_controls = control["pause"] + control["no_pause"]

    precision = candidate["pause"] / decided_candidates if decided_candidates else None
    control_pause_rate = control["pause"] / decided_controls if decided_controls else None

    # Controls sample the snoring time the detector did not flag. If a fraction p of
    # that time still contains pauses, those are misses, and recall follows from the
    # ratio of true detections to true detections plus estimated misses.
    recall = None
    if precision is not None and control_pause_rate is not None and decided_controls:
        detected = candidate["pause"]
        missed = control_pause_rate * decided_controls
        if detected + missed > 0:
            recall = detected / (detected + missed)

    return {
        "candidates": candidate,
        "controls": control,
        "precision": round(precision, 3) if precision is not None else None,
        "control_pause_rate": round(control_pause_rate, 3)
        if control_pause_rate is not None
        else None,
        "recall_estimate": round(recall, 3) if recall is not None else None,
        "caveat": (
            "Precision is measured on this listener's judgement of the audio, not on "
            "polysomnography. The recall estimate scales the control pause rate to the "
            "unflagged snoring time it samples, so it is indicative only."
        ),
    }
