#!/usr/bin/env python3
"""Standalone verification: recomputes the repeat-offender rate from the (now backfilled)
Catalyst DataStore accused_persons.csv and checks it against Section 4.7's already-validated
target (12-18% band, ~15.0% point target) — the same number the Day 2 guardrail run and the
Defect Tracker's ACCUSED_IN fix ("landed exactly on 15.0%") already established.

Deliberately separate from guardrail_validator.py, not a modification to it (out of scope for
this fix — see this session's brief). Reads data/catalyst_datastore/accused_persons.csv only;
no live Catalyst/Neo4j connection.
"""

import csv
import sys
from pathlib import Path

TARGET_PCT = 15.0
BAND_LOW = 12.0
BAND_HIGH = 18.0


def compute_repeat_offender_rate(accused_persons_csv: Path) -> tuple[int, int, float]:
    with accused_persons_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))
    total = len(rows)
    repeat_count = sum(1 for r in rows if int(r["prior_offense_count"]) > 1)
    rate_pct = repeat_count / total * 100 if total else 0.0
    return repeat_count, total, rate_pct


def main() -> int:
    csv_path = Path("data/catalyst_datastore/accused_persons.csv")
    repeat_count, total, rate_pct = compute_repeat_offender_rate(csv_path)

    print(f"[verify_repeat_offender_rate] {csv_path}")
    print(f"[verify_repeat_offender_rate] {repeat_count}/{total} accused have "
          f"prior_offense_count > 1 -> {rate_pct:.2f}%")
    print(f"[verify_repeat_offender_rate] Section 4.7 target band: "
          f"{BAND_LOW:.1f}%-{BAND_HIGH:.1f}% (point target ~{TARGET_PCT:.1f}%)")

    in_band = BAND_LOW <= rate_pct <= BAND_HIGH
    if in_band:
        print(f"[verify_repeat_offender_rate] PASS - {rate_pct:.2f}% is within the "
              f"{BAND_LOW:.1f}-{BAND_HIGH:.1f}% band")
        return 0
    else:
        print(f"[verify_repeat_offender_rate] FAIL - {rate_pct:.2f}% is outside the "
              f"{BAND_LOW:.1f}-{BAND_HIGH:.1f}% band")
        return 1


if __name__ == "__main__":
    sys.exit(main())
