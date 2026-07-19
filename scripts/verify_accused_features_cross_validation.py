#!/usr/bin/env python3
"""Cross-validates neo4j_accused_features_driver.py's output against the anchor confirmed
16 Jul 2026: accused_persons.prior_offense_count (Catalyst DataStore) exactly matches a
Neo4j ACCUSED_IN relationship count via two independent code paths (~15.0% repeat-offender
rate). Reports the match rate; does not silently reconcile any mismatch it finds.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def load_offense_counts(accused_features_csv: Path) -> dict[str, int]:
    with open(accused_features_csv, newline="") as f:
        return {row["accused_id"]: int(row["offense_count"]) for row in csv.DictReader(f)}


def load_prior_offense_counts(accused_persons_csv: Path) -> dict[str, int]:
    with open(accused_persons_csv, newline="") as f:
        return {row["accused_id"]: int(row["prior_offense_count"]) for row in csv.DictReader(f)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-validate computed offense_count vs DataStore")
    parser.add_argument("--accused-features-csv", type=Path, default=Path("accused_features.csv"))
    parser.add_argument(
        "--accused-persons-csv",
        type=Path,
        default=Path("data/catalyst_datastore/accused_persons.csv"),
    )
    args = parser.parse_args()

    computed = load_offense_counts(args.accused_features_csv)
    datastore = load_prior_offense_counts(args.accused_persons_csv)

    common_ids = set(computed) & set(datastore)
    missing_from_computed = set(datastore) - set(computed)
    missing_from_datastore = set(computed) - set(datastore)

    matches = [aid for aid in common_ids if computed[aid] == datastore[aid]]
    mismatches = [aid for aid in common_ids if computed[aid] != datastore[aid]]

    match_rate = len(matches) / len(common_ids) * 100 if common_ids else 0.0

    print(f"[cross-validation] accused in accused_features.csv: {len(computed)}")
    print(f"[cross-validation] accused in accused_persons.csv:  {len(datastore)}")
    print(f"[cross-validation] compared (present in both):      {len(common_ids)}")
    if missing_from_computed:
        print(f"[cross-validation] WARNING: {len(missing_from_computed)} accused in DataStore "
              f"but missing from accused_features.csv: {sorted(missing_from_computed)[:10]}")
    if missing_from_datastore:
        print(f"[cross-validation] WARNING: {len(missing_from_datastore)} accused in "
              f"accused_features.csv but missing from DataStore: "
              f"{sorted(missing_from_datastore)[:10]}")
    print(f"[cross-validation] offense_count == prior_offense_count: "
          f"{len(matches)}/{len(common_ids)} ({match_rate:.2f}%)")

    if mismatches:
        print(f"[cross-validation] {len(mismatches)} MISMATCHES (first 10):")
        for aid in sorted(mismatches)[:10]:
            print(f"    {aid}: computed offense_count={computed[aid]} != "
                  f"DataStore prior_offense_count={datastore[aid]}")

    if match_rate < 99.9:
        print("[cross-validation] STOPPING: match rate is not ~100% — see mismatches above, "
              "not silently reconciling.", file=sys.stderr)
        return 1

    print("[cross-validation] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
