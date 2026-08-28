from datetime import timezone

from apnea_api.garmin import normalize_payload


def test_normalizes_known_garmin_point_shapes():
    payload = {
        "heartRateValues": [[1_700_000_000_000, 58], [1_700_000_060_000, None]],
        "respirationValuesArray": [[1_700_000_000_000, 14.2]],
        "wellnessSpO2DataDTOList": [
            {"readingStartTimeGMT": "2023-11-14T22:13:20Z", "spo2Reading": 96}
        ],
        "wellnessEpochSPO2DataDTOList": [
            {"epochTimestamp": 1_700_000_120_000, "spo2Value": 95},
            {"epochTimestamp": 1_700_000_180_000, "spo2Value": -1},
        ],
    }

    points = normalize_payload(payload)

    assert {(signal, value) for _, signal, value in points} == {
        ("heart_rate", 58.0),
        ("respiration_rate", 14.2),
        ("spo2", 96.0),
        ("spo2", 95.0),
    }
    assert all(timestamp.tzinfo == timezone.utc for timestamp, _, _ in points)
