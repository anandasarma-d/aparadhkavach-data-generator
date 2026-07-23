"""Calls a deployed QuickML pipeline's REST scoring endpoint per row of a feature CSV
(ACCUSED_FEATURES or HOTSPOT_FEATURES, from feature_builder.py) and writes a CSV shaped for
`catalyst ds:import` into risk_scores or hotspot_forecasts — closing the gap QuickML itself
doesn't: AutoML pipelines produce a trained model and a scoring endpoint, but never write
predictions back into DataStore on their own (Section 7.5.1/7.5.2, Section 5.5 Flow E).

CONFIRMED vs ASSUMED about the QuickML endpoint contract:

CONFIRMED (docs.catalyst.zoho.com/en/quickml/help/pipeline-endpoints/ + live smoke 23 Jul 2026):
  - Method: POST, to the pipeline's own Deployment Url (passed in as --endpoint-url; this
    script does not construct or guess the URL).
  - Headers: X-QUICKML-ENDPOINT-KEY, Authorization: Zoho-oauthtoken <access_token>,
    CATALYST-ORG, Environment ("Development"/"Production").
  - OAuth scope: QuickML.deployment.READ (external-mode Self Client pattern from the
    External Services — Provisioning & Account Setup Guide's "Catalyst SDK Access from
    Local/External Code" section — same refresh-token/access-token exchange, reused here
    rather than inventing a new auth flow).
  - Request body: {"data": {<feature_name>: <value>, ...}} — one row per call. Flat feature
    maps and {"record": ...} wrappers return INVALID_DATA. Batch / array-of-rows is NOT
    assumed. risk_label is stripped (ADR-030 training TARGET); accused_id is REQUIRED.
    Empty-string feature values are sent as JSON null ("" is INVALID_DATA; omit is
    "missing_columns" — confirmed A2 on recidivism_interval_avg).
  - Response body: {"status":"success","pipeLineType":"prediction","result":[<numeric>]}.
    extract_prediction_value() reads result[0]; also still tries predictions[0] / candidate
    dict keys for older assumed shapes. Live smoke had no feature_importance — empty string.

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
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

import requests

from feature_builder import write_feature_csv

CATALYST_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # confirmed literal format, DataStore Runbook

RISK_SCORE_KEYS = ("risk_score", "prediction", "predicted_value", "score")
FIR_COUNT_KEYS = ("fir_count", "prediction", "predicted_value", "predicted_fir_count", "score")

RISK_SCORE_FIELDNAMES = (
    "score_id",
    "accused_id",
    "risk_score",
    "feature_importance",
    "pipeline_run_id",
    "scored_at",
    "created_at",
)

# Training TARGET only (ADR-030) — never send as a model INPUT. accused_id IS required by the
# published QuickML schema (confirmed 23 Jul 2026 smoke).
NON_INPUT_FEATURE_COLUMNS = frozenset({"risk_label"})


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
    """Confirmed single-row request shape — see module docstring.

    Strips risk_label (training target). Keeps accused_id (schema-required).
    Empty-string feature values become JSON null — QuickML rejects "" (confirmed A2:
    empty recidivism_interval_avg caused INVALID_DATA; null scores successfully).
    """
    data: dict[str, Any] = {}
    for key, value in feature_row.items():
        if key in NON_INPUT_FEATURE_COLUMNS:
            continue
        if value is None or (isinstance(value, str) and value.strip() == ""):
            data[key] = None
        else:
            data[key] = value
    return {"data": data}


def extract_prediction_value(response_json: dict[str, Any], candidate_keys: tuple[str, ...]) -> Any:
    """ASSUMED response shape — see module docstring. Fails loud, listing what was tried,
    rather than guessing which key holds the prediction.

    Also accepts the Catalyst SDK envelope shape {"status": "...", "result": [<value>]},
    which the REST docs never illustrate but the SDK predict() docs do.
    """
    sources: list[Any] = []
    predictions = response_json.get("predictions")
    if isinstance(predictions, list) and predictions:
        sources.append(predictions[0])
    result = response_json.get("result")
    if isinstance(result, list) and result:
        sources.append(result[0])
    sources.append(response_json)

    for source in sources:
        if isinstance(source, (int, float)) and not isinstance(source, bool):
            return source
        if isinstance(source, str):
            try:
                return float(source)
            except ValueError:
                pass
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
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = response.text
        raise requests.HTTPError(
            f"{exc} — response body: {body[:2000]}", response=response
        ) from None
    return response.json()


def _now_catalyst() -> str:
    return datetime.now(timezone.utc).strftime(CATALYST_DATETIME_FORMAT)


def load_scored_accused_ids(output_csv: str | Path) -> set[str]:
    """Return accused_ids already present in an incremental risk_scores CSV (for --resume)."""
    path = Path(output_csv)
    if not path.is_file() or path.stat().st_size == 0:
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "accused_id" not in reader.fieldnames:
            return set()
        return {row["accused_id"] for row in reader if row.get("accused_id")}


def open_risk_scores_writer(
    output_csv: str | Path, *, resume: bool
) -> tuple[TextIO, csv.DictWriter, set[str]]:
    """Open risk_scores CSV for append; write header if new file. Returns (fh, writer, done_ids)."""
    path = Path(output_csv)
    done_ids: set[str] = set()
    if resume and path.is_file() and path.stat().st_size > 0:
        done_ids = load_scored_accused_ids(path)
        fh = path.open("a", newline="", encoding="utf-8")
        writer = csv.DictWriter(fh, fieldnames=list(RISK_SCORE_FIELDNAMES))
        return fh, writer, done_ids

    # Fresh file (no resume, or empty/missing output)
    fh = path.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=list(RISK_SCORE_FIELDNAMES))
    writer.writeheader()
    fh.flush()
    return fh, writer, set()


def _build_risk_output_row(
    row: dict[str, str],
    response_json: dict[str, Any],
    pipeline_run_id: str,
) -> dict[str, Any]:
    risk_score = extract_prediction_value(response_json, RISK_SCORE_KEYS)
    feature_importance = response_json.get("feature_importance")
    if feature_importance is None and isinstance(response_json.get("predictions"), list):
        preds = response_json["predictions"]
        if preds and isinstance(preds[0], dict):
            feature_importance = preds[0].get("feature_importance")

    accused_id = row["accused_id"]
    now = _now_catalyst()
    return {
        "score_id": f"RISK-{pipeline_run_id}-{accused_id}",
        "accused_id": accused_id,
        "risk_score": risk_score,
        "feature_importance": json.dumps(feature_importance) if feature_importance else "",
        "pipeline_run_id": pipeline_run_id,
        "scored_at": now,
        "created_at": now,
    }


def score_risk_rows(
    feature_rows: list[dict[str, str]],
    endpoint_url: str,
    headers: dict[str, str],
    pipeline_run_id: str,
    post_fn: Callable[..., Any] = requests.post,
    *,
    output_csv: str | Path | None = None,
    resume: bool = False,
    progress_every: int = 50,
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """ACCUSED_FEATURES rows -> risk_scores rows (Section 5.4 schema).

    When output_csv is set, each scored row is flushed to disk immediately (so a killed run
    still leaves a usable partial CSV). With resume=True, accused_ids already in that file
    are skipped.
    """
    log = log or (lambda msg: print(msg, flush=True))
    output_rows: list[dict] = []
    done_ids: set[str] = set()
    writer: csv.DictWriter | None = None
    fh: TextIO | None = None

    if output_csv is not None:
        fh, writer, done_ids = open_risk_scores_writer(output_csv, resume=resume)

    pending = [row for row in feature_rows if row["accused_id"] not in done_ids]
    skipped = len(feature_rows) - len(pending)
    total = len(pending)
    if skipped:
        log(f"resume: skipping {skipped} already-scored rows; {total} remaining")
    log(f"scoring {total} risk rows → {output_csv or '(memory only)'}")

    t0 = time.time()
    try:
        for i, row in enumerate(pending, start=1):
            response_json = call_prediction_endpoint(endpoint_url, headers, row, post_fn)
            out = _build_risk_output_row(row, response_json, pipeline_run_id)
            output_rows.append(out)
            if writer is not None and fh is not None:
                writer.writerow(out)
                fh.flush()

            if progress_every > 0 and (i % progress_every == 0 or i == total):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0.0
                eta_s = (total - i) / rate if rate else 0.0
                log(
                    f"progress {i}/{total} "
                    f"last={out['accused_id']} score={out['risk_score']} "
                    f"{rate:.2f} rows/s eta={eta_s / 60:.1f}m"
                )
    finally:
        if fh is not None:
            fh.close()

    log(f"done: wrote {len(output_rows)} new rows ({skipped} skipped on resume)")
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
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Score only the first N rows (smoke-test). Omit to score the full CSV.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="Print progress every N scored rows (risk_scores). 0 disables progress lines.",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "For risk_scores: skip accused_ids already in --output-csv and append "
            "(default: true). Use --no-resume to overwrite and score from scratch."
        ),
    )
    args = parser.parse_args()

    with open(args.feature_csv, newline="", encoding="utf-8") as f:
        feature_rows = list(csv.DictReader(f))
    if not feature_rows:
        raise ValueError(f"{args.feature_csv}: no rows to score")
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError(f"--limit must be >= 1, got {args.limit}")
        feature_rows = feature_rows[: args.limit]

    headers = build_headers(args.endpoint_key, args.access_token, args.catalyst_org, args.environment)

    if args.target == "risk_scores":
        score_risk_rows(
            feature_rows,
            args.endpoint_url,
            headers,
            args.pipeline_run_id,
            requests.post,
            output_csv=args.output_csv,
            resume=args.resume,
            progress_every=args.progress_every,
        )
    else:
        output_rows = score_hotspot_rows(
            feature_rows, args.endpoint_url, headers, args.pipeline_run_id, requests.post
        )
        write_feature_csv(output_rows, args.output_csv)
        print(f"done: wrote {len(output_rows)} hotspot rows → {args.output_csv}", flush=True)


if __name__ == "__main__":
    main()
