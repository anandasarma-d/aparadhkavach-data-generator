"""Calls a deployed QuickML pipeline's REST scoring endpoint per row of a feature CSV
(ACCUSED_FEATURES or HOTSPOT_FEATURES, from feature_builder.py) and writes a CSV shaped for
`catalyst ds:import` into risk_scores or hotspot_forecasts — closing the gap QuickML itself
doesn't: AutoML pipelines produce a trained model and a scoring endpoint, but never write
predictions back into DataStore on their own (Section 7.5.1/7.5.2, Section 5.5 Flow E).

CONFIRMED vs ASSUMED about the QuickML endpoint contract (fetched fresh this session):

CONFIRMED (docs.catalyst.zoho.com/en/quickml/help/pipeline-endpoints/):
  - Method: POST, to the pipeline's own Deployment Url (passed in as --endpoint-url; this
    script does not construct or guess the URL).
  - Headers: X-QUICKML-ENDPOINT-KEY, Authorization: Zoho-oauthtoken <access_token>,
    CATALYST-ORG, Environment ("Development"/"Production").
  - OAuth scope: QuickML.deployment.READ (external-mode Self Client pattern from the
    External Services — Provisioning & Account Setup Guide's "Catalyst SDK Access from
    Local/External Code" section — same refresh-token/access-token exchange, reused here
    rather than inventing a new auth flow).

ASSUMED — not shown in QuickML's own docs (page confirms endpoints exist and lists headers,
but shows no example request/response JSON) — build_request_payload() and
extract_prediction_value() below isolate every assumption to one place each so they're a
one-function fix once a real endpoint exists:
  - Request body: {"record": {<feature_name>: <value>, ...}} — one row per call. Batch
    support is NOT assumed; nothing in the fetched docs confirms an array-of-records form,
    so this script calls the endpoint once per CSV row. OPEN QUESTION FOR ANAND: confirm
    whether the deployed endpoint accepts a batch/array body once a real endpoint exists —
    if so, this is a real optimization opportunity for large feature tables.
  - Response body: extract_prediction_value() tries, in order, response["predictions"][0][key],
    response[key], for each candidate key name — e.g. ["risk_score", "prediction",
    "predicted_value", "score"] for the risk scorer. If none of the candidates are present it
    raises, dumping the actual top-level response keys, rather than silently writing a wrong
    or empty value into risk_scores/hotspot_forecasts.

Hotspot forecaster note: Section 7.5.2's QuickML config targets `fir_count` (numeric) — the
endpoint is assumed to return a predicted fir_count, not a pre-normalized hotspot_score. The
hotspot_score = (predicted_fir_count - district_min) / (district_max - district_min) formula
(Section 7.5.2) is applied by this script, per district, across the batch of rows scored in
one run — it is not something the endpoint itself is assumed to compute.

forecast_window: HOTSPOT_FEATURES rows carry year/month (not a window label like
"next_14_days"), so forecast_window is built here as f"{year}-{month:02d}". OPEN QUESTION FOR
ANAND: confirm this is the granularity wanted for hotspot_forecasts.forecast_window, or
whether it should be a coarser rolling window.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from feature_builder import write_feature_csv

CATALYST_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # confirmed literal format, DataStore Runbook

RISK_SCORE_KEYS = ("risk_score", "prediction", "predicted_value", "score")
FIR_COUNT_KEYS = ("fir_count", "prediction", "predicted_value", "predicted_fir_count", "score")


def build_headers(
    endpoint_key: str, access_token: str, catalyst_org: str, environment: str
) -> dict[str, str]:
    """Confirmed header shape — docs.catalyst.zoho.com/en/quickml/help/pipeline-endpoints/."""
    return {
        "X-QUICKML-ENDPOINT-KEY": endpoint_key,
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "CATALYST-ORG": catalyst_org,
        "Environment": environment,
    }


def build_request_payload(feature_row: dict[str, str]) -> dict[str, Any]:
    """ASSUMED single-row request shape — see module docstring."""
    return {"record": dict(feature_row)}


def extract_prediction_value(response_json: dict[str, Any], candidate_keys: tuple[str, ...]) -> Any:
    """ASSUMED response shape — see module docstring. Fails loud, listing what was tried,
    rather than guessing which key holds the prediction."""
    predictions = response_json.get("predictions")
    sources = []
    if isinstance(predictions, list) and predictions:
        sources.append(predictions[0])
    sources.append(response_json)

    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in candidate_keys:
            if key in source:
                return source[key]

    raise ValueError(
        f"none of {candidate_keys} found in QuickML response "
        f"(top-level keys: {sorted(response_json.keys())}) — the response contract is "
        f"unconfirmed (see quickml_scorer.py module docstring); update candidate_keys or "
        f"extract_prediction_value() once a real endpoint's response shape is known"
    )


def call_prediction_endpoint(
    endpoint_url: str,
    headers: dict[str, str],
    feature_row: dict[str, str],
    post_fn: Callable[..., Any] = requests.post,
) -> dict[str, Any]:
    response = post_fn(endpoint_url, headers=headers, json=build_request_payload(feature_row))
    response.raise_for_status()
    return response.json()


def _now_catalyst() -> str:
    return datetime.now(timezone.utc).strftime(CATALYST_DATETIME_FORMAT)


def score_risk_rows(
    feature_rows: list[dict[str, str]],
    endpoint_url: str,
    headers: dict[str, str],
    pipeline_run_id: str,
    post_fn: Callable[..., Any] = requests.post,
) -> list[dict]:
    """ACCUSED_FEATURES rows -> risk_scores rows (Section 5.4 schema)."""
    output_rows = []
    for row in feature_rows:
        response_json = call_prediction_endpoint(endpoint_url, headers, row, post_fn)
        risk_score = extract_prediction_value(response_json, RISK_SCORE_KEYS)
        feature_importance = response_json.get("feature_importance")
        if feature_importance is None and isinstance(response_json.get("predictions"), list):
            preds = response_json["predictions"]
            if preds and isinstance(preds[0], dict):
                feature_importance = preds[0].get("feature_importance")

        accused_id = row["accused_id"]
        output_rows.append(
            {
                "score_id": f"RISK-{pipeline_run_id}-{accused_id}",
                "accused_id": accused_id,
                "risk_score": risk_score,
                "feature_importance": json.dumps(feature_importance) if feature_importance else "",
                "pipeline_run_id": pipeline_run_id,
                "scored_at": _now_catalyst(),
                "created_at": _now_catalyst(),
            }
        )
    return output_rows


def score_hotspot_rows(
    feature_rows: list[dict[str, str]],
    endpoint_url: str,
    headers: dict[str, str],
    pipeline_run_id: str,
    post_fn: Callable[..., Any] = requests.post,
) -> list[dict]:
    """HOTSPOT_FEATURES rows -> hotspot_forecasts rows (Section 5.4 schema), applying the
    Section 7.5.2 per-district hotspot_score normalization across this batch's predictions."""
    predicted = []
    for row in feature_rows:
        response_json = call_prediction_endpoint(endpoint_url, headers, row, post_fn)
        predicted_fir_count = extract_prediction_value(response_json, FIR_COUNT_KEYS)
        confidence = response_json.get("confidence")
        if confidence is None and isinstance(response_json.get("predictions"), list):
            preds = response_json["predictions"]
            if preds and isinstance(preds[0], dict):
                confidence = preds[0].get("confidence")
        predicted.append((row, float(predicted_fir_count), confidence))

    by_district = defaultdict(list)
    for row, predicted_fir_count, _confidence in predicted:
        by_district[row["district"]].append(predicted_fir_count)

    district_bounds = {
        district: (min(values), max(values)) for district, values in by_district.items()
    }

    output_rows = []
    for row, predicted_fir_count, confidence in predicted:
        district_min, district_max = district_bounds[row["district"]]
        if district_max > district_min:
            hotspot_score = (predicted_fir_count - district_min) / (district_max - district_min)
        else:
            hotspot_score = 0.0  # single data point for this district in this batch

        forecast_window = f"{row['year']}-{int(row['month']):02d}"
        output_rows.append(
            {
                "forecast_id": (
                    f"HOTSPOT-{pipeline_run_id}-{row['district']}-{row['crime_type']}-"
                    f"{forecast_window}"
                ),
                "district_id": row["district"],
                "crime_type": row["crime_type"],
                "forecast_window": forecast_window,
                "hotspot_score": hotspot_score,
                "confidence": confidence if confidence is not None else "",
                "pipeline_run_id": pipeline_run_id,
                "forecasted_at": _now_catalyst(),
                "created_at": _now_catalyst(),
            }
        )
    return output_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Score a feature CSV against a deployed QuickML pipeline endpoint and write a "
            "CSV shaped for catalyst ds:import into risk_scores or hotspot_forecasts."
        )
    )
    parser.add_argument("--target", required=True, choices=["risk_scores", "hotspot_forecasts"])
    parser.add_argument("--feature-csv", required=True)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--endpoint-url", required=True)
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--endpoint-key", required=True)
    parser.add_argument("--catalyst-org", required=True)
    parser.add_argument("--environment", default="Development", choices=["Development", "Production"])
    parser.add_argument("--pipeline-run-id", required=True)
    args = parser.parse_args()

    with open(args.feature_csv, newline="", encoding="utf-8") as f:
        feature_rows = list(csv.DictReader(f))
    if not feature_rows:
        raise ValueError(f"{args.feature_csv}: no rows to score")

    headers = build_headers(args.endpoint_key, args.access_token, args.catalyst_org, args.environment)

    score_fn = score_risk_rows if args.target == "risk_scores" else score_hotspot_rows
    output_rows = score_fn(
        feature_rows, args.endpoint_url, headers, args.pipeline_run_id, requests.post
    )

    write_feature_csv(output_rows, args.output_csv)


if __name__ == "__main__":
    main()
