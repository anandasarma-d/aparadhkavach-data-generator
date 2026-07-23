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
    load_scored_accused_ids,
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


def test_build_request_payload_wraps_row_in_data():
    row = {"accused_id": "ACC-1", "offense_count": "3", "district_spread": "2"}
    assert build_request_payload(row) == {"data": row}


def test_build_request_payload_strips_risk_label_keeps_accused_id():
    row = {
        "accused_id": "ACC-1",
        "offense_count": "3",
        "district_spread": "2",
        "risk_label": "55.0",
    }
    assert build_request_payload(row) == {
        "data": {
            "accused_id": "ACC-1",
            "offense_count": "3",
            "district_spread": "2",
        }
    }


def test_build_request_payload_empty_strings_become_null():
    row = {
        "accused_id": "ACC-00124",
        "offense_count": "1",
        "recidivism_interval_avg": "",
        "risk_label": "14.1",
    }
    assert build_request_payload(row) == {
        "data": {
            "accused_id": "ACC-00124",
            "offense_count": "1",
            "recidivism_interval_avg": None,
        }
    }


def test_extract_prediction_value_from_predictions_list():
    response = {"predictions": [{"risk_score": 72.5}]}
    assert extract_prediction_value(response, ("risk_score", "prediction")) == 72.5


def test_extract_prediction_value_from_top_level_fallback():
    response = {"prediction": 41}
    assert extract_prediction_value(response, ("risk_score", "prediction")) == 41


def test_extract_prediction_value_from_sdk_result_scalar_list():
    response = {"status": "success", "result": [62.5]}
    assert extract_prediction_value(response, ("risk_score", "prediction")) == 62.5


def test_extract_prediction_value_from_sdk_result_dict_list():
    response = {"status": "success", "result": [{"prediction": 18.0}]}
    assert extract_prediction_value(response, ("risk_score", "prediction")) == 18.0


def test_extract_prediction_value_raises_with_candidates_and_keys_in_message():
    response = {"unexpected_field": 1}
    with pytest.raises(ValueError, match="risk_score"):
        extract_prediction_value(response, ("risk_score", "prediction"))


def test_call_prediction_endpoint_posts_expected_url_headers_body():
    poster = FakePoster(fixed_response=FakeResponse({"prediction": 10}))
    headers = build_headers("K", "T", "O", "Development")
    row = {"accused_id": "ACC-1", "a": "1", "risk_label": "9.9"}

    result = call_prediction_endpoint("https://endpoint.example/score", headers, row, poster)

    assert result == {"prediction": 10}
    assert poster.calls == [
        {
            "url": "https://endpoint.example/score",
            "headers": headers,
            "json": {"data": {"accused_id": "ACC-1", "a": "1"}},
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


def test_score_risk_rows_writes_incrementally_and_logs_progress(tmp_path):
    feature_rows = [
        {"accused_id": "ACC-1", "offense_count": "3"},
        {"accused_id": "ACC-2", "offense_count": "1"},
        {"accused_id": "ACC-3", "offense_count": "2"},
    ]
    poster = FakePoster(
        responses=[
            FakeResponse({"result": [10.0]}),
            FakeResponse({"result": [20.0]}),
            FakeResponse({"result": [30.0]}),
        ]
    )
    headers = build_headers("K", "T", "O", "Development")
    output_csv = tmp_path / "risk_scores.csv"
    logs: list[str] = []

    rows = score_risk_rows(
        feature_rows,
        "https://endpoint.example/score",
        headers,
        "RUN-INC",
        poster,
        output_csv=output_csv,
        resume=False,
        progress_every=1,
        log=logs.append,
    )

    assert len(rows) == 3
    assert output_csv.is_file()
    with open(output_csv, newline="") as f:
        disk_rows = list(csv.DictReader(f))
    assert [r["accused_id"] for r in disk_rows] == ["ACC-1", "ACC-2", "ACC-3"]
    assert any("progress 1/3" in line for line in logs)
    assert any("progress 3/3" in line for line in logs)
    assert any(line.startswith("done:") for line in logs)


def test_score_risk_rows_resume_skips_already_scored(tmp_path):
    output_csv = tmp_path / "risk_scores.csv"
    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "score_id",
                "accused_id",
                "risk_score",
                "feature_importance",
                "pipeline_run_id",
                "scored_at",
                "created_at",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "score_id": "RISK-RUN-ACC-1",
                "accused_id": "ACC-1",
                "risk_score": "9.0",
                "feature_importance": "",
                "pipeline_run_id": "RUN",
                "scored_at": "2026-07-23 00:00:00",
                "created_at": "2026-07-23 00:00:00",
            }
        )

    assert load_scored_accused_ids(output_csv) == {"ACC-1"}

    feature_rows = [
        {"accused_id": "ACC-1", "offense_count": "3"},
        {"accused_id": "ACC-2", "offense_count": "1"},
    ]
    poster = FakePoster(responses=[FakeResponse({"result": [42.0]})])
    headers = build_headers("K", "T", "O", "Development")

    new_rows = score_risk_rows(
        feature_rows,
        "https://endpoint.example/score",
        headers,
        "RUN",
        poster,
        output_csv=output_csv,
        resume=True,
        progress_every=0,
        log=lambda _msg: None,
    )

    assert len(new_rows) == 1
    assert new_rows[0]["accused_id"] == "ACC-2"
    assert len(poster.calls) == 1
    with open(output_csv, newline="") as f:
        disk_rows = list(csv.DictReader(f))
    assert [r["accused_id"] for r in disk_rows] == ["ACC-1", "ACC-2"]
    assert disk_rows[1]["risk_score"] == "42.0"


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
            "--no-resume",
            "--progress-every", "0",
        ],
    )

    main()

    with open(output_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["accused_id"] == "ACC-1"
    assert rows[0]["risk_score"] == "55.5"
    assert rows[0]["pipeline_run_id"] == "RUN-E2E"


def test_main_limit_scores_only_first_n_rows(tmp_path, monkeypatch):
    feature_csv = tmp_path / "accused_features.csv"
    with open(feature_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["accused_id", "offense_count"])
        writer.writeheader()
        writer.writerow({"accused_id": "ACC-1", "offense_count": "3"})
        writer.writerow({"accused_id": "ACC-2", "offense_count": "1"})
        writer.writerow({"accused_id": "ACC-3", "offense_count": "2"})

    output_csv = tmp_path / "risk_scores_sample.csv"
    call_count = {"n": 0}

    def fake_post(url, headers=None, json=None):
        call_count["n"] += 1
        return FakeResponse({"predictions": [{"risk_score": 10.0 * call_count["n"]}]})

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
            "--pipeline-run-id", "RUN-SMOKE",
            "--limit", "2",
            "--no-resume",
            "--progress-every", "0",
        ],
    )

    main()

    with open(output_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert call_count["n"] == 2
    assert [r["accused_id"] for r in rows] == ["ACC-1", "ACC-2"]
