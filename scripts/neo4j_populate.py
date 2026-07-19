#!/usr/bin/env python3
"""AparadhKavach synthetic dataset — Phase 3: Neo4j Population.

Spec source: Notion Section 4.5 Phase 3 (population pseudocode), Section
4.7 Level 2 (post-population Cypher structural validation), and Section
5.4's Neo4j AuraDB "Graph Store" tables (node labels/properties,
relationship types/properties, index strategy), fetched fresh 2026-07-15.

Reads the JSON entity files + relationship CSVs written by
generate_entities.py / weave_relationships.py and loads them into Neo4j:
MERGE all node types (idempotent on entity ID) -> MERGE all relationships
-> CREATE all 10 indexes from Section 5.4 (not 4.5's abbreviated 5-index
list) -> run Section 4.7 Level 2 validation queries and report.

Connection is read from .env (NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD) via
python-dotenv - this script runs manually, is not Catalyst-deployed, and
does not hardcode anything about where Neo4j is hosted. The same script
runs unmodified against a local Neo4j (Docker or otherwise) or Aura
staging/prod - only the .env values change (Section 12.1: local dev uses
local containers, not cloud instances).

Flagged, not silently resolved: Section 5.4's Neo4j property tables list a
DELIBERATELY NARROWER field set per node/relationship than the full logical
model (Section 5.3) or the JSON/CSV files this script reads - e.g. FIR's
Neo4j properties omit police_station/narrative_text/sections_cited/
modus_operandi/investigation_stage; ACCUSED_IN's Neo4j properties omit
date_added; OWNS omits link_fir_id. This is Section 5.4's explicit design
("Only the fields needed for graph traversal, filtering, and network
analysis are stored here - detailed structured fields live in Catalyst
DataStore"), not an oversight - this script deliberately projects down to
exactly Section 5.4's documented property lists, dropping every other field
from the source JSON/CSV. The full field set is expected to be loaded into
Catalyst DataStore by a separate script (Flow A Branch 1, out of scope
here).

BankAccount: Section 4.2 generates 0 BankAccount records ("stub nodes
only... schema extension point created, no data loaded") - there is no
bank_accounts.json for this script to read, so no :BankAccount nodes are
created in this MVP run. This matches spec; it is not a bug.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

BATCH_SIZE = 500

# ---------------------------------------------------------------------------
# Section 5.4 "Key Properties Stored in Neo4j" per node label - deliberately
# narrower than Section 5.3's full logical attribute list (see module
# docstring). id_field is the MERGE key.
# ---------------------------------------------------------------------------

NODE_SPECS = {
    "FIR": {
        "source_file": "firs.json",
        "id_field": "fir_id",
        "properties": [
            "fir_id", "fir_number", "district", "date_filed", "crime_type",
            "legal_code", "status", "event_context",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    "Accused": {
        "source_file": "accused.json",
        "id_field": "accused_id",
        "properties": [
            "accused_id", "name", "age_group", "gender", "address_district",
            "prior_offense_count", "risk_score",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    "Victim": {
        "source_file": "victims.json",
        "id_field": "victim_id",
        "properties": [
            "victim_id", "name", "age_group", "gender", "address_district",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    "Witness": {
        "source_file": "witnesses.json",
        "id_field": "witness_id",
        "properties": [
            "witness_id", "name",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    "Location": {
        "source_file": "locations.json",
        "id_field": "location_id",
        "properties": [
            "location_id", "district", "taluk", "location_type", "lat", "lon",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    "Vehicle": {
        "source_file": "vehicles.json",
        "id_field": "vehicle_id",
        "properties": [
            "vehicle_id", "registration_number", "vehicle_type",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    "PhoneNumber": {
        "source_file": "phone_numbers.json",
        "id_field": "phone_id",
        "properties": [
            "phone_id", "number", "carrier",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    "InvestigationOfficer": {
        "source_file": "officers.json",
        "id_field": "officer_id",
        "properties": [
            "officer_id", "name", "rank", "district",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    "CrimeType": {
        "source_file": "crime_types.json",
        "id_field": "type_id",
        "properties": [
            "type_id", "category", "ipc_section", "bns_section", "severity_level",
            "created_at", "updated_at", "created_by", "updated_by",
        ],
    },
    # BankAccount deliberately absent - no source file, 0 records (Section 4.2).
}

# ---------------------------------------------------------------------------
# Section 5.4 "Relationship types and properties in Neo4j" - again narrower
# than 5.3. OWNS is a single Neo4j relationship TYPE for both vehicle and
# phone ownership, disambiguated by a `type` match-key property per 5.4's
# literal notation: [:OWNS {type:'vehicle'}] / [:OWNS {type:'phone'}].
# ---------------------------------------------------------------------------

REL_SPECS = [
    {
        "csv_file": "accused_in.csv",
        "rel_type": "ACCUSED_IN",
        "start": ("Accused", "accused_id", "accused_id"),
        "end": ("FIR", "fir_id", "fir_id"),
        "match_key_columns": {},
        "properties": ["role_in_case", "warrant_status"],
    },
    {
        "csv_file": "victim_in.csv",
        "rel_type": "VICTIM_IN",
        "start": ("Victim", "victim_id", "victim_id"),
        "end": ("FIR", "fir_id", "fir_id"),
        "match_key_columns": {},
        "properties": ["injury_severity"],
    },
    {
        "csv_file": "witnessed.csv",
        "rel_type": "WITNESSED",
        "start": ("Witness", "witness_id", "witness_id"),
        "end": ("FIR", "fir_id", "fir_id"),
        "match_key_columns": {},
        "properties": ["statement_reliability"],
    },
    {
        "csv_file": "occurred_at.csv",
        "rel_type": "OCCURRED_AT",
        "start": ("FIR", "fir_id", "fir_id"),
        "end": ("Location", "location_id", "location_id"),
        "match_key_columns": {},
        "properties": ["landmark"],
    },
    {
        "csv_file": "investigated_by.csv",
        "rel_type": "INVESTIGATED_BY",
        "start": ("FIR", "fir_id", "fir_id"),
        "end": ("InvestigationOfficer", "officer_id", "officer_id"),
        "match_key_columns": {},
        "properties": ["is_lead_officer"],
    },
    {
        "csv_file": "owns_vehicle.csv",
        "rel_type": "OWNS",
        "start": ("Accused", "accused_id", "accused_id"),
        "end": ("Vehicle", "vehicle_id", "vehicle_id"),
        "match_key_columns": {"type": "vehicle"},  # literal per 5.4: [:OWNS {type:'vehicle'}]
        "properties": ["ownership_type"],
    },
    {
        "csv_file": "owns_phone.csv",
        "rel_type": "OWNS",
        "start": ("Accused", "accused_id", "accused_id"),
        "end": ("PhoneNumber", "phone_id", "phone_id"),
        "match_key_columns": {"type": "phone"},  # literal per 5.4: [:OWNS {type:'phone'}]
        "properties": ["ownership_type"],
    },
    {
        "csv_file": "contacted.csv",
        "rel_type": "CONTACTED",
        "start": ("PhoneNumber", "phone_id", "phone_id_1"),
        "end": ("PhoneNumber", "phone_id", "phone_id_2"),
        "match_key_columns": {},
        "properties": ["contact_count", "contact_date"],
    },
    {
        "csv_file": "associated_with.csv",
        "rel_type": "ASSOCIATED_WITH",
        "start": ("Accused", "accused_id", "accused_id_1"),
        "end": ("Accused", "accused_id", "accused_id_2"),
        "match_key_columns": {},
        "properties": ["shared_fir_count", "relationship_type"],
    },
    {
        "csv_file": "linked_to.csv",
        "rel_type": "LINKED_TO",
        "start": ("FIR", "fir_id", "fir_id_1"),
        "end": ("FIR", "fir_id", "fir_id_2"),
        "match_key_columns": {},
        "properties": ["link_type", "link_confidence"],
    },
    {
        "csv_file": "of_type.csv",
        "rel_type": "OF_TYPE",
        "start": ("FIR", "fir_id", "fir_id"),
        "end": ("CrimeType", "type_id", "type_id"),
        "match_key_columns": {},
        "properties": ["primary_section"],
    },
]
assert len(REL_SPECS) == 11

# Section 5.4's full 10-index list, verbatim (not Section 4.5's abbreviated
# 5-index pseudocode).
INDEX_STATEMENTS = [
    "CREATE INDEX fir_id_idx IF NOT EXISTS FOR (f:FIR) ON (f.fir_id)",
    "CREATE INDEX fir_district_idx IF NOT EXISTS FOR (f:FIR) ON (f.district)",
    "CREATE INDEX fir_date_idx IF NOT EXISTS FOR (f:FIR) ON (f.date_filed)",
    "CREATE INDEX accused_id_idx IF NOT EXISTS FOR (a:Accused) ON (a.accused_id)",
    "CREATE INDEX accused_district_idx IF NOT EXISTS FOR (a:Accused) ON (a.address_district)",
    "CREATE INDEX location_district_idx IF NOT EXISTS FOR (l:Location) ON (l.district)",
    "CREATE INDEX crimetype_ipc_idx IF NOT EXISTS FOR (ct:CrimeType) ON (ct.ipc_section)",
    "CREATE INDEX crimetype_bns_idx IF NOT EXISTS FOR (ct:CrimeType) ON (ct.bns_section)",
    "CREATE INDEX phone_number_idx IF NOT EXISTS FOR (p:PhoneNumber) ON (p.number)",
    "CREATE INDEX vehicle_reg_idx IF NOT EXISTS FOR (v:Vehicle) ON (v.registration_number)",
]
assert len(INDEX_STATEMENTS) == 10


def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def load_json(path: Path) -> list[dict]:
    with path.open() as f:
        return json.load(f)


def load_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def project(record: dict, properties: list[str]) -> dict:
    """Keep only the Section 5.4-documented properties, dropping None values
    (Cypher SET of a null property removes it, which is also the correct
    representation for e.g. a state-wide officer's absent `district`)."""
    return {k: record[k] for k in properties if record.get(k) is not None}


def merge_nodes(driver, database: str, label: str, id_field: str, records: list[dict]) -> int:
    query = (
        f"UNWIND $rows AS row "
        f"MERGE (n:{label} {{{id_field}: row.{id_field}}}) "
        f"SET n += row"
    )
    total = 0
    with driver.session(database=database) as session:
        for batch in chunked(records, BATCH_SIZE):
            session.run(query, rows=batch)
            total += len(batch)
    return total


def merge_relationships(driver, database: str, spec: dict, rows: list[dict]) -> int:
    start_label, start_prop, start_col = spec["start"]
    end_label, end_prop, end_col = spec["end"]
    rel_type = spec["rel_type"]
    match_keys = spec["match_key_columns"]
    prop_names = spec["properties"]

    match_clause = ", ".join(f"{k}: {json.dumps(v)}" for k, v in match_keys.items())
    rel_pattern = f"[r:{rel_type}{' {' + match_clause + '}' if match_clause else ''}]"

    query = (
        f"UNWIND $rows AS row "
        f"MATCH (a:{start_label} {{{start_prop}: row.start_id}}) "
        f"MATCH (b:{end_label} {{{end_prop}: row.end_id}}) "
        f"MERGE (a)-{rel_pattern}->(b) "
        f"SET r += row.props"
    )

    payload = []
    skipped = 0
    for row in rows:
        start_id = row.get(start_col)
        end_id = row.get(end_col)
        if not start_id or not end_id:
            skipped += 1
            continue
        props = {k: row[k] for k in prop_names if row.get(k) not in (None, "")}
        payload.append({"start_id": start_id, "end_id": end_id, "props": props})

    total = 0
    with driver.session(database=database) as session:
        for batch in chunked(payload, BATCH_SIZE):
            session.run(query, rows=batch)
            total += len(batch)
    if skipped:
        print(f"    ! skipped {skipped} rows with a blank endpoint id")
    return total


def create_indexes(driver, database: str) -> None:
    with driver.session(database=database) as session:
        for stmt in INDEX_STATEMENTS:
            session.run(stmt)


# ---------------------------------------------------------------------------
# Section 4.7 Level 2 - Structural Validation (Cypher queries against Neo4j)
# ---------------------------------------------------------------------------

LEVEL2_QUERIES = {
    "isolated_nodes": {
        "query": "MATCH (n) WHERE NOT (n)--() RETURN count(n) AS value",
        "expected": "0",
    },
    "repeat_offenders": {
        "query": (
            "MATCH (a:Accused)-[:ACCUSED_IN]->(f:FIR) "
            "WITH a, count(f) AS fir_count "
            "WHERE fir_count > 1 "
            "RETURN count(a) AS value"
        ),
        "expected": "~670 (15% of 4,460)",
    },
    "cross_district_accused": {
        "query": (
            "MATCH (a:Accused)-[:ACCUSED_IN]->(f:FIR)-[:OCCURRED_AT]->(l:Location) "
            "WITH a, collect(DISTINCT l.district) AS districts "
            "WHERE size(districts) > 1 "
            "RETURN count(a) AS value"
        ),
        "expected": "~357 (8% of 4,460)",
    },
    "hotspot_locations": {
        "query": (
            "MATCH (l:Location)<-[:OCCURRED_AT]-(f:FIR) "
            "WITH l, count(f) AS incident_count "
            "WHERE incident_count > 3 "
            "RETURN count(l) AS value"
        ),
        "expected": "~224 (20% of 1,120 locations)",
    },
}


def run_level2_validation(driver, database: str) -> None:
    print("=" * 88)
    print("Section 4.7 Level 2 - Structural Validation (Cypher queries against Neo4j)")
    print("=" * 88)
    with driver.session(database=database) as session:
        for name, spec in LEVEL2_QUERIES.items():
            result = session.run(spec["query"])
            value = result.single()["value"]
            print(f"[{name}] actual={value}  expected={spec['expected']}")

        print()
        print("[all_crime_types_populated] all 45 CrimeType nodes should have >=1 FIR:")
        result = session.run(
            "MATCH (ct:CrimeType) "
            "OPTIONAL MATCH (ct)<-[:OF_TYPE]-(f:FIR) "
            "WITH ct, count(f) AS fir_count "
            "RETURN count(ct) AS total_crime_types, "
            "       sum(CASE WHEN fir_count = 0 THEN 1 ELSE 0 END) AS unpopulated_count, "
            "       collect(CASE WHEN fir_count = 0 THEN ct.type_id ELSE null END) AS unpopulated_ids"
        )
        row = result.single()
        unpopulated = [x for x in row["unpopulated_ids"] if x is not None]
        print(f"  total CrimeType nodes: {row['total_crime_types']} (expected 45)")
        print(f"  with 0 FIRs: {row['unpopulated_count']} (expected 0)")
        if unpopulated:
            print(f"  unpopulated type_ids: {unpopulated}")
    print("=" * 88)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AparadhKavach Phase 3 - Neo4j population")
    parser.add_argument("--entities-dir", type=Path, default=Path("data/entities"))
    parser.add_argument("--relationships-dir", type=Path, default=Path("data/relationships"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--skip-validation", action="store_true",
                         help="skip the Section 4.7 Level 2 post-population report")
    args = parser.parse_args()

    load_dotenv(dotenv_path=args.env_file)
    uri = os.environ.get("NEO4J_URI")
    username = os.environ.get("NEO4J_USERNAME")
    password = os.environ.get("NEO4J_PASSWORD")
    if not all([uri, username, password]):
        print("ERROR: NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD must all be set "
              f"(via {args.env_file} or the environment). See .env.example.", file=sys.stderr)
        sys.exit(1)

    print(f"[neo4j_populate] connecting to {uri} as {username}")
    driver = GraphDatabase.driver(uri, auth=(username, password))
    driver.verify_connectivity()

    try:
        print("[neo4j_populate] Phase 1: MERGE nodes")
        for label, spec in NODE_SPECS.items():
            path = args.entities_dir / spec["source_file"]
            records = load_json(path)
            projected = [project(r, spec["properties"]) for r in records]
            count = merge_nodes(driver, args.database, label, spec["id_field"], projected)
            print(f"  {label:<24} {count:>6} nodes merged  <- {path}")
        print("  BankAccount              (skipped - 0 records, stub node label only per Section 4.2)")

        print("[neo4j_populate] Phase 2: MERGE relationships")
        for spec in REL_SPECS:
            path = args.relationships_dir / spec["csv_file"]
            rows = load_csv(path)
            count = merge_relationships(driver, args.database, spec, rows)
            match_note = f" {spec['match_key_columns']}" if spec["match_key_columns"] else ""
            print(f"  {spec['rel_type']:<18}{match_note:<16} {count:>6} relationships merged  <- {path}")

        print("[neo4j_populate] Phase 3: creating indexes (Section 5.4 - 10 indexes)")
        create_indexes(driver, args.database)
        for stmt in INDEX_STATEMENTS:
            print(f"  {stmt}")

        print("[neo4j_populate] done.")

        if not args.skip_validation:
            print()
            run_level2_validation(driver, args.database)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
