"""Snore-anchored respiratory event detection.

Version 0.1 looked for quiet stretches, which in a calm bedroom mostly finds
"no noise happened" rather than "no breathing happened": on a real night it
flagged a quarter of the recording, and breathing was still plainly audible
inside its highest-confidence candidates.

For someone who snores, the high-contrast event is the opposite shape - loud
rhythmic snoring that STOPS while the sleeper is obviously still there, then
resumes, often with a louder recovery snort:

    snore snore snore ......... gap ......... GASP snore snore
                                (airway closed)

So this module detects individual snore bursts, then treats a gap between them
as a candidate only when snoring was established before it and resumes after.
Everything runs on a band-limited 20 Hz loudness envelope; the band starts at
250 Hz because measurements of the real recordings showed a room rumble adding +16 dB
below 60 Hz and about +5 dB up to 125 Hz, while contributing nothing above 250 Hz.
150 Hz keeps the snore fundamental (measured 4.5 dB louder peaks than a 250 Hz
cutoff) without letting that rumble back in.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SNORE_VERSION = "dsp-v0.2.0"

ENVELOPE_HZ = 20
BAND_LOW_HZ = 150.0
BAND_HIGH_HZ = 1500.0

BURST_THRESHOLD_DB = 12.0
# A burst also has to be loud in absolute terms. Without this, a faint rustle in a
# very quiet stretch clears the relative threshold and is counted as a snore, which
# then lets the gap detector invent pauses between sounds that were never snoring.
SNORE_REFERENCE_PERCENTILE = 75
SNORE_RELATIVE_FLOOR_DB = 12.0
BURST_MIN_SECONDS = 0.3
BURST_MAX_SECONDS = 3.0
BURST_MERGE_SECONDS = 0.15

FLOOR_BLOCK_SECONDS = 5.0
FLOOR_NEIGHBOURHOOD_BLOCKS = 6
FLOOR_PERCENTILE = 10

EPOCH_SECONDS = 30.0
EPOCH_MIN_BURSTS = 6

GAP_MIN_SECONDS = 10.0
GAP_MAX_SECONDS = 120.0
GAP_QUIET_MARGIN_DB = 8.0
GAP_MARGIN_SECONDS = 0.5
PRE_CONTEXT_SECONDS = 60.0
PRE_CONTEXT_MIN_BURSTS = 3
POST_CONTEXT_SECONDS = 30.0
POST_CONTEXT_MIN_BURSTS = 2
RECOVERY_GASP_DB = 3.0


@dataclass
class SnoreBurst:
    start: float
    duration: float
    peak_dbfs: float
    prominence_db: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def band_envelope(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Band-limited RMS loudness envelope sampled at ENVELOPE_HZ."""
    if samples.size == 0:
        return np.zeros(0, dtype=np.float64)
    signal = samples.astype(np.float64) / 32768.0
    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(signal.size, 1.0 / sample_rate)
    spectrum[(freqs < BAND_LOW_HZ) | (freqs > BAND_HIGH_HZ)] = 0.0
    band = np.fft.irfft(spectrum, n=signal.size)
    hop = max(1, sample_rate // ENVELOPE_HZ)
    usable = (band.size // hop) * hop
    if usable == 0:
        return np.zeros(0, dtype=np.float64)
    frames = band[:usable].reshape(-1, hop)
    return np.sqrt((frames**2).mean(axis=1))


def to_dbfs(envelope: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(envelope, 1e-12))


def rolling_floor(envelope_db: np.ndarray) -> np.ndarray:
    """Local quiet level, robust to both bursts and slow room-noise drift."""
    if envelope_db.size == 0:
        return envelope_db
    block = int(FLOOR_BLOCK_SECONDS * ENVELOPE_HZ)
    blocks = max(1, envelope_db.size // block)
    usable = blocks * block
    per_block = np.percentile(
        envelope_db[:usable].reshape(blocks, block), FLOOR_PERCENTILE, axis=1
    )
    smoothed = np.empty(blocks, dtype=np.float64)
    for index in range(blocks):
        low = max(0, index - FLOOR_NEIGHBOURHOOD_BLOCKS)
        high = min(blocks, index + FLOOR_NEIGHBOURHOOD_BLOCKS + 1)
        smoothed[index] = np.median(per_block[low:high])
    floor = np.repeat(smoothed, block)
    if floor.size < envelope_db.size:
        floor = np.concatenate([floor, np.full(envelope_db.size - floor.size, smoothed[-1])])
    return floor


def detect_bursts(envelope_db: np.ndarray, floor_db: np.ndarray) -> list[SnoreBurst]:
    """Short loud events above the local floor - individual snores."""
    loud = envelope_db > floor_db + BURST_THRESHOLD_DB
    bursts: list[SnoreBurst] = []
    index = 0
    merge_frames = int(BURST_MERGE_SECONDS * ENVELOPE_HZ)
    while index < loud.size:
        if not loud[index]:
            index += 1
            continue
        end = index
        while end < loud.size:
            if loud[end]:
                end += 1
                continue
            lookahead = loud[end : end + merge_frames]
            if lookahead.size and lookahead.any():
                end += 1
                continue
            break
        frames = end - index
        seconds = frames / ENVELOPE_HZ
        if BURST_MIN_SECONDS <= seconds <= BURST_MAX_SECONDS:
            run = envelope_db[index:end]
            bursts.append(
                SnoreBurst(
                    start=index / ENVELOPE_HZ,
                    duration=seconds,
                    peak_dbfs=float(run.max()),
                    prominence_db=float(run.max() - floor_db[index:end].mean()),
                )
            )
        index = end
    return bursts


def keep_loud_bursts(bursts: list[SnoreBurst]) -> list[SnoreBurst]:
    """Drop bursts far quieter than the night's actual snoring."""
    if len(bursts) < 10:
        return bursts
    peaks = np.array([burst.peak_dbfs for burst in bursts])
    reference = float(np.percentile(peaks, SNORE_REFERENCE_PERCENTILE))
    return [
        burst for burst in bursts if burst.peak_dbfs >= reference - SNORE_RELATIVE_FLOOR_DB
    ]


def snoring_epochs(bursts: list[SnoreBurst], total_seconds: float) -> np.ndarray:
    """Boolean mask of 30 s epochs carrying a snore-like burst rate."""
    epochs = max(1, int(np.ceil(total_seconds / EPOCH_SECONDS)))
    counts = np.zeros(epochs, dtype=np.int64)
    for burst in bursts:
        index = int(burst.start // EPOCH_SECONDS)
        if 0 <= index < epochs:
            counts[index] += 1
    return counts >= EPOCH_MIN_BURSTS


def snoring_burden(mask: np.ndarray) -> float:
    return float(100.0 * mask.mean()) if mask.size else 0.0


@dataclass
class SnoreGap:
    start: float
    duration: float
    confidence: float
    evidence: dict


def _bursts_between(bursts: list[SnoreBurst], start: float, end: float) -> list[SnoreBurst]:
    return [burst for burst in bursts if start <= burst.start < end]


def detect_snore_gaps(
    envelope_db: np.ndarray, floor_db: np.ndarray, bursts: list[SnoreBurst]
) -> list[SnoreGap]:
    """Silences that interrupt established snoring and are followed by its return."""
    gaps: list[SnoreGap] = []
    for previous, following in zip(bursts, bursts[1:]):
        gap_start = previous.end
        gap_duration = following.start - gap_start
        if not GAP_MIN_SECONDS <= gap_duration <= GAP_MAX_SECONDS:
            continue

        before = _bursts_between(bursts, gap_start - PRE_CONTEXT_SECONDS, gap_start)
        if len(before) < PRE_CONTEXT_MIN_BURSTS:
            continue
        after = _bursts_between(
            bursts, following.start, following.start + POST_CONTEXT_SECONDS
        )
        if len(after) + 1 < POST_CONTEXT_MIN_BURSTS:
            continue

        low = int((gap_start + GAP_MARGIN_SECONDS) * ENVELOPE_HZ)
        high = int((following.start - GAP_MARGIN_SECONDS) * ENVELOPE_HZ)
        if high <= low:
            continue
        window = envelope_db[low:high]
        window_floor = float(np.median(floor_db[low:high]))
        # A real pause stays near the room floor; movement or speech does not.
        if float(np.percentile(window, 90)) > window_floor + GAP_QUIET_MARGIN_DB:
            continue

        pre_peak = float(np.median([burst.peak_dbfs for burst in before]))
        recovery_gasp = bool(following.peak_dbfs >= pre_peak + RECOVERY_GASP_DB)
        quiet_depth = pre_peak - float(np.median(window))

        confidence = 0.2
        confidence += min(0.25, (gap_duration - GAP_MIN_SECONDS) * 0.015)
        confidence += min(0.2, max(0.0, quiet_depth / 40.0))
        confidence += 0.15 if recovery_gasp else 0.0
        confidence += min(0.1, len(before) * 0.02)

        gaps.append(
            SnoreGap(
                start=gap_start,
                duration=gap_duration,
                confidence=confidence,
                evidence={
                    "snoring_before": True,
                    "snore_bursts_before": len(before),
                    "pause_gt_10s": True,
                    "recovery_gasp": recovery_gasp,
                    "reduced_respiratory_audio": True,
                    "snore_level_before_dbfs": round(pre_peak, 2),
                    "pause_level_dbfs": round(float(np.median(window)), 2),
                    "room_floor_dbfs": round(window_floor, 2),
                    "recovery_peak_dbfs": round(following.peak_dbfs, 2),
                },
            )
        )
    return gaps
