"""Tests for pragmatic F2 hotspot_forecasts seed (no live QuickML)."""

from __future__ import annotations

from seed_hotspot_forecasts import seed_rows_from_features


def test_seed_normalizes_per_district_latest_month():
    features = [
        {
            "district": "D1",
            "crime_type": "Theft",
            "year": 2025,
            "month": 12,
            "fir_count": 10,
        },
        {
            "district": "D1",
            "crime_type": "Fraud / cheating",
            "year": 2025,
            "month": 12,
            "fir_count": 1,
        },
        {
            "district": "D1",
            "crime_type": "Theft",
            "year": 2025,
            "month": 11,
            "fir_count": 99,
        },
        {
            "district": "D2",
            "crime_type": "Theft",
            "year": 2025,
            "month": 12,
            "fir_count": 5,
        },
    ]
    rows = seed_rows_from_features(features, "RUN-TEST", latest_month_only=True)
    assert len(rows) == 3
    by_key = {(r["district_id"], r["crime_type"]): r for r in rows}
    assert by_key[("D1", "Theft")]["hotspot_score"] == 1.0
    assert by_key[("D1", "Fraud / cheating")]["hotspot_score"] == 0.0
    assert by_key[("D2", "Theft")]["hotspot_score"] == 0.0  # single point in district
    assert by_key[("D1", "Theft")]["forecast_window"] == "2025-12"
    assert "HOTSPOT-RUN-TEST-D1-Theft-2025-12" == by_key[("D1", "Theft")]["forecast_id"]
