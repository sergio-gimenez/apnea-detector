import numpy as np

from apnea_api.review import (
    CONTROL_SEPARATION_SECONDS,
    plan_batch,
    plan_controls,
    score_batch,
)

EPOCH = 30.0


def mask(*ranges, total=120):
    m = np.zeros(total, dtype=bool)
    for lo, hi in ranges:
        m[lo:hi] = True
    return m


def test_controls_land_inside_snoring_and_avoid_candidates():
    candidates = [(600.0, 20.0), (1200.0, 30.0)]
    spans = [(0.0, 3600.0)]
    controls = plan_controls(candidates, spans, count=20, seed=1)
    assert len(controls) == 20
    for item in controls:
        assert 0.0 <= item.start_offset_seconds <= 3600.0
        for start, duration in candidates:
            assert (
                item.start_offset_seconds + item.duration_seconds
                < start - CONTROL_SEPARATION_SECONDS
                or item.start_offset_seconds > start + duration + CONTROL_SEPARATION_SECONDS
            )


def test_control_durations_match_candidate_durations():
    candidates = [(600.0, 17.0), (1200.0, 41.0)]
    controls = plan_controls(candidates, [(0.0, 7200.0)], count=30, seed=3)
    assert set(item.duration_seconds for item in controls) <= {17.0, 41.0}


def test_controls_only_come_from_snoring_periods():
    # snoring only in the first 30 minutes of a two-hour night
    spans = [(0.0, 1800.0)]
    controls = plan_controls([(60.0, 20.0)], spans, count=15, seed=5)
    assert controls
    assert all(item.start_offset_seconds + item.duration_seconds <= 1800.0 for item in controls)


def test_batch_is_shuffled_and_blinds_nothing_by_order():
    candidates = [(index, 100.0 * index + 60, 20.0) for index in range(1, 16)]
    _, items = plan_batch(candidates, mask((0, 120)), EPOCH, seed=11)
    kinds = [item.kind for item in items]
    assert kinds.count("candidate") == 15
    assert kinds.count("control") == 15
    # a shuffled mix must not be all candidates first
    assert kinds[:15] != ["candidate"] * 15


def test_batch_without_snoring_has_no_controls():
    candidates = [(1, 60.0, 20.0)]
    _, items = plan_batch(candidates, np.zeros(120, dtype=bool), EPOCH, seed=2)
    assert [item.kind for item in items] == ["candidate"]


def test_scoring_precision_and_recall():
    rows = (
        [("candidate", "pause")] * 8
        + [("candidate", "no_pause")] * 2
        + [("control", "no_pause")] * 9
        + [("control", "pause")] * 1
    )
    result = score_batch(rows)
    assert result["precision"] == 0.8
    assert result["control_pause_rate"] == 0.1
    # 8 detected, 1.0 estimated missed
    assert result["recall_estimate"] == round(8 / 9, 3)


def test_scoring_handles_unlabelled_batch():
    result = score_batch([("candidate", None), ("control", None)])
    assert result["precision"] is None
    assert result["recall_estimate"] is None
    assert result["candidates"]["labeled"] == 0


def test_unclear_labels_are_excluded_from_rates():
    rows = [("candidate", "pause"), ("candidate", "unclear"), ("candidate", "no_pause")]
    result = score_batch(rows)
    assert result["precision"] == 0.5
    assert result["candidates"]["unclear"] == 1
