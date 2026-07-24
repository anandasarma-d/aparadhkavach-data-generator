#!/usr/bin/env python3
"""Pragmatic F2 seed: build hotspot_forecasts.csv without a live QuickML hotspot endpoint.

Uses Section 7.5.2 feature grouping from firs.csv, then applies the same *per-district*
min–max normalization as quickml_scorer.score_hotspot_rows — but substitutes fir_count
(observed) for predicted_fir_count. Honest for demo wiring; replace via QuickML scorer
+ ds:import when the hotspot endpoint is published.

Output columns match quickml_scorer hotspot target (Section 5.4 / scorer contract):
  forecast_id, district_id, crime_type, forecast_window, hotspot_score, confidence,
  pipeline_run_id, forecasted_at, created_at
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from feature_builder import build_hotspot_features, load_fir_records, write_feature_csv

DEFAULT_PIPELINE_RUN_ID = "RUN-MVP1-F2-SEED-20260724"


def _now_catalyst() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def district_names_from_firs(firs_csv: Path) -> dict[str, str]:
    """Map Catalyst district ROWID → Karnataka district name via police_station prefix."""
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    with firs_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ps = row.get("police_station") or ""
            name = ps.split(" Police Station")[0].strip() if " Police Station" in ps else ps
            if name:
                counts[row["district_id"]][name] += 1
    return {did: cnt.most_common(1)[0][0] for did, cnt in counts.items()}


def seed_rows_from_features(
    feature_rows: list[dict],
    pipeline_run_id: str,
    latest_month_only: bool,
) -> list[dict]:
    rows = feature_rows
    if latest_month_only and rows:
        ym = max((int(r["year"]), int(r["month"])) for r in rows)
        rows = [r for r in rows if (int(r["year"]), int(r["month"])) == ym]

    # Proxy "prediction" = fir_count; normalize per district like Section 7.5.2.
    by_district: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_district[str(row["district"])].append(float(row["fir_count"]))
    bounds = {d: (min(vs), max(vs)) for d, vs in by_district.items()}

    now = _now_catalyst()
    out: list[dict] = []
    for row in rows:
        district = str(row["district"])
        crime_type = str(row["crime_type"])
        year, month = int(row["year"]), int(row["month"])
        forecast_window = f"{year}-{month:02d}"
        predicted = float(row["fir_count"])
        dmin, dmax = bounds[district]
        if dmax > dmin:
            hotspot_score = (predicted - dmin) / (dmax - dmin)
        else:
            hotspot_score = 0.0
        # Omit confidence: Catalyst Double rejects empty string on ds:import; seed has none.
        out.append(
            {
                "forecast_id": (
                    f"HOTSPOT-{pipeline_run_id}-{district}-{crime_type}-{forecast_window}"
                ),
                "district_id": district,
                "crime_type": crime_type,
                "forecast_window": forecast_window,
                "hotspot_score": round(hotspot_score, 4),
                "pipeline_run_id": pipeline_run_id,
                "forecasted_at": now,
                "created_at": now,
            }
        )
    out.sort(key=lambda r: (-float(r["hotspot_score"]), r["district_id"], r["crime_type"]))
    return out


def write_district_names_csv(mapping: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["district_id", "district_name"])
        w.writeheader()
        for did, name in sorted(mapping.items(), key=lambda x: x[1]):
            w.writerow({"district_id": did, "district_name": name})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--firs-csv",
        default="data/catalyst_datastore/firs.csv",
        help="firs export / seed CSV (default: data/catalyst_datastore/firs.csv)",
    )
    parser.add_argument(
        "--output-csv",
        default="hotspot_forecasts.csv",
        help="ds:import-ready hotspot_forecasts CSV (gitignored)",
    )
    parser.add_argument(
        "--district-names-csv",
        default="hotspot_district_names.csv",
        help="ROWID→name map for the client (gitignored; optional commit of generated TS later)",
    )
    parser.add_argument("--pipeline-run-id", default=DEFAULT_PIPELINE_RUN_ID)
    parser.add_argument(
        "--all-months",
        action="store_true",
        help="Seed every (district, crime_type, month) group (~3k rows). Default: latest month only.",
    )
    args = parser.parse_args()

    firs_path = Path(args.firs_csv)
    if not firs_path.is_file():
        print(f"missing firs CSV: {firs_path}", file=sys.stderr)
        sys.exit(1)

    firs = load_fir_records(firs_path)
    features = build_hotspot_features(firs)
    seed = seed_rows_from_features(
        features, args.pipeline_run_id, latest_month_only=not args.all_months
    )
    write_feature_csv(seed, args.output_csv)

    names = district_names_from_firs(firs_path)
    write_district_names_csv(names, Path(args.district_names_csv))

    print(
        f"wrote {len(seed)} hotspot rows → {args.output_csv} "
        f"(pipeline_run_id={args.pipeline_run_id})",
        flush=True,
    )
    print(f"wrote {len(names)} district name rows → {args.district_names_csv}", flush=True)
    if seed:
        print(
            f"top score: {seed[0]['hotspot_score']} "
            f"{seed[0]['district_id']} / {seed[0]['crime_type']} / {seed[0]['forecast_window']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
