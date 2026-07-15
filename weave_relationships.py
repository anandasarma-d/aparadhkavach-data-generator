#!/usr/bin/env python3
"""AparadhKavach synthetic dataset — Phase 2: Relationship Weaving.

Spec source: Notion Section 4.5 Phase 2 pseudocode, Section 5.2 (conceptual
relationships), 5.3 (relationship attributes/cardinalities), fetched fresh
2026-07-14. Reads the Phase 1 JSON entity files written by
generate_entities.py and writes one CSV per relationship type (11 total).

Only two relationship-weaving statistics are actually gated by
guardrail_validator.py's Level 1 checks (Section 4.7): repeat-offender rate
(12-18%, target ~15%) and cross-district accused rate (6-10%, target ~8%),
both structural properties of ACCUSED_IN. Every other cardinality below
(witness coverage, location sharing, vehicle/phone sharing, cross-FIR phone
contacts) is implemented to match Section 4.5 Phase 2's stated percentages
as closely as a single generation pass reasonably allows, but is not itself
part of the automated gate (those are Level 2/3 checks, out of Day 2 scope
per the user's instructions).

Crime-type independence (Section 4.6) is preserved structurally here: accused
selection for ACCUSED_IN never looks at a FIR's crime_type/category when
choosing which accused to attach - selection is uniform-random over the
eligible accused pool regardless of the FIR's crime type.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROLE_IN_CASE = ["primary", "secondary", "suspect"]
WARRANT_STATUS = ["NONE", "ISSUED", "PENDING", "EXECUTED"]
INJURY_SEVERITY = ["NONE", "MINOR", "MODERATE", "SEVERE"]
COMPLAINT_TYPE = ["SELF", "FAMILY_MEMBER", "THIRD_PARTY", "POLICE_SUO_MOTU"]
STATEMENT_RELIABILITY = ["HIGH", "MEDIUM", "LOW"]
OWNERSHIP_TYPE = ["registered", "used"]

DV_CATEGORY = "Domestic violence / 498A"


def load_entities(entities_dir: Path) -> dict:
    data = {}
    for name in ["crime_types", "locations", "officers", "accused", "victims",
                 "witnesses", "vehicles", "phone_numbers", "firs"]:
        with (entities_dir / f"{name}.json").open() as f:
            data[name] = json.load(f)
    return data


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"  wrote {len(rows):>6} rows -> {path}")


def random_date_between(d1: date, d2: date) -> date:
    if d2 < d1:
        d1, d2 = d2, d1
    span = (d2 - d1).days
    return d1 + timedelta(days=random.randint(0, span)) if span > 0 else d1


# ---------------------------------------------------------------------------
# ACCUSED_IN  (+ prior_offense_count/first/last_offense_date bookkeeping used
# only to report repeat-offender stats here; entity JSON files are NOT
# rewritten - Section 4.5 Phase 3 treats Phase 1's entity JSON as fixed.)
# ---------------------------------------------------------------------------

def weave_accused_in(accused: list[dict], firs: list[dict], repeat_rate: float,
                      cross_district_rate: float, cap: int = 3) -> list[dict]:
    fir_district = {f["fir_id"]: f["district"] for f in firs}
    firs_by_district: dict[str, list[str]] = defaultdict(list)
    for f in firs:
        firs_by_district[f["district"]].append(f["fir_id"])

    fir_slots: dict[str, list[str]] = {f["fir_id"]: [] for f in firs}
    all_fir_ids = list(fir_slots.keys())

    accused_ids = [a["accused_id"] for a in accused]
    random.shuffle(accused_ids)

    n_repeat = round(len(accused_ids) * repeat_rate)
    n_cross = round(len(accused_ids) * cross_district_rate)
    n_cross = min(n_cross, n_repeat)  # cross-district accused are a subset of repeat offenders
    repeat_ids = set(accused_ids[:n_repeat])
    cross_ids_set = set(accused_ids[:n_cross])
    # Which accused end up repeat/cross is still seed-determined (drawn from
    # accused_ids, itself seeded via random.shuffle above) - only the
    # *iteration order* below needs to be deterministic. Python's string
    # hash randomization (PYTHONHASHSEED, on by default per-process) means
    # two set objects with identical elements can iterate in a different
    # order in different process runs even with the same random seed, which
    # would change which accused gets role_in_case "primary" and the
    # sequence of random.choice() draws in the loops below. Sorting fixes
    # both loops (Defect Tracker: weave_relationships.py non-deterministic
    # set iteration).
    same_district_repeat_ids = sorted(repeat_ids - cross_ids_set)
    cross_ids = sorted(cross_ids_set)
    non_repeat_ids = [a for a in accused_ids if a not in repeat_ids]

    def has_room(fir_id: str) -> bool:
        return len(fir_slots[fir_id]) < cap

    def place(accused_id: str, fir_id: str) -> None:
        fir_slots[fir_id].append(accused_id)

    def pick_any_fir_with_room(max_tries: int = 200) -> str | None:
        for _ in range(max_tries):
            fir_id = random.choice(all_fir_ids)
            if has_room(fir_id):
                return fir_id
        # fallback: linear scan
        for fir_id in all_fir_ids:
            if has_room(fir_id):
                return fir_id
        return None

    # Round-robin cursor over a shuffled FIR order, used only for the
    # non-repeat primary pass below. Uniform random.choice() for ~3791
    # draws over ~3720 FIRs leaves a meaningful fraction of FIRs empty
    # (coupon-collector effect) and forces the coverage backstop (step 4)
    # to reuse already-placed accused, artificially inflating the
    # repeat-offender rate. Round-robin guarantees near-even coverage
    # instead, while accused-to-FIR pairing is still effectively random
    # because non_repeat_ids itself is drawn from a pre-shuffled list.
    _rr_order = list(all_fir_ids)
    random.shuffle(_rr_order)
    _rr_cursor = [0]

    def pick_round_robin_fir_with_room() -> str | None:
        n = len(_rr_order)
        for _ in range(n):
            fir_id = _rr_order[_rr_cursor[0]]
            _rr_cursor[0] = (_rr_cursor[0] + 1) % n
            if has_room(fir_id):
                return fir_id
        return None

    def pick_fir_in_district_with_room(district: str, exclude: set | None = None, max_tries: int = 200) -> str | None:
        candidates = firs_by_district.get(district, [])
        if not candidates:
            return None
        for _ in range(max_tries):
            fir_id = random.choice(candidates)
            if has_room(fir_id) and (not exclude or fir_id not in exclude):
                return fir_id
        for fir_id in candidates:
            if has_room(fir_id) and (not exclude or fir_id not in exclude):
                return fir_id
        return None

    def pick_fir_in_other_district_with_room(exclude_district: str, max_tries: int = 200) -> str | None:
        other_districts = [d for d in firs_by_district if d != exclude_district]
        for _ in range(max_tries):
            district = random.choice(other_districts)
            fir_id = pick_fir_in_district_with_room(district, max_tries=10)
            if fir_id:
                return fir_id
        return pick_any_fir_with_room()

    # 1) Non-repeat accused: exactly one FIR each. Round-robin FIR coverage,
    # random accused order (crime-type independent either way).
    for accused_id in non_repeat_ids:
        fir_id = pick_round_robin_fir_with_room()
        if fir_id:
            place(accused_id, fir_id)

    # 2) Same-district repeat offenders: 2 FIRs in the same district
    for accused_id in same_district_repeat_ids:
        district = random.choice(list(firs_by_district.keys()))
        fir1 = pick_fir_in_district_with_room(district)
        fir2 = pick_fir_in_district_with_room(district, exclude={fir1} if fir1 else None)
        for fir_id in (fir1, fir2):
            if fir_id:
                place(accused_id, fir_id)

    # 3) Cross-district repeat offenders: 2 FIRs in different districts
    for accused_id in cross_ids:
        fir1 = pick_any_fir_with_room()
        if not fir1:
            continue
        fir2 = pick_fir_in_other_district_with_room(fir_district[fir1])
        for fir_id in (fir1, fir2):
            if fir_id:
                place(accused_id, fir_id)

    # 4) Backstop: guarantee every FIR has >= 1 accused (ignore cap if needed)
    unfilled = [fid for fid, lst in fir_slots.items() if len(lst) == 0]
    if unfilled:
        for fir_id in unfilled:
            filler = random.choice(accused_ids)
            fir_slots[fir_id].append(filler)

    rows = []
    for fir in firs:
        fir_id = fir["fir_id"]
        date_filed = date.fromisoformat(fir["date_filed"])
        acc_list = fir_slots[fir_id]
        for i, accused_id in enumerate(acc_list):
            rows.append({
                "accused_id": accused_id,
                "fir_id": fir_id,
                "role_in_case": "primary" if i == 0 else random.choice(ROLE_IN_CASE[1:]),
                "date_added": date_filed.isoformat(),
                "warrant_status": random.choices(WARRANT_STATUS, weights=[70, 15, 10, 5], k=1)[0],
            })
    return rows


# ---------------------------------------------------------------------------
# VICTIM_IN
# ---------------------------------------------------------------------------

def weave_victim_in(victims: list[dict], firs: list[dict]) -> list[dict]:
    """Defect Tracker: weave_relationships.py (VICTIM_IN/WITNESSED
    coupon-collector coverage gap). The old version drew every slot via
    random.choice(all_ids), which - like ACCUSED_IN before its Day 2 fix -
    leaves a meaningful fraction of the pool never drawn at all even when
    total draws roughly match or exceed pool size (coupon collector
    problem). Fixed the same way ACCUSED_IN was: round-robin assignment
    guarantees every victim is used at least once before any repeats.

    DV (498A/304B) female-skew (Section 4.6) is applied as a *post-hoc
    swap* on top of the round-robin assignment rather than a per-draw
    coin flip: swapping two already-assigned slots' victims can never
    reduce coverage (the same multiset of victims stays assigned, just
    redistributed across FIRs), so the coverage guarantee holds
    regardless of how the swap pass behaves.
    """
    victim_ids = [v["victim_id"] for v in victims]
    gender_by_id = {v["victim_id"]: v["gender"] for v in victims}
    random.shuffle(victim_ids)

    slot_firs: list[dict] = []
    for fir in firs:
        k = random.choices([1, 2], weights=[75, 25], k=1)[0]
        slot_firs.extend([fir] * k)

    n_slots = len(slot_firs)
    # Round-robin coverage: slot i gets victim_ids[i % len(victim_ids)].
    # Wraparound (n_slots > len(victim_ids)) is exactly Section 5.3's
    # "one victim can appear in multiple FIRs (rare but possible)" -
    # spread evenly by construction instead of a bolted-on random-reuse
    # chance.
    assigned = [victim_ids[i % len(victim_ids)] for i in range(n_slots)]
    if n_slots < len(victim_ids):
        print(f"    ! only {n_slots} victim slots for {len(victim_ids)} victims - "
              f"{len(victim_ids) - n_slots} victims cannot be covered this run")

    # DV female-skew swap pass.
    dv_slot_indices = [i for i in range(n_slots) if slot_firs[i]["crime_type"] == DV_CATEGORY]
    dv_slot_set = set(dv_slot_indices)  # membership checks only, never iterated
    female_donor_queue = [i for i in range(n_slots)
                           if i not in dv_slot_set and gender_by_id[assigned[i]] == "FEMALE"]
    donor_ptr = 0
    for dv_idx in dv_slot_indices:
        if gender_by_id[assigned[dv_idx]] == "FEMALE":
            continue
        if random.random() >= 0.85:
            continue
        if donor_ptr >= len(female_donor_queue):
            continue  # no more female donors available - best-effort skew, not a hard guarantee
        donor_idx = female_donor_queue[donor_ptr]
        donor_ptr += 1
        assigned[dv_idx], assigned[donor_idx] = assigned[donor_idx], assigned[dv_idx]

    rows = []
    for fir, victim_id in zip(slot_firs, assigned):
        rows.append({
            "victim_id": victim_id,
            "fir_id": fir["fir_id"],
            "injury_severity": random.choices(INJURY_SEVERITY, weights=[40, 30, 20, 10], k=1)[0],
            "complaint_type": random.choices(COMPLAINT_TYPE, weights=[55, 25, 15, 5], k=1)[0],
        })
    return rows


# ---------------------------------------------------------------------------
# WITNESSED
# ---------------------------------------------------------------------------

def weave_witnessed(witnesses: list[dict], firs: list[dict], coverage: float = 0.40) -> list[dict]:
    """Defect Tracker: weave_relationships.py (VICTIM_IN/WITNESSED
    coupon-collector coverage gap) - same round-robin-before-repeats fix
    as weave_victim_in() above and weave_accused_in()'s Day 2 fix."""
    witness_ids = [w["witness_id"] for w in witnesses]
    random.shuffle(witness_ids)

    n_target = round(len(firs) * coverage)
    chosen_firs = random.sample(firs, k=min(n_target, len(firs)))

    slot_firs: list[dict] = []
    for fir in chosen_firs:
        k = random.choices([1, 2], weights=[70, 30], k=1)[0]
        slot_firs.extend([fir] * k)

    n_slots = len(slot_firs)
    assigned = [witness_ids[i % len(witness_ids)] for i in range(n_slots)]
    if n_slots < len(witness_ids):
        print(f"    ! only {n_slots} witness slots for {len(witness_ids)} witnesses - "
              f"{len(witness_ids) - n_slots} witnesses cannot be covered this run")

    rows = []
    for fir, witness_id in zip(slot_firs, assigned):
        date_filed = date.fromisoformat(fir["date_filed"])
        statement_date = date_filed + timedelta(days=random.randint(0, 10))
        rows.append({
            "witness_id": witness_id,
            "fir_id": fir["fir_id"],
            "statement_date": statement_date.isoformat(),
            "statement_reliability": random.choices(STATEMENT_RELIABILITY, weights=[50, 35, 15], k=1)[0],
        })
    return rows


# ---------------------------------------------------------------------------
# OCCURRED_AT
# ---------------------------------------------------------------------------

def weave_occurred_at(locations: list[dict], firs: list[dict], hotspot_fraction: float = 0.20,
                       hotspot_min_incidents: int = 4, hotspot_max_incidents: int = 8,
                       normal_cap: int = 3) -> list[dict]:
    """Defect Tracker: weave_relationships.py (OCCURRED_AT hotspot
    draw-split mismatch). The old version drew from the hotspot subset vs.
    the full per-district pool via a flat 50/50 coin flip. Two problems
    followed: (1) the full "pool" branch always included the hotspot
    locations too, so hotspots got hit from *both* branches and badly
    overshot the >3-incidents-each hotspot definition (304 actual vs. ~224
    target = 20% of 1,120); (2) plain random.choice() over the ~80% normal
    locations left a coupon-collector-style gap - 172 locations never drawn
    at all (isolated nodes in Neo4j).

    Fixed with an explicit per-district budget instead of a coin flip:
    each hotspot location gets a reserved incident count randomly drawn
    from [hotspot_min_incidents, hotspot_max_incidents] (comfortably over
    the >3 threshold without being absurd), and every remaining FIR is
    distributed round-robin across the normal (non-hotspot) locations,
    capped at `normal_cap` each so they don't accidentally cross the
    hotspot threshold themselves - closing the same coupon-collector gap
    class as the VICTIM_IN/WITNESSED fix above. Only genuine overflow (a
    district with far more FIRs than 3-per-normal-location can absorb)
    spills back onto the hotspot locations, which is the correct place for
    a district's "extra" incident density to land.
    """
    locations_by_district: dict[str, list[str]] = defaultdict(list)
    for loc in locations:
        locations_by_district[loc["district"]].append(loc["location_id"])

    firs_by_district: dict[str, list[dict]] = defaultdict(list)
    for fir in firs:
        firs_by_district[fir["district"]].append(fir)

    assignment: dict[str, str] = {}

    for district, loc_ids in locations_by_district.items():
        loc_ids = list(loc_ids)
        random.shuffle(loc_ids)
        district_firs = firs_by_district.get(district, [])
        n_firs = len(district_firs)
        if n_firs == 0 or not loc_ids:
            continue

        n_hot = max(1, round(len(loc_ids) * hotspot_fraction))
        hot_locs = loc_ids[:n_hot]
        normal_locs = loc_ids[n_hot:]

        hotspot_targets = [random.randint(hotspot_min_incidents, hotspot_max_incidents) for _ in hot_locs]
        total_hotspot_budget = sum(hotspot_targets)
        if total_hotspot_budget > n_firs:
            # Sparse district - this district's FIR supply can't fill even
            # the minimum hotspot reservation; scale down proportionally
            # rather than starving every other location in the district.
            scale = n_firs / total_hotspot_budget
            hotspot_targets = [max(1, int(t * scale)) for t in hotspot_targets]
            total_hotspot_budget = sum(hotspot_targets)

        location_queue: list[str] = []
        for loc_id, target in zip(hot_locs, hotspot_targets):
            location_queue.extend([loc_id] * target)

        remaining = n_firs - len(location_queue)

        if normal_locs and remaining > 0:
            per_location_count: dict[str, int] = defaultdict(int)
            i = 0
            n_normal = len(normal_locs)
            while remaining > 0 and any(per_location_count[loc] < normal_cap for loc in normal_locs):
                loc_id = normal_locs[i % n_normal]
                i += 1
                if per_location_count[loc_id] < normal_cap:
                    location_queue.append(loc_id)
                    per_location_count[loc_id] += 1
                    remaining -= 1

        overflow_pool = hot_locs or normal_locs
        if remaining > 0 and overflow_pool:
            j = 0
            n_overflow = len(overflow_pool)
            while remaining > 0:
                location_queue.append(overflow_pool[j % n_overflow])
                j += 1
                remaining -= 1

        # Shuffle so hotspot-vs-normal assignment doesn't correlate with
        # district_firs's original (roughly chronological) order.
        random.shuffle(location_queue)
        for fir, location_id in zip(district_firs, location_queue):
            assignment[fir["fir_id"]] = location_id

    all_location_ids = [l["location_id"] for l in locations]
    rows = []
    for fir in firs:
        district = fir["district"]
        location_id = assignment.get(fir["fir_id"])
        if location_id is None:
            # Fallback: FIR's district had no locations at all (shouldn't
            # happen - Section 4.2 guarantees every district gets
            # locations) - pick from the global pool so no FIR is left
            # without a location.
            location_id = random.choice(all_location_ids)
        rows.append({
            "fir_id": fir["fir_id"],
            "location_id": location_id,
            "exact_address": f"Near {location_id}, {district}",
            "landmark": random.choice(["Bus Stand", "Market", "Temple", "School", "Petrol Bunk", "Circle"]),
        })
    return rows


# ---------------------------------------------------------------------------
# INVESTIGATED_BY
# ---------------------------------------------------------------------------

def weave_investigated_by(officers: list[dict], firs: list[dict]) -> list[dict]:
    officers_by_district: dict[str, list[str]] = defaultdict(list)
    for o in officers:
        officers_by_district[o["district"]].append(o["officer_id"])

    rows = []
    for fir in firs:
        district = fir["district"]
        pool = officers_by_district.get(district) or [o["officer_id"] for o in officers]
        officer_id = random.choice(pool)
        date_filed = date.fromisoformat(fir["date_filed"])
        rows.append({
            "fir_id": fir["fir_id"],
            "officer_id": officer_id,
            "assigned_date": date_filed.isoformat(),
            "is_lead_officer": True,
        })
    return rows


# ---------------------------------------------------------------------------
# OWNS (vehicle) / OWNS (phone)
# ---------------------------------------------------------------------------

def weave_owns_vehicle(accused: list[dict], vehicles: list[dict], accused_in_rows: list[dict],
                        ownership_rate: float = 0.30, shared_rate: float = 0.05) -> list[dict]:
    accused_first_fir: dict[str, str] = {}
    for row in accused_in_rows:
        accused_first_fir.setdefault(row["accused_id"], row["fir_id"])

    accused_ids = [a["accused_id"] for a in accused]
    random.shuffle(accused_ids)
    n_owners = round(len(accused_ids) * ownership_rate)
    owner_ids = accused_ids[:n_owners]

    vehicle_ids = [v["vehicle_id"] for v in vehicles]
    random.shuffle(vehicle_ids)
    n_shared = max(1, round(len(vehicle_ids) * shared_rate))
    shared_vehicles = vehicle_ids[:n_shared]
    unique_vehicles = vehicle_ids[n_shared:]

    rows = []
    unique_iter = iter(unique_vehicles)
    remaining_owners = list(owner_ids)
    random.shuffle(remaining_owners)

    # fill unique vehicles 1:1 first
    n_unique_assign = min(len(unique_vehicles), len(remaining_owners))
    for i in range(n_unique_assign):
        accused_id = remaining_owners[i]
        vehicle_id = unique_vehicles[i]
        rows.append({
            "accused_id": accused_id,
            "vehicle_id": vehicle_id,
            "ownership_type": random.choices(OWNERSHIP_TYPE, weights=[80, 20], k=1)[0],
            "link_fir_id": accused_first_fir.get(accused_id, ""),
        })
    leftover_owners = remaining_owners[n_unique_assign:]

    # spread the remaining owners across the shared-vehicle pool (>=2 owners each where possible)
    if leftover_owners and shared_vehicles:
        for i, accused_id in enumerate(leftover_owners):
            vehicle_id = shared_vehicles[i % len(shared_vehicles)]
            rows.append({
                "accused_id": accused_id,
                "vehicle_id": vehicle_id,
                "ownership_type": random.choices(OWNERSHIP_TYPE, weights=[80, 20], k=1)[0],
                "link_fir_id": accused_first_fir.get(accused_id, ""),
            })
    return rows


def weave_owns_phone(accused: list[dict], phones: list[dict], accused_in_rows: list[dict],
                      ownership_rate: float = 0.50) -> tuple[list[dict], dict[str, list[str]]]:
    accused_firs: dict[str, list[str]] = defaultdict(list)
    for row in accused_in_rows:
        accused_firs[row["accused_id"]].append(row["fir_id"])

    accused_ids = [a["accused_id"] for a in accused]
    random.shuffle(accused_ids)
    n_owners = round(len(accused_ids) * ownership_rate)
    owner_ids = accused_ids[:n_owners]

    phone_ids = [p["phone_id"] for p in phones]
    random.shuffle(phone_ids)
    n_assign = min(len(owner_ids), len(phone_ids))

    rows = []
    phone_owner: dict[str, str] = {}
    for i in range(n_assign):
        accused_id = owner_ids[i]
        phone_id = phone_ids[i]
        phone_owner[phone_id] = accused_id
        rows.append({
            "accused_id": accused_id,
            "phone_id": phone_id,
            "ownership_type": random.choices(OWNERSHIP_TYPE, weights=[75, 25], k=1)[0],
            "link_fir_id": accused_firs.get(accused_id, [""])[0],
        })
    return rows, accused_firs


# ---------------------------------------------------------------------------
# CONTACTED  (8% of phone pool participates in a cross-FIR CONTACTED edge)
# ---------------------------------------------------------------------------

def weave_contacted(phones: list[dict], owns_phone_rows: list[dict], accused_firs: dict[str, list[str]],
                     cross_fir_fraction: float = 0.08) -> list[dict]:
    phone_to_accused = {row["phone_id"]: row["accused_id"] for row in owns_phone_rows}
    owned_phone_ids = list(phone_to_accused.keys())
    random.shuffle(owned_phone_ids)

    n_active = round(len(phones) * cross_fir_fraction)
    n_active = min(n_active, len(owned_phone_ids) - (len(owned_phone_ids) % 2))
    active_phones = owned_phone_ids[:n_active]

    rows = []
    for i in range(0, len(active_phones) - 1, 2):
        phone_a, phone_b = active_phones[i], active_phones[i + 1]
        accused_a = phone_to_accused[phone_a]
        accused_b = phone_to_accused[phone_b]
        fir_ids = sorted(set(accused_firs.get(accused_a, []) + accused_firs.get(accused_b, [])))
        rows.append({
            "phone_id_1": phone_a,
            "phone_id_2": phone_b,
            "contact_date": (date(2021, 1, 1) + timedelta(days=random.randint(0, 1825))).isoformat(),
            "contact_count": random.randint(1, 25),
            "link_fir_ids": "|".join(fir_ids),
        })
    return rows


# ---------------------------------------------------------------------------
# ASSOCIATED_WITH  (derived co-accused, bidirectional, one row per pair)
# ---------------------------------------------------------------------------

def weave_associated_with(accused_in_rows: list[dict], firs: list[dict]) -> list[dict]:
    fir_date = {f["fir_id"]: f["date_filed"] for f in firs}
    accused_by_fir: dict[str, list[str]] = defaultdict(list)
    for row in accused_in_rows:
        accused_by_fir[row["fir_id"]].append(row["accused_id"])

    pair_shared_firs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for fir_id, acc_ids in accused_by_fir.items():
        acc_ids = sorted(set(acc_ids))
        for i in range(len(acc_ids)):
            for j in range(i + 1, len(acc_ids)):
                pair_shared_firs[(acc_ids[i], acc_ids[j])].append(fir_id)

    rows = []
    for (a1, a2), fir_ids in pair_shared_firs.items():
        fir_ids_sorted = sorted(fir_ids, key=lambda fid: fir_date[fid])
        rows.append({
            "accused_id_1": a1,
            "accused_id_2": a2,
            "shared_fir_count": len(fir_ids_sorted),
            "first_shared_fir_id": fir_ids_sorted[0],
            "relationship_type": "co-accused",
        })
    return rows


# ---------------------------------------------------------------------------
# LINKED_TO  (series crime: same crime category + same district, <=90 days)
# ---------------------------------------------------------------------------

def weave_linked_to(firs: list[dict], window_days: int = 90) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for fir in firs:
        groups[(fir["district"], fir["crime_type"])].append(fir)

    rows = []
    for key, group_firs in groups.items():
        group_firs.sort(key=lambda f: f["date_filed"])
        for i in range(len(group_firs)):
            d_i = date.fromisoformat(group_firs[i]["date_filed"])
            for j in range(i + 1, len(group_firs)):
                d_j = date.fromisoformat(group_firs[j]["date_filed"])
                delta = (d_j - d_i).days
                if delta > window_days:
                    break  # sorted by date - no later j can be within window either
                rows.append({
                    "fir_id_1": group_firs[i]["fir_id"],
                    "fir_id_2": group_firs[j]["fir_id"],
                    "link_type": "series_crime",
                    "link_confidence": round(random.uniform(0.6, 0.95), 2),
                })
    return rows


# ---------------------------------------------------------------------------
# OF_TYPE
# ---------------------------------------------------------------------------

def weave_of_type(firs: list[dict]) -> list[dict]:
    rows = []
    for fir in firs:
        primary_section = f"{fir['legal_code']} {fir['sections_cited'][0]}" if fir["sections_cited"] else fir["legal_code"]
        rows.append({
            "fir_id": fir["fir_id"],
            "type_id": fir["_crime_type_id"],
            "primary_section": primary_section,
        })
    return rows


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AparadhKavach Phase 2 - relationship weaving")
    parser.add_argument("--entities-dir", type=Path, default=Path("data/entities"))
    parser.add_argument("--out-dir", type=Path, default=Path("data/relationships"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repeat-rate", type=float, default=0.15)
    parser.add_argument("--cross-district-rate", type=float, default=0.08)
    args = parser.parse_args()

    random.seed(args.seed)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[weave_relationships] seed={args.seed} entities_dir={args.entities_dir}")
    data = load_entities(args.entities_dir)

    accused_in_rows = weave_accused_in(
        data["accused"], data["firs"], args.repeat_rate, args.cross_district_rate,
    )
    victim_in_rows = weave_victim_in(data["victims"], data["firs"])
    witnessed_rows = weave_witnessed(data["witnesses"], data["firs"])
    occurred_at_rows = weave_occurred_at(data["locations"], data["firs"])
    investigated_by_rows = weave_investigated_by(data["officers"], data["firs"])
    owns_vehicle_rows = weave_owns_vehicle(data["accused"], data["vehicles"], accused_in_rows)
    owns_phone_rows, accused_firs = weave_owns_phone(data["accused"], data["phone_numbers"], accused_in_rows)
    contacted_rows = weave_contacted(data["phone_numbers"], owns_phone_rows, accused_firs)
    associated_with_rows = weave_associated_with(accused_in_rows, data["firs"])
    linked_to_rows = weave_linked_to(data["firs"])
    of_type_rows = weave_of_type(data["firs"])

    write_csv(args.out_dir / "accused_in.csv", accused_in_rows,
              ["accused_id", "fir_id", "role_in_case", "date_added", "warrant_status"])
    write_csv(args.out_dir / "victim_in.csv", victim_in_rows,
              ["victim_id", "fir_id", "injury_severity", "complaint_type"])
    write_csv(args.out_dir / "witnessed.csv", witnessed_rows,
              ["witness_id", "fir_id", "statement_date", "statement_reliability"])
    write_csv(args.out_dir / "occurred_at.csv", occurred_at_rows,
              ["fir_id", "location_id", "exact_address", "landmark"])
    write_csv(args.out_dir / "investigated_by.csv", investigated_by_rows,
              ["fir_id", "officer_id", "assigned_date", "is_lead_officer"])
    write_csv(args.out_dir / "owns_vehicle.csv", owns_vehicle_rows,
              ["accused_id", "vehicle_id", "ownership_type", "link_fir_id"])
    write_csv(args.out_dir / "owns_phone.csv", owns_phone_rows,
              ["accused_id", "phone_id", "ownership_type", "link_fir_id"])
    write_csv(args.out_dir / "contacted.csv", contacted_rows,
              ["phone_id_1", "phone_id_2", "contact_date", "contact_count", "link_fir_ids"])
    write_csv(args.out_dir / "associated_with.csv", associated_with_rows,
              ["accused_id_1", "accused_id_2", "shared_fir_count", "first_shared_fir_id", "relationship_type"])
    write_csv(args.out_dir / "linked_to.csv", linked_to_rows,
              ["fir_id_1", "fir_id_2", "link_type", "link_confidence"])
    write_csv(args.out_dir / "of_type.csv", of_type_rows,
              ["fir_id", "type_id", "primary_section"])

    repeat_count = sum(1 for _, cnt in
                        _count_fir_per_accused(accused_in_rows).items() if cnt >= 2)
    print(f"[weave_relationships] repeat offenders: {repeat_count} "
          f"({repeat_count / len(data['accused']) * 100:.1f}% of {len(data['accused'])} accused)")
    print("[weave_relationships] done.")


def _count_fir_per_accused(accused_in_rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in accused_in_rows:
        counts[row["accused_id"]] += 1
    return counts


if __name__ == "__main__":
    main()
