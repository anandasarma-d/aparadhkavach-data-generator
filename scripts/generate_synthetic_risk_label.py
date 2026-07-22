"""Generates a SYNTHETIC risk_label training column for QuickML, per ADR-030, since no
historical ground truth exists anywhere in this dataset — every accused_persons.risk_score in
the raw seed data (data/entities/accused.json) is null, and accused_features.csv
(feature_builder.py's Section 7.5.1 output) has no target column at all.

Named risk_label, not risk_score — Section 7.5.1's own target specification, and deliberately
distinct from risk_score, which names the trained model's eventual output once QuickML
actually produces one. This column is that model's training target, not a prediction.

THIS IS NOT A VALIDATED CRIMINOLOGICAL RISK MODEL (ADR-030). It is a hand-picked weighted
formula over existing features, used only to give QuickML's regression trainer something to
learn from so the pipeline architecture and its feature-importance explainability can be
demonstrated end-to-end. Never present risk_label values derived from this script as real
predictive signal in demo/judge Q&A — they're a synthetic proxy label, not ground truth.

Formula (ADR-030; weights and feature choice are Anand's explicit call, not derived from data):

    risk_label = 100 * (
        0.40 * normalize(crime_type_severity_max)
      + 0.25 * normalize(co_accused_count)
      + 0.20 * normalize(1 / (days_since_last_offense + 1))
      + 0.15 * normalize(offense_count)
    )

normalize(x) = (x - min(x)) / (max(x) - min(x)), min/max taken per-feature across all rows.
0-100 numeric scale per Section 7.5.1's target specification.

recidivism_interval_avg and modus_operandi_consistency are excluded per ADR-030 — documented
deviations from Section 7.5.1's 7-feature set: the former is only 15% populated (empty
whenever offense_count < 2, since there's no interval to average over a single offense), the
latter isn't a real column in accused_features.csv at all in this run (see
feature_builder.py's module docstring on Neo4j's :FIR node carrying no modus_operandi
property) — there's nothing to normalize for either.

Output: writes risk_label directly into accused_features.csv (in place) — no derived file.
"""

from __future__ import annotations

import csv
import statistics

SOURCE_CSV = "accused_features.csv"

WEIGHTS = {
    "crime_type_severity_max": 0.40,
    "co_accused_count": 0.25,
    "_inverse_days_since_last_offense": 0.20,
    "offense_count": 0.15,
}


def normalize(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def main() -> None:
    with open(SOURCE_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if fieldnames and "risk_label" in fieldnames:
        raise SystemExit(
            f"{SOURCE_CSV} already has a risk_label column — remove it first if regenerating"
        )

    crime_severity = [float(r["crime_type_severity_max"]) for r in rows]
    co_accused = [float(r["co_accused_count"]) for r in rows]
    inverse_days = [1.0 / (float(r["days_since_last_offense"]) + 1.0) for r in rows]
    offense_count = [float(r["offense_count"]) for r in rows]

    normalized = {
        "crime_type_severity_max": normalize(crime_severity),
        "co_accused_count": normalize(co_accused),
        "_inverse_days_since_last_offense": normalize(inverse_days),
        "offense_count": normalize(offense_count),
    }

    risk_labels = []
    for i in range(len(rows)):
        label = 100.0 * sum(WEIGHTS[key] * normalized[key][i] for key in WEIGHTS)
        risk_labels.append(label)
        rows[i]["risk_label"] = f"{label:.4f}"

    output_fieldnames = list(fieldnames) + ["risk_label"]
    with open(SOURCE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote risk_label into {len(rows)} rows of {SOURCE_CSV}")
    print(f"risk_label: min={min(risk_labels):.4f} max={max(risk_labels):.4f} "
          f"mean={statistics.mean(risk_labels):.4f} "
          f"stdev={statistics.stdev(risk_labels):.4f}")


if __name__ == "__main__":
    main()
