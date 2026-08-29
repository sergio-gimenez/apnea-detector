from apnea_api.oximetry import analyze_oximetry, match_desaturation


def _series(minutes: int, dip_every: int | None = None, dip_value: float = 91.0):
    points = []
    for minute in range(minutes):
        value = 96.0
        if dip_every and minute % dip_every == 0 and minute > 0:
            value = dip_value
        points.append((minute * 60.0, value))
    return points


def test_flat_series_has_no_desaturations():
    result = analyze_oximetry(_series(60)).as_json()
    assert result["desaturations_3"] == 0
    assert result["odi3"] == 0.0
    assert result["t90_seconds"] == 0.0
    assert result["minimum_spo2"] == 96.0


def test_periodic_dips_produce_odi():
    result = analyze_oximetry(_series(60, dip_every=3)).as_json()
    assert result["desaturations_3"] >= 15
    assert result["odi3"] > 10
    assert result["odi4"] <= result["odi3"]
    assert result["minimum_spo2"] == 91.0
    assert result["baseline_spo2"] == 96.0


def test_shallow_dips_score_odi3_but_not_odi4():
    result = analyze_oximetry(_series(60, dip_every=3, dip_value=93.0)).as_json()
    assert result["desaturations_3"] > 0
    assert result["desaturations_4"] == 0


def test_time_below_thresholds_and_burden():
    points = _series(60, dip_every=2, dip_value=87.0)
    result = analyze_oximetry(points).as_json()
    assert result["t90_seconds"] > 0
    assert result["t88_seconds"] > 0
    assert result["t90_seconds"] >= result["t88_seconds"]
    assert result["desaturation_burden_pct_min_per_hour"] > 0


def test_implausible_spike_is_rejected():
    points = _series(30)
    points[10] = (600.0, 62.0)
    result = analyze_oximetry(points).as_json()
    assert result["rejected_samples"] == 1
    assert result["minimum_spo2"] == 96.0
    assert result["desaturations_3"] == 0


def test_short_series_returns_empty_result():
    result = analyze_oximetry([(0.0, 96.0), (60.0, 95.0)]).as_json()
    assert result["samples"] == 0
    assert result["odi3"] is None
    assert result["events"] == []


def test_match_desaturation_picks_deepest_within_lag():
    events = analyze_oximetry(_series(30, dip_every=3)).desaturations_3
    assert events
    first = events[0]
    matched = match_desaturation(events, first.start - 20.0, first.start - 5.0)
    assert matched is first
    assert match_desaturation(events, 100000.0, 100020.0) is None
