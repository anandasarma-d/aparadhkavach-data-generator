#!/usr/bin/env python3
"""AparadhKavach synthetic dataset — Catalyst DataStore CSV transform.

Spec source: Notion Section 5.4 ("Physical Data Model" - Catalyst DataStore
table DDL) and Section 5.5 Flow A Branch 1 ("DataStore bulk insert (districts
first - everything else FKs into it): districts, firs, accused_persons,
victims, officers"), fetched fresh 2026-07-15.

Reads the JSON entity files written by generate_entities.py and writes one
CSV per DataStore table, matching Section 5.4's column names/order exactly,
ready for `catalyst ds:import --table <name> <file>`. Does not call the
Catalyst CLI itself - table creation (schema) is a Catalyst console action
with no CLI equivalent, so this script only prepares import-ready files.

**districts is not one of the four tables asked for in this pass, but it is
a hard prerequisite, not optional**: Section 5.4 gives firs.district_id,
accused_persons.address_district_id, victims.address_district_id, and
officers.district_id as genuine Foreign Key columns into districts, and
Section 5.5 explicitly orders districts first for exactly this reason. The
source JSON carries district as a plain name string (e.g. "Bagalkot"), not
an ID - this script derives districts.csv from generate_entities.py's own
KARNATAKA_DISTRICTS/DISTRICT_CODE (the same source already used for vehicle
plate codes, not a second, potentially divergent mapping) and rewrites every
*_district(_id) column below as a district_id.

Two judgment calls Section 5.4 doesn't fully specify, flagged rather than
silently resolved:
- `districts.district_id` values: Section 5.4 only says "VARCHAR PRIMARY
  KEY", no ID scheme. Uses "DIST-{2-digit DISTRICT_CODE}" (e.g. "DIST-01"),
  matching this repo's existing ID conventions (FIR-000001, ACC-00001) and
  reusing the code already assigned per district for vehicle registrations,
  rather than inventing an unrelated second numbering.
- `districts.region`: Section 5.4 says "VARCHAR", no value set. Populated
  from KARNATAKA_DISTRICTS' second tuple element (urban/rural/border) -
  the only per-district classification this dataset actually has - not a
  geographic region name (no "North Karnataka"/"South Karnataka" data
  exists anywhere in this repo). Confirm this is the intended semantic
  before treating it as final.

Gap observed, not fixed here (out of this script's scope - the officer count
target is ~180, which the current officers.json already matches without
it): Section 5.8's seeded Super Admin row (officer_id OFF-SUPERADMIN-001)
is not present in data/entities/officers.json.

Defect Tracker: "catalyst_datastore_transform.py / DateTime CSV format",
found via a real failed districts.csv import (31/31 rows failed, "Invalid
input value for created_at. datetime value expected."). Catalyst's DateTime
columns require literal `YYYY-MM-DD HH:MM:SS` (space separator, no
timezone designator) - confirmed against Zoho's Data Store Columns docs -
but every DateTime value in the source JSON is ISO-8601 with a `T`
separator and (for created_at/updated_at) a `Z` suffix. to_catalyst_datetime()
below reformats every DateTime column at CSV-write time; Date-typed columns
(date_filed, first_offense_date, last_offense_date) are left untouched -
Date's YYYY-MM-DD has no time component and was never affected by this bug.
This is a pure reformat, not a timezone conversion - every source timestamp
is already UTC.

Second defect found the same way (real failed firs/accused_persons/victims
import, 100% failure - officers partially failed, exactly the ~128/180
rows with a non-null district): "Invalid input value for district_id.
bigint value expected". Catalyst's actual Foreign Key mechanism does NOT
reference the parent table's declared VARCHAR primary key value
(district_id = "DIST-01") - it references the parent row's own
Catalyst-internal ROWID (an auto-assigned bigint, e.g. "42963000000035012"),
confirmed by exporting the already-imported districts table and finding a
ROWID column holding exactly that. This is the "Phase 2 two-pass load"
referenced in this repo's task history: districts must be imported FIRST,
its ROWIDs read back via `catalyst ds:export --table districts`, THEN
firs/accused_persons/victims/officers's *_district(_id) columns rewritten
to hold those ROWIDs instead of the "DIST-XX" string - which is what
--district-rowid-csv below does. Without it, the FK columns still use the
"DIST-XX" scheme, which Catalyst will reject the same way every time.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from generate_entities import DISTRICT_CODE, KARNATAKA_DISTRICTS

SEED_TIMESTAMP = "2026-07-15T02:35:45Z"  # matches created_at/updated_at already in the source JSON


def district_id_for(name: str) -> str:
    return f"DIST-{DISTRICT_CODE[name]}"


def load_district_rowid_map(path: Path) -> dict[str, str]:
    """district_name -> Catalyst ROWID, parsed directly from the CSV
    `catalyst ds:export --table districts` produces (has ROWID and
    district_name columns among others) - not a hand-built mapping, so it
    can't drift from whatever Catalyst actually assigned."""
    mapping: dict[str, str] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            mapping[row["district_name"]] = row["ROWID"]
    return mapping


def district_fk_for(name: str, rowid_map: dict[str, str] | None) -> str:
    """FK column value for a *_district(_id) column pointing at districts.
    Requires the real Catalyst ROWID (see module docstring's second defect
    note) - falls back to the "DIST-XX" business key only when no map is
    supplied, which Catalyst's ds:import will reject the same way every
    time; that fallback exists so districts.csv alone can still be
    generated before the first import pass, not as a working alternative."""
    if rowid_map is not None:
        return rowid_map[name]
    return district_id_for(name)


def to_catalyst_datetime(value: str | None) -> str:
    """ISO-8601 ('...THH:MM:SS' or '...THH:MM:SSZ') -> Catalyst DateTime
    ('...HH:MM:SS'). Empty/None passes through as "" (e.g. risk_score_updated_at,
    still a Phase 1 stub - not a value this function needs to format)."""
    if not value:
        return ""
    return value.rstrip("Z").replace("T", " ")


def load_json(path: Path) -> list[dict]:
    with path.open() as f:
        return json.load(f)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def transform_districts() -> list[dict]:
    return [
        {
            "district_id": district_id_for(name),
            "district_name": name,
            "region": region,
            "created_at": to_catalyst_datetime(SEED_TIMESTAMP),
            "updated_at": to_catalyst_datetime(SEED_TIMESTAMP),
            "created_by": "SYSTEM_SEED",
            "updated_by": "SYSTEM_SEED",
        }
        for name, region in KARNATAKA_DISTRICTS
    ]


def transform_firs(firs: list[dict], rowid_map: dict[str, str] | None) -> list[dict]:
    return [
        {
            "fir_id": f["fir_id"],
            "fir_number": f["fir_number"],
            "district_id": district_fk_for(f["district"], rowid_map),
            "police_station": f["police_station"],
            "date_filed": f["date_filed"],
            "date_of_incident": to_catalyst_datetime(f["date_of_incident"]),
            "crime_type": f["crime_type"],
            "legal_code": f["legal_code"],
            "sections_cited": ",".join(f["sections_cited"]),
            "status": f["status"],
            "narrative_text": f["narrative_text"],
            "modus_operandi": f["modus_operandi"],
            "event_context": f["event_context"],
            "investigation_stage": f["investigation_stage"],
            "created_at": to_catalyst_datetime(f["created_at"]),
            "updated_at": to_catalyst_datetime(f["updated_at"]),
            "created_by": f["created_by"],
            "updated_by": f["updated_by"],
        }
        for f in firs
    ]


def transform_accused_persons(accused: list[dict], rowid_map: dict[str, str] | None) -> list[dict]:
    return [
        {
            "accused_id": a["accused_id"],
            "name": a["name"],
            "age": a["age"],
            "age_group": a["age_group"],
            "gender": a["gender"],
            "address_district_id": district_fk_for(a["address_district"], rowid_map),
            "address_taluk": a["address_taluk"],
            "occupation": a["occupation"],
            "prior_offense_count": a["prior_offense_count"],
            "first_offense_date": a["first_offense_date"] or "",
            "last_offense_date": a["last_offense_date"] or "",
            "risk_score": a["risk_score"] if a["risk_score"] is not None else "",
            "risk_score_updated_at": to_catalyst_datetime(a["risk_score_updated_at"]),
            "created_at": to_catalyst_datetime(a["created_at"]),
            "updated_at": to_catalyst_datetime(a["updated_at"]),
            "created_by": a["created_by"],
            "updated_by": a["updated_by"],
        }
        for a in accused
    ]


def transform_victims(victims: list[dict], rowid_map: dict[str, str] | None) -> list[dict]:
    return [
        {
            "victim_id": v["victim_id"],
            "name": v["name"],
            "age": v["age"],
            "age_group": v["age_group"],
            "gender": v["gender"],
            "address_district_id": district_fk_for(v["address_district"], rowid_map),
            "created_at": to_catalyst_datetime(v["created_at"]),
            "updated_at": to_catalyst_datetime(v["updated_at"]),
            "created_by": v["created_by"],
            "updated_by": v["updated_by"],
        }
        for v in victims
    ]


def transform_officers(officers: list[dict], rowid_map: dict[str, str] | None) -> list[dict]:
    return [
        {
            "officer_id": o["officer_id"],
            "name": o["name"],
            "rank": o["rank"],
            "badge_number": o["badge_number"],
            "station": o["station"],
            # NULL = state-wide scope (Section 5.8) - not every officer has a district
            "district_id": district_fk_for(o["district"], rowid_map) if o["district"] else "",
            "is_active": "true" if o["is_active"] else "false",
            "roles": o["roles"],
            "created_at": to_catalyst_datetime(o["created_at"]),
            "updated_at": to_catalyst_datetime(o["updated_at"]),
            "created_by": o["created_by"],
            "updated_by": o["updated_by"],
        }
        for o in officers
    ]


def main():
    parser = argparse.ArgumentParser(description="AparadhKavach Catalyst DataStore CSV transform")
    parser.add_argument("--entities-dir", type=Path, default=Path("data/entities"))
    parser.add_argument("--out-dir", type=Path, default=Path("data/catalyst_datastore"))
    parser.add_argument("--district-rowid-csv", type=Path, default=None,
                         help="CSV from `catalyst ds:export --table districts` (has ROWID + "
                              "district_name columns) - required to produce working "
                              "firs/accused_persons/victims/officers CSVs; districts must be "
                              "imported first. Without it, *_district(_id) columns fall back to "
                              "the DIST-XX business key, which Catalyst's ds:import rejects.")
    args = parser.parse_args()

    firs = load_json(args.entities_dir / "firs.json")
    accused = load_json(args.entities_dir / "accused.json")
    victims = load_json(args.entities_dir / "victims.json")
    officers = load_json(args.entities_dir / "officers.json")

    rowid_map = load_district_rowid_map(args.district_rowid_csv) if args.district_rowid_csv else None
    if rowid_map is None:
        print("[catalyst_datastore_transform] WARNING: no --district-rowid-csv given - "
              "firs/accused_persons/victims/officers *_district(_id) columns will use the "
              "DIST-XX business key, which Catalyst's ds:import will reject (see module "
              "docstring's second defect note).")

    tables = {
        "districts": (
            ["district_id", "district_name", "region", "created_at", "updated_at",
             "created_by", "updated_by"],
            transform_districts(),
        ),
        "firs": (
            ["fir_id", "fir_number", "district_id", "police_station", "date_filed",
             "date_of_incident", "crime_type", "legal_code", "sections_cited", "status",
             "narrative_text", "modus_operandi", "event_context", "investigation_stage",
             "created_at", "updated_at", "created_by", "updated_by"],
            transform_firs(firs, rowid_map),
        ),
        "accused_persons": (
            ["accused_id", "name", "age", "age_group", "gender", "address_district_id",
             "address_taluk", "occupation", "prior_offense_count", "first_offense_date",
             "last_offense_date", "risk_score", "risk_score_updated_at", "created_at",
             "updated_at", "created_by", "updated_by"],
            transform_accused_persons(accused, rowid_map),
        ),
        "victims": (
            ["victim_id", "name", "age", "age_group", "gender", "address_district_id",
             "created_at", "updated_at", "created_by", "updated_by"],
            transform_victims(victims, rowid_map),
        ),
        "officers": (
            ["officer_id", "name", "rank", "badge_number", "station", "district_id",
             "is_active", "roles", "created_at", "updated_at", "created_by", "updated_by"],
            transform_officers(officers, rowid_map),
        ),
    }

    print("[catalyst_datastore_transform] writing CSVs to", args.out_dir)
    for table_name, (fieldnames, rows) in tables.items():
        out_path = args.out_dir / f"{table_name}.csv"
        write_csv(out_path, fieldnames, rows)
        print(f"  {table_name:<18} {len(rows):>6} rows -> {out_path}")


if __name__ == "__main__":
    main()
