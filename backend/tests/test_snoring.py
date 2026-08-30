import numpy as np

from apnea_api.snoring import (
    ENVELOPE_HZ,
    band_envelope,
    detect_bursts,
    detect_snore_gaps,
    rolling_floor,
    snoring_burden,
    snoring_epochs,
    to_dbfs,
)

RATE = 16_000


def synth_night(
    seconds: int = 600,
    breath_period: float = 4.0,
    gaps: tuple[tuple[float, float], ...] = (),
    snore_amplitude: float = 0.05,
    noise: float = 0.0005,
    rumble: float = 0.0,
) -> np.ndarray:
    """Snoring at a fixed rate, silent during `gaps`, plus room noise."""
    time = np.arange(seconds * RATE) / RATE
    audio = np.random.default_rng(7).normal(0, noise, time.size)
    if rumble:
        audio += rumble * np.sin(2 * np.pi * 45 * time)
    for onset in np.arange(1.0, seconds, breath_period):
        if any(start <= onset < start + length for start, length in gaps):
            continue
        low = int(onset * RATE)
        high = min(time.size, low + int(0.8 * RATE))
        span = np.arange(high - low) / RATE
        # a snore: low harmonic buzz under an envelope
        burst = np.hanning(span.size) * (
            np.sin(2 * np.pi * 320 * span) + 0.6 * np.sin(2 * np.pi * 640 * span)
        )
        audio[low:high] += snore_amplitude * burst
    return np.clip(audio * 32768, -32768, 32767).astype("<i2")


def analyse(samples):
    envelope = to_dbfs(band_envelope(samples, RATE))
    floor = rolling_floor(envelope)
    bursts = detect_bursts(envelope, floor)
    return envelope, floor, bursts


def test_detects_regular_snoring():
    _, _, bursts = analyse(synth_night(300))
    assert len(bursts) > 60
    intervals = np.diff([burst.start for burst in bursts])
    assert 3.5 < float(np.median(intervals)) < 4.5


def test_quiet_breathing_is_not_reported_as_snoring():
    _, _, bursts = analyse(synth_night(300, snore_amplitude=0.0))
    mask = snoring_epochs(bursts, 300)
    assert snoring_burden(mask) == 0.0


def acoustic_gap(start, length, seconds=600, period=4.0, snore=0.8):
    """The audible silence is wider than the suppression window: it runs from the
    end of the last snore before it to the start of the first snore after it."""
    onsets = np.arange(1.0, seconds, period)
    before = onsets[onsets < start].max()
    after = onsets[onsets >= start + length].min()
    return before + snore, after - (before + snore)


def test_finds_the_planted_pauses():
    planted = ((200.0, 22.0), (400.0, 35.0))
    samples = synth_night(600, gaps=planted)
    envelope, floor, bursts = analyse(samples)
    events = sorted(detect_snore_gaps(envelope, floor, bursts), key=lambda e: e.start)
    assert len(events) == 2
    for event, (start, length) in zip(events, planted):
        expected_start, expected_duration = acoustic_gap(start, length)
        assert abs(event.start - expected_start) < 1.5
        assert abs(event.duration - expected_duration) < 1.5
        assert event.duration >= length


def test_uninterrupted_snoring_yields_no_events():
    envelope, floor, bursts = analyse(synth_night(600))
    assert detect_snore_gaps(envelope, floor, bursts) == []


def test_pause_without_snoring_before_it_is_ignored():
    # silence first, snoring only afterwards: no established snoring to interrupt
    samples = synth_night(400, gaps=((0.0, 200.0),))
    envelope, floor, bursts = analyse(samples)
    assert detect_snore_gaps(envelope, floor, bursts) == []


def test_room_rumble_does_not_break_detection():
    plain = analyse(synth_night(600, gaps=((300.0, 25.0),)))
    noisy = analyse(synth_night(600, gaps=((300.0, 25.0),), rumble=0.05))
    plain_events = detect_snore_gaps(*plain)
    noisy_events = detect_snore_gaps(*noisy)
    assert len(noisy_events) == len(plain_events) == 1
    assert abs(noisy_events[0].start - plain_events[0].start) < 2


def test_recovery_gasp_is_flagged_and_raises_confidence():
    samples = synth_night(600, gaps=((300.0, 25.0),))
    # make the first snore after the pause much louder
    low = int(325.5 * RATE)
    samples[low : low + int(0.8 * RATE)] = np.clip(
        samples[low : low + int(0.8 * RATE)].astype(np.float64) * 4, -32768, 32767
    ).astype("<i2")
    envelope, floor, bursts = analyse(samples)
    events = detect_snore_gaps(envelope, floor, bursts)
    assert len(events) == 1
    assert events[0].evidence["recovery_gasp"] is True


def test_snoring_burden_tracks_time_spent_snoring():
    mask = snoring_epochs(analyse(synth_night(600, gaps=((0.0, 300.0),)))[2], 600)
    assert 40 < snoring_burden(mask) < 60


def test_envelope_sampling_rate():
    envelope = band_envelope(synth_night(10), RATE)
    assert abs(envelope.size - 10 * ENVELOPE_HZ) <= 1


def test_faint_noise_is_not_counted_as_snoring():
    from apnea_api.snoring import keep_loud_bursts

    samples = synth_night(600, gaps=((200.0, 200.0),))
    # a few very faint blips during the silent stretch
    for onset in (240.0, 260.0, 280.0, 300.0, 320.0, 340.0):
        low = int(onset * RATE)
        span = np.arange(int(0.8 * RATE)) / RATE
        samples[low : low + span.size] += (
            0.0015 * np.hanning(span.size) * np.sin(2 * np.pi * 320 * span) * 32768
        ).astype("<i2")
    envelope, floor, raw = analyse(samples)
    loud = keep_loud_bursts(raw)
    assert len(loud) < len(raw)
    faint = [b for b in loud if 230 < b.start < 350]
    assert not faint, "faint blips must not be treated as snoring"
