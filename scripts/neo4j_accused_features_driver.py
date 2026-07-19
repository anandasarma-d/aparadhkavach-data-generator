#!/usr/bin/env python3
"""AparadhKavach — Day 7: builds ACCUSED_FEATURES (Section 7.5.1) from the local Neo4j graph
and writes accused_features.csv, ready for QuickML's Local File System connector — same
output convention as PR #4's hotspot_features.csv.

Why this exists: 6 of the 7 ACCUSED_FEATURES require traversing an accused across multiple
FIRs (recidivism interval, crime-type severity, district spread, days since last offense) or
querying Neo4j directly (co_accused_count, explicitly sourced from ASSOCIATED_WITH per
Section 7.5.1) — none of this exists in the flat accused_persons DataStore table, even after
the 16 Jul prior_offense_count backfill (confirmed: that backfill only touched Catalyst
DataStore's CSV, not Neo4j's Accused.prior_offense_count property — see the cross-validation
note below). feature_builder.py's build_accused_features() already expected exactly this
shape of input (Pre-Debug Hardening Pass, 16 Jul 2026, fixture-tested only) — this driver
supplies the real thing. Extended, not rewritten: build_accused_features() itself only
gained two small, backward-compatible Offense fields (crime_type_severity, an Optional
modus_operandi) to accommodate what real Neo4j data can and can't supply — see
feature_builder.py's module docstring for the full reasoning.

Spec sources (fetched fresh 19 Jul 2026): Section 7.5.1 (feature set), Section 5.4 (Neo4j
node/relationship property names) — all Cypher below was written against Section 5.4's
CONFIRMED property lists and independently verified against the live local graph's actual
schema (`UNWIND keys(n)` over every node/relationship type queried here) before being
trusted, per this task's "do not fabricate property/relationship names" constraint.

Confirmed facts this driver relies on (live-verified, not assumed):
- Every FIR has exactly one OF_TYPE edge (3,720/3,720; 0 with >1) — safe to use as an inner
  join without silently dropping offenses.
- ASSOCIATED_WITH is stored as a SINGLE directed edge per co-accused pair (1,629 total edges,
  0 pairs with both directions present) despite Section 4.5 prose calling it "bidirectional"
  — an undirected Cypher pattern (`-[:ASSOCIATED_WITH]-`) is required, or half of all
  co-accused counts would be silently undercounted.
- f.date_filed comes back as a plain Python str ('YYYY-MM-DD'), not a Neo4j temporal type.
- Zero of the 4,460 Accused nodes have zero ACCUSED_IN edges (defensive handling below is
  belt-and-braces, not covering an observed real case).

Known, confirmed gap — modus_operandi_consistency is NOT computed by this driver:
Neo4j's :FIR node has no modus_operandi property (confirmed via exhaustive live schema query
across all 3,720 FIR nodes, matching Section 5.4's documented property list exactly).
neo4j_populate.py's own module docstring already documents this as a deliberate Section 5.4
field-projection decision ("FIR's Neo4j properties omit ... modus_operandi ... detailed
structured fields live in Catalyst DataStore"), not an oversight discovered here for the
first time. Per this task's explicit scope ("local Neo4j only — do not touch ... Catalyst
DataStore ... in this pass") and Anand's decision when this gap was surfaced: this driver
ships 6 of 7 features and drops modus_operandi_consistency from the output CSV entirely
(see strip_unavailable_feature()) rather than shipping a null column QuickML's own
completeness gate would reject anyway (Section 7.7: "0 nulls in required feature columns").
Follow-up options for Anand: extend neo4j_populate.py to also write modus_operandi onto FIR
nodes, or a separate DataStore-CSV cross-reference pass — neither is done here.

crime_type_severity_max: CrimeType.severity_level is confirmed live as a 4-value ordinal
STRING (LOW/MEDIUM/HIGH/CRITICAL, from generate_entities.py's taxonomy) — not the "1-5 scale"
Section 7.5.1 describes. SEVERITY_ORDINAL below maps these 4 confirmed values to 1-4; this
ordinal mapping is this driver's own reasonable interpretation of real, confirmed categorical
values, not sourced from Notion. Resolved per-FIR via that FIR's own OF_TYPE edge (not a
crime_type-category-level guess) — see feature_builder.py's module docstring for why that
matters (a category like "Domestic violence / 498A" spans both HIGH and CRITICAL FIRs).

offense_count cross-validation: see verify_accused_features_cross_validation.py (companion
script, run separately) for the match-rate check against accused_persons.prior_offense_count.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

from feature_builder import AccusedCase, Offense, build_accused_features, write_feature_csv

SEVERITY_ORDINAL = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

ALL_ACCUSED_QUERY = "MATCH (a:Accused) RETURN a.accused_id AS accused_id"

# Inner join on OF_TYPE is safe — confirmed every FIR has exactly one such edge (see module
# docstring) — so this never silently drops an offense for lack of a CrimeType link.
OFFENSE_QUERY = """
MATCH (a:Accused)-[:ACCUSED_IN]->(f:FIR)-[:OF_TYPE]->(ct:CrimeType)
RETURN a.accused_id AS accused_id, f.fir_id AS fir_id, f.date_filed AS date_filed,
       f.district AS district, f.crime_type AS crime_type, ct.severity_level AS severity_level
"""

# Undirected pattern required — ASSOCIATED_WITH is stored as one directed edge per pair, not
# both directions (see module docstring). OPTIONAL MATCH so a genuinely isolated accused
# (co_accused_count = 0) still appears with a row instead of being dropped.
CO_ACCUSED_QUERY = """
MATCH (a:Accused)
OPTIONAL MATCH (a)-[:ASSOCIATED_WITH]-(other:Accused)
RETURN a.accused_id AS accused_id, count(DISTINCT other) AS co_accused_count
"""


def fetch_accused_cases(driver, database: str) -> list[AccusedCase]:
    """Runs the 3 Cypher queries above and assembles one AccusedCase per Accused node —
    every accused gets a row even if they have zero offenses or zero co-accused."""
    with driver.session(database=database) as session:
        all_ids = [r["accused_id"] for r in session.run(ALL_ACCUSED_QUERY)]
        offense_rows = list(session.run(OFFENSE_QUERY))
        co_accused_by_id = {
            r["accused_id"]: r["co_accused_count"] for r in session.run(CO_ACCUSED_QUERY)
        }

    offenses_by_accused: dict[str, list[Offense]] = defaultdict(list)
    for row in offense_rows:
        offenses_by_accused[row["accused_id"]].append(
            Offense(
                fir_id=row["fir_id"],
                date_of_incident=date.fromisoformat(row["date_filed"]),
                district_id=row["district"],
                crime_type=row["crime_type"],
                modus_operandi=None,
                crime_type_severity=SEVERITY_ORDINAL.get(row["severity_level"]),
            )
        )

    return [
        AccusedCase(
            accused_id=accused_id,
            offenses=tuple(offenses_by_accused.get(accused_id, ())),
            co_accused_count=co_accused_by_id.get(accused_id, 0),
        )
        for accused_id in all_ids
    ]


def strip_unavailable_feature(rows: list[dict]) -> list[dict]:
    """Drops modus_operandi_consistency from the output rows — see module docstring."""
    return [{k: v for k, v in row.items() if k != "modus_operandi_consistency"} for row in rows]


def main():
    parser = argparse.ArgumentParser(
        description="AparadhKavach Day 7 — build ACCUSED_FEATURES (6/7 features) from local Neo4j"
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--output-csv", type=Path, default=Path("accused_features.csv"))
    args = parser.parse_args()

    load_dotenv(dotenv_path=args.env_file)
    uri = os.environ.get("NEO4J_URI")
    username = os.environ.get("NEO4J_USERNAME")
    password = os.environ.get("NEO4J_PASSWORD")
    if not all([uri, username, password]):
        print(
            "ERROR: NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD must all be set "
            f"(via {args.env_file} or the environment). See .env.example.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[neo4j_accused_features_driver] connecting to {uri} as {username}")
    driver = GraphDatabase.driver(uri, auth=(username, password))
    driver.verify_connectivity()

    try:
        print("[neo4j_accused_features_driver] fetching accused offense histories from Neo4j")
        cases = fetch_accused_cases(driver, args.database)
        print(f"[neo4j_accused_features_driver] {len(cases)} accused fetched")

        rows = build_accused_features(cases, crime_type_severity={}, reference_date=date.today())
        rows = strip_unavailable_feature(rows)

        write_feature_csv(rows, args.output_csv)
        print(f"[neo4j_accused_features_driver] wrote {len(rows)} rows -> {args.output_csv}")
        print(
            "[neo4j_accused_features_driver] NOTE: modus_operandi_consistency omitted — "
            "not a Neo4j property (see module docstring). 6/7 features shipped."
        )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
