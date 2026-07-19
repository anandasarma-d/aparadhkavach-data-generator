#!/usr/bin/env python3
"""AparadhKavach synthetic dataset — Level 1 Statistical Validation gate.

Spec source: Notion Section 4.7 ("Level 1 — Statistical Validation
(guardrail_validator.py)") and Section 4.6 ("Ethical Guardrails - Technical
Implementation" — "not a manual review... automated gate"), fetched fresh
2026-07-14.

This is a HARD GATE, not advisory: exits 1 (reject) if any check fails.
Per this repo's AGENTS.md / Cursor rules, a failing check must never be
"fixed" by loosening the threshold here - the fix belongs in
generate_entities.py / weave_relationships.py's sampling logic, or a
different --seed. This script will not adjust its own thresholds.

Flagged spec gap (not silently resolved): Section 4.7's Level 1 table
includes an 11th check, "Cross-regime narrative similarity", which requires
cosine similarity over Voyage AI embeddings in PgVector. Per Section 4.5's
own Flow A / Phase ordering, guardrail_validator.py runs *before* Phase 4
(PgVector population) - the embeddings this check needs do not exist yet at
the point this gate runs. That check is reported below as SKIPPED with an
explanation, and does NOT affect the pass/fail gate. It should be re-run for
real once Phase 4 (embedding ingestion) exists later in the pipeline.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from generate_entities import (
    BNS_TRANSITION_DATE,
    CATEGORY_WEIGHTS,
    EVENT_CONTEXTS,
    KARNATAKA_DISTRICTS,
)

CHI2_ALPHA = 0.05
CRIME_DIST_TOLERANCE_PCT = 3.0
REPEAT_OFFENDER_RANGE = (12.0, 18.0)
CROSS_DISTRICT_RANGE = (6.0, 10.0)
DECLUSTER_MIN_STDDEV_DAYS = 5.0
N_DISTRICTS_EXPECTED = 31

# Fields the generator always populates in Phase 1 and that must never be
# null/empty (a null here indicates a generation bug). Deliberately excludes
# risk_score, risk_score_updated_at, first_offense_date, last_offense_date -
# these are intentional Phase 1 stubs (populated later by the QuickML risk
# scorer / repeat-offender linkage), not generation bugs. See
# generate_entities.py's build_accused().
REQUIRED_FIELDS = {
    "firs": ["fir_id", "fir_number", "district", "police_station", "date_filed",
              "crime_type", "legal_code", "sections_cited", "status", "narrative_text"],
    "accused": ["accused_id", "name", "age", "age_group", "gender", "address_district"],
    "victims": ["victim_id", "name", "age", "age_group", "gender", "address_district"],
    "witnesses": ["witness_id", "name", "statement_summary"],
    "locations": ["location_id", "district", "location_name", "location_type", "lat", "lon"],
    "vehicles": ["vehicle_id", "registration_number", "vehicle_type"],
    "phone_numbers": ["phone_id", "number", "carrier"],
    # "district" deliberately excluded: Section 4.9 (added 15 Jul 2026) makes
    # it legitimately null for state-wide-scoped officers (ANALYST,
    # POLICYMAKER, and half of SUPERVISOR) - district=None IS the
    # state-wide-scope signal (Section 5.8 convention), not a generation bug.
    "officers": ["officer_id", "name", "rank", "roles"],
    "crime_types": ["type_id", "category", "subcategory", "ipc_section", "bns_section"],
}


class CheckResult:
    def __init__(self, name: str, passed: bool, detail: str):
        self.name = name
        self.passed = passed
        self.detail = detail


# ---------------------------------------------------------------------------
# chi-square (self-contained, no scipy dependency)
# ---------------------------------------------------------------------------

def _log_gammainc_terms(a: float, x: float) -> tuple[float, float]:
    return -x + a * math.log(x), math.lgamma(a)


def _gammainc_lower_reg_series(a: float, x: float) -> float:
    ap = a
    summ = 1.0 / a
    delta = summ
    for _ in range(1000):
        ap += 1
        delta *= x / ap
        summ += delta
        if abs(delta) < abs(summ) * 1e-14:
            break
    log_pref, log_gamma_a = _log_gammainc_terms(a, x)
    return summ * math.exp(log_pref - log_gamma_a)


def _gammainc_upper_reg_cf(a: float, x: float) -> float:
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    log_pref, log_gamma_a = _log_gammainc_terms(a, x)
    return math.exp(log_pref - log_gamma_a) * h


def chi2_sf(stat: float, dof: int) -> float:
    """Survival function (1 - CDF) of the chi-square distribution."""
    if stat <= 0 or dof <= 0:
        return 1.0
    a = dof / 2.0
    x = stat / 2.0
    if x < a + 1.0:
        p = _gammainc_lower_reg_series(a, x)
        return max(0.0, min(1.0, 1.0 - p))
    q = _gammainc_upper_reg_cf(a, x)
    return max(0.0, min(1.0, q))


def chi_square_independence(rows: list[str], cols: list[str], observed: dict[tuple[str, str], int]) -> tuple[float, int, float]:
    row_totals = {r: sum(observed.get((r, c), 0) for c in cols) for r in rows}
    col_totals = {c: sum(observed.get((r, c), 0) for r in rows) for c in cols}
    grand_total = sum(row_totals.values())
    if grand_total == 0:
        return 0.0, 0, 1.0
    stat = 0.0
    for r in rows:
        for c in cols:
            expected = row_totals[r] * col_totals[c] / grand_total
            if expected <= 0:
                continue
            obs = observed.get((r, c), 0)
            stat += (obs - expected) ** 2 / expected
    dof = (len(rows) - 1) * (len(cols) - 1)
    p = chi2_sf(stat, dof) if dof > 0 else 1.0
    return stat, dof, p


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------

def load_entities(entities_dir: Path) -> dict:
    data = {}
    for name in REQUIRED_FIELDS:
        with (entities_dir / f"{name}.json").open() as f:
            data[name] = json.load(f)
    return data


def load_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def load_relationships(rel_dir: Path) -> dict:
    return {
        "accused_in": load_csv(rel_dir / "accused_in.csv"),
    }


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_crime_type_distribution(firs: list[dict]) -> CheckResult:
    total = len(firs)
    counts = defaultdict(int)
    for f in firs:
        counts[f["crime_type"]] += 1
    lines = []
    all_ok = True
    for category, target_pct in CATEGORY_WEIGHTS.items():
        actual_pct = counts.get(category, 0) / total * 100 if total else 0
        diff = abs(actual_pct - target_pct)
        ok = diff <= CRIME_DIST_TOLERANCE_PCT
        all_ok = all_ok and ok
        lines.append(f"{category}: target {target_pct:.1f}%, actual {actual_pct:.1f}% "
                      f"(diff {diff:.1f}pp) {'OK' if ok else 'FAIL'}")
    return CheckResult("Crime type distribution (±3% of target)", all_ok, "; ".join(lines))


def check_repeat_offender_rate(accused: list[dict], accused_in: list[dict]) -> CheckResult:
    counts = defaultdict(int)
    for row in accused_in:
        counts[row["accused_id"]] += 1
    repeat = sum(1 for c in counts.values() if c >= 2)
    rate = repeat / len(accused) * 100 if accused else 0
    lo, hi = REPEAT_OFFENDER_RANGE
    ok = lo <= rate <= hi
    return CheckResult("Repeat offender rate (12-18%, target ~15%)", ok,
                        f"{repeat}/{len(accused)} = {rate:.2f}%")


def check_cross_district_rate(accused: list[dict], accused_in: list[dict], firs: list[dict]) -> CheckResult:
    fir_district = {f["fir_id"]: f["district"] for f in firs}
    districts_by_accused = defaultdict(set)
    for row in accused_in:
        d = fir_district.get(row["fir_id"])
        if d:
            districts_by_accused[row["accused_id"]].add(d)
    cross = sum(1 for dset in districts_by_accused.values() if len(dset) >= 2)
    rate = cross / len(accused) * 100 if accused else 0
    lo, hi = CROSS_DISTRICT_RANGE
    ok = lo <= rate <= hi
    return CheckResult("Cross-district accused rate (6-10%, target ~8%)", ok,
                        f"{cross}/{len(accused)} = {rate:.2f}%")


def check_event_context_coverage(firs: list[dict]) -> CheckResult:
    counts = defaultdict(int)
    for f in firs:
        counts[f["event_context"]] += 1
    present = set(counts.keys())
    missing = set(EVENT_CONTEXTS) - present
    largest = max(counts, key=counts.get) if counts else None
    ok = not missing and largest == "NONE"
    detail = f"present={sorted(present)}, missing={sorted(missing)}, largest_bucket={largest} ({counts.get(largest, 0)})"
    return CheckResult("Event context coverage (all 8 present, NONE largest)", ok, detail)


def check_demographic_independence(accused: list[dict], accused_in: list[dict], firs: list[dict]) -> CheckResult:
    accused_gender = {a["accused_id"]: a["gender"] for a in accused}
    accused_age_group = {a["accused_id"]: a["age_group"] for a in accused}
    fir_crime_type = {f["fir_id"]: f["crime_type"] for f in firs}

    categories = list(CATEGORY_WEIGHTS.keys())
    genders = sorted({g for g in accused_gender.values()})
    age_groups = sorted({g for g in accused_age_group.values()})

    gender_obs = defaultdict(int)
    age_obs = defaultdict(int)
    for row in accused_in:
        crime_type = fir_crime_type.get(row["fir_id"])
        gender = accused_gender.get(row["accused_id"])
        age_group = accused_age_group.get(row["accused_id"])
        if crime_type is None:
            continue
        if gender:
            gender_obs[(crime_type, gender)] += 1
        if age_group:
            age_obs[(crime_type, age_group)] += 1

    stat_g, dof_g, p_gender = chi_square_independence(categories, genders, gender_obs)
    stat_a, dof_a, p_age = chi_square_independence(categories, age_groups, age_obs)

    ok = p_gender > CHI2_ALPHA and p_age > CHI2_ALPHA
    detail = (f"crime_type x gender: chi2={stat_g:.2f} dof={dof_g} p={p_gender:.4f} "
              f"({'OK' if p_gender > CHI2_ALPHA else 'FAIL'}); "
              f"crime_type x age_group: chi2={stat_a:.2f} dof={dof_a} p={p_age:.4f} "
              f"({'OK' if p_age > CHI2_ALPHA else 'FAIL'})")
    return CheckResult("Demographic independence (chi-square p > 0.05)", ok, detail)


def check_all_districts_represented(firs: list[dict]) -> CheckResult:
    districts = {f["district"] for f in firs}
    ok = len(districts) == N_DISTRICTS_EXPECTED
    return CheckResult(f"All districts represented ({N_DISTRICTS_EXPECTED})", ok,
                        f"{len(districts)} distinct districts in FIR table")


def check_temporal_distribution(firs: list[dict]) -> CheckResult:
    counts = defaultdict(int)
    for f in firs:
        d = date.fromisoformat(f["date_filed"])
        counts[(d.year, d.month)] += 1
    if not counts:
        return CheckResult("Temporal distribution (no zero months; peaks visible)", False, "no FIRs")
    all_months = sorted(counts.keys())
    span_months = []
    y, m = all_months[0]
    end_y, end_m = all_months[-1]
    while (y, m) <= (end_y, end_m):
        span_months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    zero_months = [ym for ym in span_months if counts.get(ym, 0) == 0]
    values = list(counts.values())
    mean_v = statistics.mean(values)
    max_v = max(values)
    has_peak = max_v >= mean_v * 1.10
    ok = not zero_months and has_peak
    detail = f"{len(span_months)} months spanned, {len(zero_months)} zero-FIR months, mean={mean_v:.1f}/mo, max={max_v} ({'peak visible' if has_peak else 'flat'})"
    return CheckResult("Temporal distribution (no zero months; peaks visible)", ok, detail)


def check_fir_date_declustering(firs: list[dict]) -> CheckResult:
    days_by_month = defaultdict(list)
    for f in firs:
        d = date.fromisoformat(f["date_filed"])
        days_by_month[(d.year, d.month)].append(d.day)

    failing = []
    skipped = []
    for (y, m), days in sorted(days_by_month.items()):
        if len(days) < 2:
            skipped.append((y, m))
            continue
        sd = statistics.pstdev(days)
        if sd <= DECLUSTER_MIN_STDDEV_DAYS:
            failing.append((y, m, sd))
    ok = not failing
    detail = f"{len(days_by_month) - len(failing) - len(skipped)}/{len(days_by_month)} months pass (std > {DECLUSTER_MIN_STDDEV_DAYS}d)"
    if failing:
        worst = sorted(failing, key=lambda t: t[2])[:5]
        detail += "; failing (worst 5): " + ", ".join(f"{y}-{m:02d} std={sd:.2f}" for y, m, sd in worst)
    if skipped:
        detail += f"; {len(skipped)} months skipped (<2 FIRs)"
    return CheckResult(f"FIR date declustering (std dev > {DECLUSTER_MIN_STDDEV_DAYS} days/month)", ok, detail)


def check_no_null_required_fields(data: dict) -> CheckResult:
    null_counts = defaultdict(int)
    for entity_name, fields in REQUIRED_FIELDS.items():
        for record in data[entity_name]:
            for field in fields:
                value = record.get(field, None)
                if value is None or value == "" or value == []:
                    null_counts[(entity_name, field)] += 1
    total_nulls = sum(null_counts.values())
    ok = total_nulls == 0
    detail = f"{total_nulls} null/empty required-field values"
    if null_counts:
        worst = sorted(null_counts.items(), key=lambda kv: -kv[1])[:5]
        detail += "; worst: " + ", ".join(f"{e}.{fld}={c}" for (e, fld), c in worst)
    return CheckResult("No null required fields", ok, detail)


def check_legal_code_date_consistency(firs: list[dict]) -> CheckResult:
    mismatches = 0
    for f in firs:
        d = date.fromisoformat(f["date_filed"])
        expected = "IPC" if d < BNS_TRANSITION_DATE else "BNS"
        if f["legal_code"] != expected:
            mismatches += 1
    ok = mismatches == 0
    return CheckResult("Legal code / date_filed consistency (100% match)", ok,
                        f"{mismatches}/{len(firs)} mismatches (transition {BNS_TRANSITION_DATE.isoformat()})")


def check_cross_regime_narrative_similarity_SKIPPED() -> CheckResult:
    detail = ("SKIPPED - requires cosine similarity over Voyage AI embeddings in "
              "PgVector (Phase 4), which do not exist at this pipeline stage per "
              "Section 4.5 Flow A ordering (guardrail runs before Phase 4). Not "
              "counted toward pass/fail. Re-run this specific check once Phase 4 "
              "(embedding ingestion) exists.")
    result = CheckResult("Cross-regime narrative similarity (0.65-0.85 band)", True, detail)
    result.skipped = True
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AparadhKavach Level 1 statistical validation gate")
    parser.add_argument("--entities-dir", type=Path, default=Path("data/entities"))
    parser.add_argument("--relationships-dir", type=Path, default=Path("data/relationships"))
    args = parser.parse_args()

    data = load_entities(args.entities_dir)
    rels = load_relationships(args.relationships_dir)

    checks = [
        check_crime_type_distribution(data["firs"]),
        check_repeat_offender_rate(data["accused"], rels["accused_in"]),
        check_cross_district_rate(data["accused"], rels["accused_in"], data["firs"]),
        check_event_context_coverage(data["firs"]),
        check_demographic_independence(data["accused"], rels["accused_in"], data["firs"]),
        check_all_districts_represented(data["firs"]),
        check_temporal_distribution(data["firs"]),
        check_fir_date_declustering(data["firs"]),
        check_no_null_required_fields(data),
        check_legal_code_date_consistency(data["firs"]),
    ]
    skipped_check = check_cross_regime_narrative_similarity_SKIPPED()

    print("=" * 88)
    print("AparadhKavach guardrail_validator.py - Level 1 Statistical Validation (Section 4.7)")
    print("=" * 88)
    all_passed = True
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        if not c.passed:
            all_passed = False
        print(f"[{status}] {c.name}")
        print(f"       {c.detail}")
    print(f"[SKIP] {skipped_check.name}")
    print(f"       {skipped_check.detail}")
    print("=" * 88)

    if all_passed:
        print("RESULT: PASS - all gated Level 1 checks passed. Safe to proceed to Phase 3 (Neo4j population).")
        sys.exit(0)
    else:
        print("RESULT: REJECTED - one or more Level 1 checks failed.")
        print("This is a hard gate: do NOT loosen a threshold above to force a pass.")
        print("Fix generate_entities.py / weave_relationships.py's sampling logic, or re-run")
        print("both scripts with a different --seed, then re-run this validator.")
        sys.exit(1)


if __name__ == "__main__":
    main()
