"""Unit tests for quickml_scorer.py. All HTTP calls are mocked via a fake post_fn — no real
network access, per this task's scope (no live QuickML endpoint exists yet to test against)."""

import csv
import sys

import pytest

from quickml_scorer import (
    build_headers,
    build_request_payload,
    call_prediction_endpoint,
    extract_prediction_value,
    main,
    score_hotspot_rows,
    score_risk_rows,
)


class FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


class FakePoster:
    """Records every call; returns responses from a queue (one per call) or a fixed one."""

    def __init__(self, responses=None, fixed_response=None):
        self.calls = []
        self._responses = list(responses) if responses is not None else None
        self._fixed_response = fixed_response

    def __call__(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self._responses is not None:
            return self._responses.pop(0)
        return self._fixed_response


def test_build_headers_shape():
    headers = build_headers("EPKEY123", "TOKEN456", "ORG789", "Development")
    assert headers == {
        "X-QUICKML-ENDPOINT-KEY": "EPKEY123",
        "Authorization": "Zoho-oauthtoken TOKEN456",
        "CATALYST-ORG": "ORG789",
        "Environment": "Development",
    }


def test_build_request_payload_wraps_row_in_record():
    row = {"offense_count": "3", "district_spread": "2"}
    assert build_request_payload(row) == {"record": row}


def test_extract_prediction_value_from_predictions_list():
    response = {"predictions": [{"risk_score": 72.5}]}
    assert extract_prediction_value(response, ("risk_score", "prediction")) == 72.5


def test_extract_prediction_value_from_top_level_fallback():
    response = {"prediction": 41}
    assert extract_prediction_value(response, ("risk_score", "prediction")) == 41


def test_extract_prediction_value_raises_with_candidates_and_keys_in_message():
    response = {"unexpected_field": 1}
    with pytest.raises(ValueError, match="risk_score"):
        extract_prediction_value(response, ("risk_score", "prediction"))


def test_call_prediction_endpoint_posts_expected_url_headers_body():
    poster = FakePoster(fixed_response=FakeResponse({"prediction": 10}))
    headers = build_headers("K", "T", "O", "Development")
    row = {"a": "1"}

    result = call_prediction_endpoint("https://endpoint.example/score", headers, row, poster)

    assert result == {"prediction": 10}
    assert poster.calls == [
        {
            "url": "https://endpoint.example/score",
            "headers": headers,
            "json": {"record": row},
        }
    ]


def test_call_prediction_endpoint_raises_on_http_error():
    poster = FakePoster(fixed_response=FakeResponse({}, status_code=500))
    headers = build_headers("K", "T", "O", "Development")

    with pytest.raises(RuntimeError, match="HTTP 500"):
        call_prediction_endpoint("https://endpoint.example/score", headers, {"a": "1"}, poster)


def test_score_risk_rows_builds_risk_scores_rows():
    feature_rows = [
        {"accused_id": "ACC-1", "offense_count": "3"},
        {"accused_id": "ACC-2", "offense_count": "1"},
    ]
    poster = FakePoster(
        responses=[
            FakeResponse({"predictions": [{"risk_score": 78.0, "feature_importance": {"offense_count": 0.4}}]}),
            FakeResponse({"predictions": [{"risk_score": 12.0, "feature_importance": {"offense_count": 0.2}}]}),
        ]
    )
    headers = build_headers("K", "T", "O", "Development")

    rows = score_risk_rows(feature_rows, "https://endpoint.example/score", headers, "RUN-001", poster)

    assert len(rows) == 2
    assert rows[0]["accused_id"] == "ACC-1"
    assert rows[0]["risk_score"] == 78.0
    assert rows[0]["score_id"] == "RISK-RUN-001-ACC-1"
    assert rows[0]["pipeline_run_id"] == "RUN-001"
    assert '"offense_count": 0.4' in rows[0]["feature_importance"]
    assert rows[1]["risk_score"] == 12.0


def test_score_hotspot_rows_normalizes_per_district():
    feature_rows = [
        {"district": "D1", "crime_type": "Theft", "year": "2024", "month": "1"},
        {"district": "D1", "crime_type": "Theft", "year": "2024", "month": "2"},
        {"district": "D1", "crime_type": "Theft", "year": "2024", "month": "3"},
    ]
    poster = FakePoster(
        responses=[
            FakeResponse({"predictions": [{"fir_count": 5}]}),
            FakeResponse({"predictions": [{"fir_count": 15}]}),
            FakeResponse({"predictions": [{"fir_count": 10}]}),
        ]
    )
    headers = build_headers("K", "T", "O", "Development")

    rows = score_hotspot_rows(feature_rows, "https://endpoint.example/score", headers, "RUN-002", poster)

    assert len(rows) == 3
    scores = {(r["forecast_window"]): r["hotspot_score"] for r in rows}
    assert scores["2024-01"] == 0.0    # min -> 0
    assert scores["2024-02"] == 1.0    # max -> 1
    assert scores["2024-03"] == 0.5    # midpoint
    assert rows[0]["district_id"] == "D1"
    assert rows[0]["forecast_id"] == "HOTSPOT-RUN-002-D1-Theft-2024-01"


def test_score_hotspot_rows_single_point_district_gets_zero_score():
    feature_rows = [{"district": "D9", "crime_type": "Theft", "year": "2024", "month": "6"}]
    poster = FakePoster(responses=[FakeResponse({"predictions": [{"fir_count": 7}]})])
    headers = build_headers("K", "T", "O", "Development")

    rows = score_hotspot_rows(feature_rows, "https://endpoint.example/score", headers, "RUN-003", poster)

    assert rows[0]["hotspot_score"] == 0.0


def test_main_end_to_end_risk_scores(tmp_path, monkeypatch):
    feature_csv = tmp_path / "accused_features.csv"
    with open(feature_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["accused_id", "offense_count"])
        writer.writeheader()
        writer.writerow({"accused_id": "ACC-1", "offense_count": "3"})

    output_csv = tmp_path / "risk_scores.csv"

    def fake_post(url, headers=None, json=None):
        return FakeResponse({"predictions": [{"risk_score": 55.5}]})

    monkeypatch.setattr("quickml_scorer.requests.post", fake_post)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quickml_scorer.py",
            "--target", "risk_scores",
            "--feature-csv", str(feature_csv),
            "--output-csv", str(output_csv),
            "--endpoint-url", "https://endpoint.example/score",
            "--access-token", "TOKEN",
            "--endpoint-key", "EPKEY",
            "--catalyst-org", "ORG",
            "--pipeline-run-id", "RUN-E2E",
        ],
    )

    main()

    with open(output_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["accused_id"] == "ACC-1"
    assert rows[0]["risk_score"] == "55.5"
    assert rows[0]["pipeline_run_id"] == "RUN-E2E"
