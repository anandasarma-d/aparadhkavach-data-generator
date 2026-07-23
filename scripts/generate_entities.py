#!/usr/bin/env python3
"""AparadhKavach synthetic dataset — Phase 1: Entity Generation.

Spec source: Notion "Section 4 — Synthetic Dataset Design & Ethical
Guardrails" (4.2 Data Volume & Distribution, 4.3 Narrative Design, 4.4 Legal
Code Mapping, 4.5 Phase 1 pseudocode, 4.6 Ethical Guardrails) and
"Section 5 — Data Architecture" (5.3 Logical Data Model, 5.4 Physical Data
Model), fetched fresh 2026-07-14 (post gender-ratio / prior_offense_count
correction logged the same day in Implementation Log).

Output: one JSON file per entity type in --out-dir, matching Section 5.3's
logical attribute names exactly. This is the Phase 1 -> Phase 2 handoff
format described in Section 4.5 Phase 3.

Known spec gaps, resolved here and disclosed (not silently picked):
  - Section 4.4's Legal Code Mapping table documents ~18 concrete crime
    subcategories, but Section 4.2 calls for 45 fixed CrimeType nodes. The
    27 extra subcategories added below to reach 45 are NOT in the fetched
    Notion table; their bns_section is marked "TBD - verify" (mirroring
    4.4's own caution for unverified rows) and ipc_section uses generally
    known section numbers that have NOT been checked against a primary
    legal source. Treat these 27 as placeholders for a future legal review
    pass, same as 4.4's existing "(verify)"/"TBD" rows.
  - Faker has NO `kn_IN` (Kannada/India) locale at all - confirmed against
    the installed Faker 40.29.0's faker.config.AVAILABLE_LOCALES, which
    lists only en_IN, gu_IN, hi_IN, mr_IN, or_IN, ta_IN for India. This is a
    library gap, not a stale Notion cache issue: AGENTS.md, Section 4.5, and
    Section 4.8 all say "Faker(kn_IN)" but that locale does not exist to
    instantiate. Resolved here as: Faker("en_IN") as the base, PLUS a small
    curated Kannada name pool used for ~40% of generated person names
    (see person_name() below) to approximate Section 4.3's "~40% Kannada
    transliterated names" intent. Flagging this rather than silently
    swapping locales without disclosure.
  - Section 4.2's 9-category crime-type weight table (Theft, Vehicle
    theft/snatching, Assault/hurt, Fraud/cheating, Robbery/dacoity,
    Domestic violence/498A, Cybercrime, Burglary/housebreaking, Other) does
    not line up 1:1 with Section 4.4's category grouping (which has
    separate Murder/Kidnapping/Property categories, and folds vehicle
    theft/chain snatching under "Theft" rather than a separate top-level
    category). Murder, Kidnapping, and Property/mischief are mapped into
    4.2's "Other" (3%) bucket here; vehicle theft + chain snatching are
    mapped into 4.2's "Vehicle theft / snatching" (18%) bucket. Flagging
    this rather than silently reconciling it.
"""
from __future__ import annotations

import argparse
import json
import random
import string
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from faker import Faker

# ---------------------------------------------------------------------------
# Constants (also imported by weave_relationships.py / guardrail_validator.py)
# ---------------------------------------------------------------------------

BNS_TRANSITION_DATE = date(2024, 7, 1)  # Section 4.4 / ADR-015

# Karnataka's 31 districts (Section 4.2). weight_class drives volume/officer
# allocation: "urban" (Bengaluru Urban, Mysuru, Mangaluru/Dakshina Kannada,
# Hubballi-Dharwad/Dharwad) weighted higher for volume; "border" (Belagavi,
# Bidar, Chamarajanagar) weighted for cross-district patterns; "rural" is
# everything else, weighted for agricultural crime + harvest correlation.
KARNATAKA_DISTRICTS = [
    ("Bagalkot", "rural"),
    ("Ballari", "rural"),
    ("Belagavi", "border"),
    ("Bengaluru Rural", "rural"),
    ("Bengaluru Urban", "urban"),
    ("Bidar", "border"),
    ("Chamarajanagar", "border"),
    ("Chikballapur", "rural"),
    ("Chikkamagaluru", "rural"),
    ("Chitradurga", "rural"),
    ("Dakshina Kannada", "urban"),
    ("Davanagere", "rural"),
    ("Dharwad", "urban"),
    ("Gadag", "rural"),
    ("Hassan", "rural"),
    ("Haveri", "rural"),
    ("Kalaburagi", "rural"),
    ("Kodagu", "rural"),
    ("Kolar", "rural"),
    ("Koppal", "rural"),
    ("Mandya", "rural"),
    ("Mysuru", "urban"),
    ("Raichur", "rural"),
    ("Ramanagara", "rural"),
    ("Shivamogga", "rural"),
    ("Tumakuru", "rural"),
    ("Udupi", "rural"),
    ("Uttara Kannada", "rural"),
    ("Vijayapura", "rural"),
    ("Yadgir", "rural"),
    ("Vijayanagara", "rural"),
]
assert len(KARNATAKA_DISTRICTS) == 31

DISTRICT_WEIGHT = {"urban": 3.0, "border": 1.5, "rural": 1.0}
# RTO-style 2-digit code per district, for Vehicle registration_number KA-xx-####
DISTRICT_CODE = {name: f"{i+1:02d}" for i, (name, _) in enumerate(KARNATAKA_DISTRICTS)}

TALUK_SUFFIXES = ["Taluk HQ", "North", "South", "East", "West", "Rural"]
LOCATION_TYPES = [
    "RAILWAY_STATION", "BUS_STAND", "MARKET_AREA", "RESIDENTIAL_AREA",
    "HIGHWAY", "ATM_VICINITY", "COMMERCIAL_COMPLEX", "AGRICULTURAL_FIELD",
    "EDUCATIONAL_INSTITUTION",
]
LOCATION_NAME_TEMPLATES = {
    "RAILWAY_STATION": ["{d} Railway Station", "{d} Junction"],
    "BUS_STAND": ["{d} Bus Stand", "KSRTC Bus Stand, {d}"],
    "MARKET_AREA": ["{d} Market Area", "Gandhi Bazaar, {d}", "Main Market Road, {d}"],
    "RESIDENTIAL_AREA": ["{d} Residential Layout", "{d} Extension"],
    "HIGHWAY": ["Highway NH-48 near {d}", "Highway NH-4 near {d}", "Ring Road, {d}"],
    "ATM_VICINITY": ["ATM Vicinity, {d} Main Road", "ATM near {d} Circle"],
    "COMMERCIAL_COMPLEX": ["{d} Commercial Complex", "{d} Shopping Complex"],
    "AGRICULTURAL_FIELD": ["Agricultural Field, {d} Outskirts", "Farmland near {d}"],
    "EDUCATIONAL_INSTITUTION": ["{d} College Road", "Near {d} Government School"],
}

# Karnataka bounding box (approx)
LAT_RANGE = (11.6, 18.4)
LON_RANGE = (74.1, 78.5)

EVENT_CONTEXTS = [
    "NONE", "DASARA", "UGADI", "GANESHA_CHATURTHI", "DEEPAVALI",
    "HARVEST_SEASON", "NEW_YEAR", "MONSOON_SEASON",
]
assert len(EVENT_CONTEXTS) == 8  # Section 4.7: all 8 present, NONE largest

# (month range inclusive) windows used only to *assign* event_context on a
# FIR whose date_filed happens to fall in the window - approximate, since
# exact lunar-calendar festival dates vary year to year.
EVENT_MONTH_WINDOWS = {
    "UGADI": {3, 4},
    "GANESHA_CHATURTHI": {8, 9},
    "DASARA": {9, 10},
    "DEEPAVALI": {10, 11},
    "NEW_YEAR": {1},
    "MONSOON_SEASON": {6, 7},
    "HARVEST_SEASON": {10, 11, 12},
}

OFFICER_RANKS = ["SUB_INSPECTOR", "INSPECTOR", "CIRCLE_INSPECTOR", "DEPUTY_SUPERINTENDENT"]

# Section 4.9 "Full officer pool role distribution" (added 15 Jul 2026,
# Defect Tracker: generate_entities.py (InvestigationOfficer roles field)).
# Each of the ~180 officers gets exactly ONE role - stored in the `roles`
# field (5.4 Catalyst DataStore column: comma-separated, single value here).
OFFICER_ROLE_DISTRIBUTION = {
    "INVESTIGATOR": 0.65,
    "ANALYST": 0.15,
    "SUPERVISOR": 0.12,
    "POLICYMAKER": 0.08,
}
# ANALYST/POLICYMAKER are state-wide by design ("districts = [\"ALL\"]" per
# Section 4.9). The InvestigationOfficer schema (5.3/5.4) has only a single
# nullable `district` field, no multi-value districts field - so state-wide
# scope is represented as district=None, the SAME NULL-means-state-wide
# convention Section 5.8 already established for the Super Admin seed
# principal ("NULL = state-wide scope... not a sentinel string"), not a new
# field. See build_officers()'s docstring for the SUPERVISOR caveat this
# doesn't fully resolve.
STATE_WIDE_ROLES = {"ANALYST", "POLICYMAKER"}

GENDER_MALE, GENDER_FEMALE = "MALE", "FEMALE"

VEHICLE_TYPES = ["TWO_WHEELER", "CAR", "AUTO_RICKSHAW", "COMMERCIAL_VEHICLE", "TRACTOR"]
VEHICLE_MAKES = {
    "TWO_WHEELER": ["Hero", "Honda", "TVS", "Bajaj", "Royal Enfield"],
    "CAR": ["Maruti Suzuki", "Hyundai", "Tata", "Toyota", "Honda"],
    "AUTO_RICKSHAW": ["Bajaj", "TVS", "Piaggio"],
    "COMMERCIAL_VEHICLE": ["Tata", "Ashok Leyland", "Mahindra"],
    "TRACTOR": ["Mahindra", "Swaraj", "John Deere"],
}
VEHICLE_COLORS = ["White", "Black", "Silver", "Red", "Blue", "Grey"]

PHONE_CARRIERS = ["Airtel", "Jio", "Vodafone Idea", "BSNL"]
KARNATAKA_MOBILE_PREFIXES = ["6304", "7411", "8123", "9448", "9902", "9880", "9740", "8971"]

FIR_STATUSES = ["OPEN", "UNDER_INVESTIGATION", "CLOSED", "CHARGESHEETED"]
INVESTIGATION_STAGES = {
    "OPEN": ["FIR_REGISTERED"],
    "UNDER_INVESTIGATION": ["EVIDENCE_COLLECTION", "STATEMENT_RECORDING"],
    "CLOSED": ["CASE_CLOSED_UNTRACED", "CASE_CLOSED_MEDIATED"],
    "CHARGESHEETED": ["CHARGESHEET_FILED", "UNDER_TRIAL"],
}

SYSTEM_SEED = "SYSTEM_SEED"

# Faker has no kn_IN locale (see module docstring) - this curated pool
# stands in for ~40% Kannada-transliterated names per Section 4.3.
KANNADA_FIRST_NAMES_MALE = [
    "Basavaraj", "Chandrashekar", "Gopalakrishna", "Harish", "Jagadish",
    "Kariyappa", "Lokesh", "Manjunath", "Nagaraj", "Prakash", "Puttaswamy",
    "Raghavendra", "Ravindranath", "Shivakumar", "Siddalingaiah", "Sureshgowda",
    "Thimmaiah", "Veeranna", "Yallappa", "Channabasappa",
]
KANNADA_FIRST_NAMES_FEMALE = [
    "Akkamahadevi", "Bhagyalakshmi", "Chandrakala", "Gangamma", "Jayamma",
    "Kaveri", "Lakshmamma", "Mahadevi", "Nagaveni", "Parvatamma", "Pushpalatha",
    "Rathnamma", "Savitramma", "Shanthakumari", "Sujathamma", "Sunandamma",
    "Thimmakka", "Vasanthamma", "Yashodamma", "Girijamma",
]
KANNADA_LAST_NAMES = [
    "Gowda", "Hegde", "Naik", "Poojary", "Shetty", "Rai", "Reddy",
    "Devadiga", "Bhat", "Achar", "Nayak", "Urs", "Setty", "Kulkarni",
]


def person_name(fake: Faker, gender: str | None = None, kannada_prob: float = 0.40) -> str:
    if random.random() < kannada_prob:
        if gender == GENDER_FEMALE:
            first = random.choice(KANNADA_FIRST_NAMES_FEMALE)
        elif gender == GENDER_MALE:
            first = random.choice(KANNADA_FIRST_NAMES_MALE)
        else:
            first = random.choice(KANNADA_FIRST_NAMES_MALE + KANNADA_FIRST_NAMES_FEMALE)
        return f"{first} {random.choice(KANNADA_LAST_NAMES)}"
    if gender == GENDER_FEMALE:
        return fake.name_female()
    if gender == GENDER_MALE:
        return fake.name_male()
    return fake.name()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def audit_fields() -> dict:
    ts = now_iso()
    return {
        "created_at": ts,
        "updated_at": ts,
        "created_by": SYSTEM_SEED,
        "updated_by": SYSTEM_SEED,
    }


# ---------------------------------------------------------------------------
# 4.4 Legal Code Mapping -> 45 CrimeType nodes (Section 4.2 fixed taxonomy)
# ---------------------------------------------------------------------------
# category -> 4.2 weight bucket key used for the weighted FIR crime_type draw
CATEGORY_WEIGHTS = {
    "Theft": 22,
    "Vehicle theft / snatching": 18,
    "Assault / hurt": 15,
    "Fraud / cheating": 12,
    "Robbery / dacoity": 10,
    "Domestic violence / 498A": 8,
    "Cybercrime": 7,
    "Burglary / housebreaking": 5,
    "Other": 3,
}

# (category, subcategory, ipc_section, ipc_description, bns_section,
#  bns_description, severity_level, sourced_from_notion)
_CRIME_TYPE_ROWS = [
    # --- Verbatim from Notion Section 4.4 (18 rows) ---
    ("Theft", "General theft", "379, 380", "Theft; theft in dwelling house", "303, 305 (verify)", "Theft; theft in dwelling house (unverified)", "LOW", True),
    ("Vehicle theft / snatching", "Vehicle theft", "379, 411", "Theft; dishonestly receiving stolen property", "303, 317(2) (verify)", "Theft; dishonestly receiving stolen property (unverified)", "MEDIUM", True),
    ("Vehicle theft / snatching", "Chain snatching", "379, 356", "Theft; assault/criminal force to commit theft", "304", "Snatching (standalone BNS offense)", "MEDIUM", True),
    ("Robbery / dacoity", "Armed robbery", "392, 397", "Robbery; robbery with deadly weapon", "TBD - verify", "TBD - verify", "HIGH", True),
    ("Robbery / dacoity", "Dacoity", "395, 396", "Dacoity; dacoity with murder", "TBD - verify", "TBD - verify", "CRITICAL", True),
    ("Burglary / housebreaking", "Housebreaking", "454, 457", "Housebreaking to commit offence; housebreaking by night", "331 (verify)", "Housebreaking (unverified)", "MEDIUM", True),
    ("Assault / hurt", "Simple hurt", "323, 324", "Voluntarily causing hurt; hurt by dangerous weapon", "TBD - verify", "TBD - verify", "LOW", True),
    ("Assault / hurt", "Grievous hurt", "325, 326", "Voluntarily causing grievous hurt; grievous hurt by dangerous weapon", "TBD - verify", "TBD - verify", "MEDIUM", True),
    ("Assault / hurt", "With intent to rob", "394", "Voluntarily causing hurt in committing robbery", "296", "Hurt in committing robbery", "HIGH", True),
    ("Fraud / cheating", "Cheating", "420", "Cheating and dishonestly inducing delivery of property", "318", "Cheating", "MEDIUM", True),
    ("Fraud / cheating", "Forgery", "468, 471", "Forgery for cheating; using forged document as genuine", "TBD - verify", "TBD - verify", "MEDIUM", True),
    ("Cybercrime", "Online fraud", "66C, 66D (IT Act) + 420 IPC", "Identity theft; cheating by personation using computer + IPC cheating", "66C, 66D (IT Act - unaffected by BNS) + 318 BNS", "Identity theft; cheating by personation using computer + BNS cheating", "MEDIUM", True),
    ("Cybercrime", "Harassment", "67A (IT Act)", "Publishing sexually explicit material electronically", "67A (IT Act - unaffected by BNS)", "Publishing sexually explicit material electronically", "MEDIUM", True),
    ("Domestic violence / 498A", "Cruelty by husband/relatives", "498A", "Cruelty by husband or relatives of husband", "85", "Cruelty (see also 86 - definitions)", "HIGH", True),
    ("Domestic violence / 498A", "Dowry death", "304B", "Dowry death", "80", "Dowry death", "CRITICAL", True),
    ("Other", "Culpable homicide (murder)", "302, 304", "Murder; culpable homicide not amounting to murder", "103 (murder), 105 (culpable homicide)", "Murder; culpable homicide not amounting to murder", "CRITICAL", True),
    ("Other", "Kidnapping / abduction", "363, 364", "Kidnapping; kidnapping/abduction to murder", "137 (verify split across subsections)", "Kidnapping/abduction (unverified subsection split)", "CRITICAL", True),
    ("Other", "Mischief / damage", "427, 436", "Mischief causing damage; mischief by fire/explosive", "324 range (verify)", "Mischief causing damage (unverified)", "LOW", True),
    # --- Extended to reach the 45-node fixed taxonomy (Section 4.2). NOT
    #     sourced from the fetched Notion 4.4 table - ipc_section values use
    #     generally known section numbers, bns_section is intentionally left
    #     "TBD - verify" throughout, consistent with 4.4's own caution. ---
    ("Theft", "Shop theft / shoplifting", "379", "Theft", "TBD - verify", "TBD - verify", "LOW", False),
    ("Theft", "Pickpocketing", "379", "Theft", "TBD - verify", "TBD - verify", "LOW", False),
    ("Theft", "Theft of agricultural produce / cattle", "379", "Theft", "TBD - verify", "TBD - verify", "LOW", False),
    ("Theft", "Theft from parked motor vehicle", "379", "Theft", "TBD - verify", "TBD - verify", "LOW", False),
    ("Vehicle theft / snatching", "Two-wheeler theft", "379, 411", "Theft; dishonestly receiving stolen property", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Vehicle theft / snatching", "Auto-rickshaw theft", "379, 411", "Theft; dishonestly receiving stolen property", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Vehicle theft / snatching", "Mobile phone snatching", "379, 356", "Theft; assault/criminal force to commit theft", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Assault / hurt", "Assault on public servant", "353", "Assault/criminal force to deter public servant", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Assault / hurt", "Attempt to murder", "307", "Attempt to murder", "TBD - verify", "TBD - verify", "CRITICAL", False),
    ("Assault / hurt", "Criminal intimidation", "506", "Criminal intimidation", "TBD - verify", "TBD - verify", "LOW", False),
    ("Fraud / cheating", "Online banking fraud", "420, 66C/66D (IT Act)", "Cheating + identity theft using computer", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Fraud / cheating", "Cheque dishonour", "138 (NI Act)", "Dishonour of cheque for insufficiency of funds", "TBD - verify", "TBD - verify", "LOW", False),
    ("Fraud / cheating", "Criminal breach of trust", "406", "Criminal breach of trust", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Robbery / dacoity", "Highway robbery", "392", "Robbery", "TBD - verify", "TBD - verify", "HIGH", False),
    ("Robbery / dacoity", "Extortion", "384", "Extortion", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Robbery / dacoity", "Attempt to commit robbery", "393", "Attempt to commit robbery", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Domestic violence / 498A", "Dowry harassment", "498A", "Cruelty by husband or relatives of husband", "TBD - verify", "TBD - verify", "HIGH", False),
    ("Domestic violence / 498A", "Physical abuse (PWDVA)", "3, Protection of Women from Domestic Violence Act 2005", "Domestic violence", "TBD - verify", "TBD - verify", "HIGH", False),
    ("Domestic violence / 498A", "Mental cruelty", "498A", "Cruelty by husband or relatives of husband", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Cybercrime", "Identity theft", "66C (IT Act)", "Identity theft", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Cybercrime", "SIM swap fraud", "66D (IT Act)", "Cheating by personation using computer", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Cybercrime", "Social media impersonation", "66D (IT Act), 66A (unreported)", "Cheating by personation using computer", "TBD - verify", "TBD - verify", "LOW", False),
    ("Cybercrime", "Cyberstalking", "354D, 66A (IT Act)", "Stalking (incl. online)", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Burglary / housebreaking", "Shop burglary (night)", "457, 380", "Housebreaking by night; theft in dwelling house", "TBD - verify", "TBD - verify", "MEDIUM", False),
    ("Burglary / housebreaking", "Burglary with hurt", "458", "House-trespass/housebreaking by night after preparation for hurt", "TBD - verify", "TBD - verify", "HIGH", False),
    ("Other", "Public nuisance", "268", "Public nuisance", "TBD - verify", "TBD - verify", "LOW", False),
    ("Other", "Criminal trespass", "447", "Criminal trespass", "TBD - verify", "TBD - verify", "LOW", False),
]
assert len(_CRIME_TYPE_ROWS) == 45, f"expected 45 CrimeType rows, got {len(_CRIME_TYPE_ROWS)}"


def build_crime_types() -> list[dict]:
    crime_types = []
    for idx, row in enumerate(_CRIME_TYPE_ROWS, start=1):
        category, subcategory, ipc_section, ipc_description, bns_section, bns_description, severity, _sourced = row
        crime_types.append({
            "type_id": f"CT-{idx:03d}",
            "category": category,
            "subcategory": subcategory,
            "ipc_section": ipc_section,
            "ipc_description": ipc_description,
            "bns_section": bns_section,
            "bns_description": bns_description,
            "severity_level": severity,
            **audit_fields(),
        })
    return crime_types


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def build_locations(fake: Faker, count: int) -> list[dict]:
    locations = []
    # allocate location count per district proportional to DISTRICT_WEIGHT
    weights = [DISTRICT_WEIGHT[wc] for _, wc in KARNATAKA_DISTRICTS]
    total_weight = sum(weights)
    allocations = _largest_remainder_allocation(count, weights)
    idx = 1
    for (district, _wc), n in zip(KARNATAKA_DISTRICTS, allocations):
        for _ in range(n):
            location_type = random.choice(LOCATION_TYPES)
            name_template = random.choice(LOCATION_NAME_TEMPLATES[location_type])
            locations.append({
                "location_id": f"LOC-{idx:05d}",
                "district": district,
                "taluk": f"{district} {random.choice(TALUK_SUFFIXES)}",
                "village": fake.city(),
                "location_name": name_template.format(d=district),
                "location_type": location_type,
                "lat": round(random.uniform(*LAT_RANGE), 6),
                "lon": round(random.uniform(*LON_RANGE), 6),
                **audit_fields(),
            })
            idx += 1
    return locations


def _largest_remainder_allocation(total: int, weights: list[float]) -> list[int]:
    """Distribute `total` integer units across buckets proportional to
    `weights`, guaranteeing the allocations sum to exactly `total`."""
    weight_sum = sum(weights)
    raw = [w / weight_sum * total for w in weights]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    # give the leftover units to the buckets with the largest fractional part
    fractional_order = sorted(range(len(weights)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in fractional_order[:remainder]:
        floors[i] += 1
    return floors


# ---------------------------------------------------------------------------
# InvestigationOfficer
# ---------------------------------------------------------------------------

def build_officers(fake: Faker, count: int, seed: int) -> list[dict]:
    """Section 4.9: each officer gets exactly one role (INVESTIGATOR 65%,
    ANALYST 15%, SUPERVISOR 12%, POLICYMAKER 8%) and a district scope
    derived from that role.

    Flagged gap (not silently resolved): Section 4.9 describes SUPERVISOR
    district assignment as "roughly half single-district, half
    multi-district/range-level" - i.e. a handful of *specific* neighbouring
    districts, distinct from full state-wide ALL scope (this distinction is
    exactly what 8.7's audit-log RBAC test wants to catch). The
    InvestigationOfficer schema (5.3/5.4) has no field that can express
    "these 3 of 31 districts" - only a single nullable `district`. The
    "multi-district/range-level" half of SUPERVISOR is therefore collapsed
    here into the same district=None / state-wide representation used for
    ANALYST/POLICYMAKER, which is schema-representable but loses the
    range-vs-full-state distinction. A true fix needs a schema decision
    (e.g. an officer_districts join, or an array field) that's out of scope
    for this fix - flagging for whoever implements Section 8 RBAC.

    Randomness for role/district-scope assignment runs on a dedicated
    random.Random instance, seeded independently of the module-level
    `random` calls used elsewhere in this file (rank assignment,
    Accused/Victim/Witness/Vehicle/PhoneNumber/FIR generation) - so this
    fix does not shift those already-validated draw sequences.
    """
    role_rng = random.Random(f"officer_roles:{seed}")

    role_counts = _largest_remainder_allocation(
        count, [OFFICER_ROLE_DISTRIBUTION[r] for r in OFFICER_ROLE_DISTRIBUTION]
    )
    roles = []
    for role, n in zip(OFFICER_ROLE_DISTRIBUTION, role_counts):
        roles.extend([role] * n)
    role_rng.shuffle(roles)

    supervisor_indices = [i for i, r in enumerate(roles) if r == "SUPERVISOR"]
    n_statewide_supervisors = len(supervisor_indices) // 2
    statewide_supervisor_indices = set(role_rng.sample(supervisor_indices, n_statewide_supervisors))

    officers = []
    districts = [d for d, _ in KARNATAKA_DISTRICTS]
    idx = 1
    d_idx = 0
    for i in range(count):
        role = roles[i]
        district = districts[d_idx % len(districts)]
        d_idx += 1

        state_wide = role in STATE_WIDE_ROLES or i in statewide_supervisor_indices
        officer_district = None if state_wide else district
        badge_code = DISTRICT_CODE[district] if not state_wide else "HQ"

        # Draw name, rank, then station_number in that exact order - the
        # same order the pre-fix dict literal evaluated them in
        # (name -> rank -> badge_number[no randomness] -> station's
        # random.randint). Reordering these draws (even keeping the same
        # call *count*) changes what the shared Mersenne Twister state
        # yields at each call site, which would still shift the
        # Accused/Victim/Witness/Vehicle/PhoneNumber/FIR sequence that
        # follows in main() despite the new role logic living on its own
        # isolated role_rng above.
        name = person_name(fake)
        rank = random.choice(OFFICER_RANKS)
        station_number = random.randint(1, 5)  # drawn unconditionally even when unused (state-wide)
        station = "State Police HQ" if state_wide else f"{district} Police Station {station_number}"

        officers.append({
            "officer_id": f"OFF-{idx:04d}",
            "name": name,
            "rank": rank,
            "badge_number": f"KSP-{badge_code}-{idx:04d}",
            "station": station,
            "district": officer_district,
            "is_active": True,
            "roles": role,
            **audit_fields(),
        })
        idx += 1
    return officers


# ---------------------------------------------------------------------------
# Accused / Victim
# ---------------------------------------------------------------------------

ACCUSED_AGE_GROUPS = [(18, 25), (26, 35), (36, 45), (46, 55), (56, 65)]
VICTIM_AGE_GROUPS = [(0, 17), (18, 25), (26, 35), (36, 45), (46, 55), (56, 65), (66, 80)]


def _age_group_label(age: int, buckets: list[tuple[int, int]]) -> str:
    for lo, hi in buckets:
        if lo <= age <= hi:
            return f"{lo}-{hi}" if lo > 0 else f"Under {hi + 1}"
    return f"{buckets[-1][1]}+"


def build_accused(fake: Faker, count: int) -> list[dict]:
    accused = []
    districts = [d for d, _ in KARNATAKA_DISTRICTS]
    district_weights = [DISTRICT_WEIGHT[wc] for _, wc in KARNATAKA_DISTRICTS]
    male_count = round(count * 0.70)  # Section 4.6: 70% M / 30% F, not 50/50
    genders = [GENDER_MALE] * male_count + [GENDER_FEMALE] * (count - male_count)
    random.shuffle(genders)
    for i in range(count):
        # Crime-type independence (Section 4.6): age/gender/district assigned
        # with no reference to any crime or FIR data whatsoever - Accused
        # entities carry no crime_type field at all.
        age = random.randint(18, 65)
        district = random.choices(districts, weights=district_weights, k=1)[0]
        accused.append({
            "accused_id": f"ACC-{i+1:05d}",
            "name": person_name(fake, genders[i]),
            "age": age,
            "age_group": _age_group_label(age, ACCUSED_AGE_GROUPS),
            "gender": genders[i],
            "address_district": district,
            "address_taluk": f"{district} {random.choice(TALUK_SUFFIXES)}",
            "occupation": fake.job(),
            "prior_offense_count": 0,
            "first_offense_date": None,
            "last_offense_date": None,
            "risk_score": None,
            "risk_score_updated_at": None,
            **audit_fields(),
        })
    return accused


def build_victims(fake: Faker, count: int) -> list[dict]:
    """Section 4.5 Phase 1 step 5 / 4.6: victim gender is uniform across
    crime types, EXCEPT domestic violence (498A/304B) victims skew female.
    Since Victim entities (like Accused) carry no crime_type field, the
    498A/304B skew is applied later by weave_relationships.py at the
    VICTIM_IN assignment step, where the FIR's crime type is known. Here we
    generate a uniform-gender pool; weave_relationships.py's DV assignment
    logic preferentially draws female victims for 498A/304B FIRs from
    within this same pool (still not a field on Victim itself)."""
    victims = []
    districts = [d for d, _ in KARNATAKA_DISTRICTS]
    district_weights = [DISTRICT_WEIGHT[wc] for _, wc in KARNATAKA_DISTRICTS]
    for i in range(count):
        age = random.randint(0, 80)
        district = random.choices(districts, weights=district_weights, k=1)[0]
        gender = random.choice([GENDER_MALE, GENDER_FEMALE])  # uniform
        victims.append({
            "victim_id": f"VIC-{i+1:05d}",
            "name": person_name(fake, gender),
            "age": age,
            "age_group": _age_group_label(age, VICTIM_AGE_GROUPS),
            "gender": gender,
            "address_district": district,
            **audit_fields(),
        })
    return victims


def build_witnesses(fake: Faker, count: int) -> list[dict]:
    witnesses = []
    statement_templates = [
        "Witness stated they saw the incident from a short distance and could identify the accused.",
        "Witness heard a commotion and observed the aftermath of the incident.",
        "Witness was present at the location and corroborated the complainant's account.",
        "Witness provided partial details, citing poor visibility at the time of the incident.",
    ]
    for i in range(count):
        witnesses.append({
            "witness_id": f"WIT-{i+1:05d}",
            "name": person_name(fake),
            "statement_summary": random.choice(statement_templates),
            "is_hostile": random.random() < 0.08,
            **audit_fields(),
        })
    return witnesses


# ---------------------------------------------------------------------------
# Vehicle / PhoneNumber
# ---------------------------------------------------------------------------

def build_vehicles(count: int) -> list[dict]:
    vehicles = []
    districts = [d for d, _ in KARNATAKA_DISTRICTS]
    for i in range(count):
        vtype = random.choice(VEHICLE_TYPES)
        district = random.choice(districts)
        plate_number = "".join(random.choices(string.ascii_uppercase, k=2)) + str(random.randint(1000, 9999))
        vehicles.append({
            "vehicle_id": f"VEH-{i+1:05d}",
            "registration_number": f"KA-{DISTRICT_CODE[district]}-{plate_number}",
            "vehicle_type": vtype,
            "make": random.choice(VEHICLE_MAKES[vtype]),
            "model": f"Model-{random.randint(1, 20)}",
            "color": random.choice(VEHICLE_COLORS),
            "year": random.randint(2005, 2025),
            **audit_fields(),
        })
    return vehicles


def build_phone_numbers(count: int) -> list[dict]:
    phones = []
    seen = set()
    i = 0
    while i < count:
        prefix = random.choice(KARNATAKA_MOBILE_PREFIXES)
        number = prefix + "".join(random.choices(string.digits, k=6))
        if number in seen:
            continue
        seen.add(number)
        phones.append({
            "phone_id": f"PHN-{i+1:05d}",
            "number": number,
            "carrier": random.choice(PHONE_CARRIERS),
            "is_active": random.random() < 0.95,
            **audit_fields(),
        })
        i += 1
    return phones


# ---------------------------------------------------------------------------
# FIR
# ---------------------------------------------------------------------------

NARRATIVE_MO_PHRASES = {
    "Theft": ["the accused allegedly took the property without the owner's consent",
              "the item was found missing after the accused was seen nearby",
              "the accused reportedly slipped away with the belongings unnoticed"],
    "Vehicle theft / snatching": ["the vehicle was allegedly driven away by the accused",
                                   "the accused reportedly snatched the item and fled on a two-wheeler",
                                   "the vehicle was reported missing from where it was parked"],
    "Assault / hurt": ["the accused allegedly assaulted the complainant following an altercation",
                        "a physical altercation reportedly broke out, resulting in injuries",
                        "the accused reportedly used force against the complainant"],
    "Fraud / cheating": ["the accused allegedly induced the complainant to part with money under false pretenses",
                          "the complainant reported being deceived into a fraudulent transaction",
                          "the accused reportedly misrepresented facts to obtain property"],
    "Robbery / dacoity": ["the accused allegedly used force to take the property by threat",
                           "a group reportedly surrounded the complainant and took valuables by force",
                           "the accused reportedly threatened the complainant with a weapon during the robbery"],
    "Domestic violence / 498A": ["the complainant alleged sustained harassment by family members",
                                  "the complainant reported repeated cruelty over dowry demands",
                                  "the complainant alleged mistreatment by the accused's relatives"],
    "Cybercrime": ["the accused allegedly gained unauthorized access to the complainant's account",
                   "the complainant reported a fraudulent online transaction linked to the accused",
                   "the accused reportedly impersonated the complainant online"],
    "Burglary / housebreaking": ["the accused allegedly entered the premises during the night",
                                  "the premises were reportedly broken into while unoccupied",
                                  "the accused reportedly gained entry by breaking a window"],
    "Other": ["the incident was reported following a disturbance at the location",
              "the complainant alleged the accused caused damage to property",
              "the accused was reportedly involved in the disturbance described"],
}

WITNESS_NOTE_TEMPLATES = [
    "A witness present at the scene has been recorded.",
    "Statements from persons nearby at the time are being recorded.",
]

# Round 2 fix for Section 4.7 Level 3 semantic validation (Defect Tracker,
# "generate_entities.py (FIR narrative_text lexical variation)", re-diagnosed
# 15 Jul 2026). Round 1 added wording variety to narrative_text's boilerplate
# clauses, but investigation showed boilerplate is ~88% of narrative_text's
# word count regardless of phrasing, and the embedding formula's other
# component - modus_operandi - draws from only 3 fixed phrases per category
# (NARRATIVE_MO_PHRASES above), so ~34-36% of a category's FIRs share the
# exact same phrase. A semantic embedding model treats reworded boilerplate
# as equivalent content, so Round 1 couldn't move same-category/cross-
# category/gradient numbers - and the sparse MO pool alone guarantees heavy
# same-category duplication once boilerplate is no longer diluting it.
#
# Fix: narrative_core (built below by _build_narrative_core) is a NEW field,
# separate from narrative_text - short, boilerplate-free, and drawn from a
# combinatorial pool (independent action + circumstance slots) rather than
# a single flat list, so authoring ~8 action phrases per category still
# yields 8x15=120 combinations, not 8. NARRATIVE_MO_PHRASES/modus_operandi
# are left completely untouched (still feed narrative_text's intro sentence
# and the modus_operandi field exactly as before) - narrative_core is used
# only by embedding_ingestion.py's content formula, not stored in Catalyst
# DataStore (Section 5.4's firs DDL has no narrative_core column; this is a
# Phase-1-output-only field, flagged rather than silently added as schema).
MO_ACTION_PHRASES = {
    "Theft": [
        "the accused allegedly pickpocketed the complainant's wallet in a crowded market",
        "the accused reportedly slipped away with an unattended bag left on a shop counter",
        "the complainant's mobile phone was allegedly lifted from a coat pocket on a crowded bus",
        "the accused allegedly walked off with groceries and cash from an unlocked storefront",
        "a gold chain was reportedly snatched from a display counter while the shopkeeper was distracted",
        "the accused allegedly made off with a laptop bag left unattended at a cafe table",
        "cash from the till was reportedly taken while the shop was momentarily unstaffed",
        "the complainant's purse was allegedly lifted from a shopping cart in a supermarket",
    ],
    "Vehicle theft / snatching": [
        "the accused allegedly rode off with a motorcycle parked outside a residential complex",
        "a chain was reportedly snatched from the complainant while she was walking near a bus stop",
        "the accused allegedly hotwired and drove away an unattended two-wheeler from a parking area",
        "a mobile phone was reportedly snatched from the complainant's hand while boarding an auto-rickshaw",
        "the accused allegedly drove off with a car left running outside a shop",
        "an auto-rickshaw was reportedly taken from outside a workshop overnight",
        "the accused allegedly snatched a gold bracelet while riding past on a two-wheeler",
        "a delivery bike was reportedly stolen from outside a food stall during peak hours",
    ],
    "Assault / hurt": [
        "the accused allegedly struck the complainant with a blunt object during a heated argument",
        "a physical altercation reportedly broke out after a dispute over a parking space",
        "the accused allegedly slapped and pushed the complainant following a verbal dispute",
        "the complainant was reportedly punched in the face during a confrontation over an unpaid debt",
        "a scuffle allegedly broke out between the two parties over a property boundary dispute",
        "the accused reportedly assaulted the complainant with a wooden stick during a quarrel",
        "the complainant allegedly sustained injuries after being thrown to the ground during a fight",
        "the accused reportedly grabbed the complainant by the collar and struck him repeatedly",
    ],
    "Fraud / cheating": [
        "the accused allegedly collected an advance payment for goods that were never delivered",
        "the complainant was reportedly persuaded to invest in a scheme that turned out to be fraudulent",
        "the accused allegedly issued a cheque that was later found to bounce due to insufficient funds",
        "the complainant reportedly transferred money after being promised a job that did not exist",
        "the accused allegedly forged signatures on a property document to claim ownership",
        "a fake insurance claim was reportedly filed using fabricated medical documents",
        "the complainant was allegedly overcharged through a manipulated billing system at a business",
        "the accused reportedly sold counterfeit goods while claiming they were genuine branded products",
    ],
    "Robbery / dacoity": [
        "the accused allegedly threatened the complainant with a knife before taking his wallet",
        "a group reportedly surrounded the complainant near a deserted lane and took his belongings by force",
        "the accused allegedly forced entry into the shop and took cash from the register at gunpoint",
        "the complainant was reportedly waylaid on a highway and robbed of valuables by armed men",
        "the accused allegedly snatched jewellery after threatening the complainant with a sharp weapon",
        "a gang reportedly held up the complainant's vehicle and demanded valuables under threat",
        "the accused allegedly beat the complainant and fled with cash during an attempted robbery",
        "the complainant reportedly handed over belongings after being threatened by multiple assailants",
    ],
    "Domestic violence / 498A": [
        "the complainant alleged repeated physical abuse by her husband over dowry demands",
        "the complainant reported being confined to a room and denied food by in-laws",
        "the complainant alleged verbal and emotional abuse by family members over a period of months",
        "the complainant reported being threatened with harm if additional dowry was not provided",
        "the complainant alleged being physically assaulted by her husband after a domestic dispute",
        "the complainant reported being subjected to persistent harassment by her in-laws",
        "the complainant alleged being denied access to her children by the accused",
        "the complainant reported sustained mental cruelty linked to ongoing dowry-related demands",
    ],
    "Cybercrime": [
        "the complainant's bank account was reportedly accessed without authorization and funds transferred out",
        "the accused allegedly created a fake social media profile impersonating the complainant",
        "the complainant reportedly received a phishing link that led to unauthorized withdrawal of funds",
        "the accused allegedly used the complainant's SIM card details to access his banking app",
        "the complainant reported being blackmailed using morphed images posted online",
        "the accused allegedly gained access to the complainant's email account and altered its recovery settings",
        "the complainant reportedly lost money after clicking a fraudulent investment advertisement online",
        "the accused allegedly used stolen card details to make unauthorized online purchases",
    ],
    "Burglary / housebreaking": [
        "the accused allegedly broke a rear window and entered the house while the family was away",
        "the premises were reportedly broken into overnight and valuables were found missing the next morning",
        "the accused allegedly picked the lock of the shop's shutter and entered after closing hours",
        "jewellery and cash were reportedly taken from a locked cupboard after forced entry",
        "the accused allegedly climbed over a compound wall and entered through an unlocked door",
        "the complainant returned home to find the main door forced open and belongings missing",
        "the accused allegedly cut through a grill window to gain entry into the residence",
        "electronics and cash were reportedly stolen after the accused broke into an unoccupied flat",
    ],
    "Other": [
        "the accused allegedly set fire to farm equipment following a longstanding dispute",
        "the complainant alleged that the accused damaged his vehicle during a heated confrontation",
        "the accused reportedly caused a public disturbance that escalated into property damage",
        "the complainant alleged the accused trespassed onto his land and damaged the boundary fence",
        "the accused allegedly threw stones at the complainant's shop during a dispute",
        "the complainant reported that the accused vandalized his vehicle overnight",
        "the accused allegedly caused a scene at a public gathering leading to a scuffle",
        "the complainant alleged the accused damaged crops on his land during a boundary dispute",
    ],
}

# Shared across categories deliberately - circumstance framing (time/place/
# distraction) doesn't carry crime-type signal in reality either, so reusing
# one pool keeps the *action* phrases as the only cross-category-differentiating
# axis while still multiplying combinations (8 actions x 15 circumstances =
# 120 per category) without authoring 9 separate circumstance pools.
MO_CIRCUMSTANCE_PHRASES = [
    "in a quiet residential lane late at night",
    "amid heavy traffic near a busy signal junction",
    "while the complainant had briefly stepped away",
    "during the crowded evening commute hours",
    "as the complainant was distracted by a passerby",
    "near an unattended area outside a local market",
    "shortly after the premises had been left unattended",
    "close to a bus stand during peak hours",
    "during a momentary lapse in supervision",
    "in broad daylight near a busy junction",
    "amid the confusion of a large public gathering",
    "late in the evening as shops were closing",
    "in an isolated stretch away from public view",
    "during the early morning hours before daybreak",
    "over the course of several days leading up to the incident",
]

# Round 3 (Defect Tracker: Round 2's 21-word narrative_core sample-tested
# WORSE on near-duplicates than the full-corpus Round 2 result - 199/225 vs
# 1,124/3,720 - because Voyage's embeddings for very short inputs cluster
# together almost independent of content (mean pairwise cosine similarity
# 0.7576 across ALL 225 sampled FIRs, regardless of category - higher than
# the OLD 82-word narrative_text's cross-category baseline of ~0.82 on the
# full corpus). Round 3 targets 40-60 words, but every added word must be
# genuine case-specific content, not generic scene-setting (that would just
# recreate Round 1/2's dilution problem at smaller scale) - so this round
# adds two NEW category-specific slots (method/weapon specifics, outcome/
# escape sequence) and, critically, incorporates REAL per-FIR data already
# generated earlier in build_firs (location_name, district, accused_known,
# accused_name) rather than more authored filler. MO_CIRCUMSTANCE_PHRASES
# above is dropped from narrative_core's composition for this reason - it's
# generic time/place framing, exactly the kind of padding this round is
# guarding against, and real location_name/district now cover that role
# with actual case data instead.
MO_METHOD_PHRASES = {
    "Theft": [
        "using a small blade to slit open the bag",
        "by quietly lifting the item without drawing attention",
        "while posing as a fellow customer browsing nearby",
        "by creating a distraction before reaching for the item",
        "using a decoy conversation to divert attention",
        "by reaching in through a gap left in the counter",
    ],
    "Vehicle theft / snatching": [
        "by breaking the steering lock within moments",
        "while riding pillion on another two-wheeler",
        "by snapping the chain in a single swift motion",
        "using a duplicate key suspected to have been made in advance",
        "by pushing the vehicle away before starting the engine at a distance",
        "while the ignition had been left unattended briefly",
    ],
    "Assault / hurt": [
        "using a wooden stick kept nearby",
        "with bare hands during the scuffle",
        "by pushing the complainant against a wall",
        "using a metal rod picked up during the argument",
        "by repeatedly kicking the complainant after he fell",
        "with an open palm across the face",
    ],
    "Fraud / cheating": [
        "through a fabricated investment proposal shared over phone calls",
        "using a forged letterhead resembling a government office",
        "by promising a discount available only for immediate cash payment",
        "through a fake online storefront created for the purpose",
        "using a borrowed identity document to open the transaction",
        "by presenting doctored bank statements as proof of funds",
    ],
    "Robbery / dacoity": [
        "brandishing a knife to threaten the complainant",
        "with three others blocking the complainant's path",
        "using a country-made weapon to intimidate the complainant",
        "by surrounding the complainant's vehicle at a signal",
        "while one accomplice kept watch nearby",
        "using physical force to overpower the complainant",
    ],
    "Domestic violence / 498A": [
        "through repeated verbal threats over several weeks",
        "by withholding basic household necessities",
        "through physical intimidation in front of family members",
        "by restricting contact with her own family",
        "through coordinated pressure from multiple in-laws",
        "by repeatedly raising the same dowry demand despite refusal",
    ],
    "Cybercrime": [
        "through a spoofed customer-care phone call",
        "using a cloned SIM obtained through the telecom provider",
        "via a malicious link sent through a messaging app",
        "by exploiting a weak password reused across accounts",
        "through a fake login page mimicking the bank's website",
        "using remote-access software installed under false pretenses",
    ],
    "Burglary / housebreaking": [
        "by prying open a window latch with a metal tool",
        "using a duplicate key suspected to have been copied earlier",
        "by cutting through a grill using a hacksaw",
        "through a rear door left unlocked by mistake",
        "by scaling a drainpipe to reach an upper floor window",
        "using a crowbar to force open the main door",
    ],
    "Other": [
        "following a prolonged dispute over shared property",
        "during an argument that escalated quickly",
        "using stones and other objects found nearby",
        "after repeated warnings were ignored",
        "during a confrontation at a public function",
        "following an earlier disagreement between the two parties",
    ],
}

MO_OUTCOME_PHRASES = {
    "Theft": [
        "before disappearing into the crowd on foot",
        "and was gone by the time the loss was noticed",
        "before slipping out through a side entrance",
        "and had left the area before any alarm was raised",
        "before boarding a passing bus to leave the area",
        "and could not be traced despite an immediate search",
    ],
    "Vehicle theft / snatching": [
        "before speeding away in the direction of the highway",
        "and disappeared into the by-lanes before anyone could react",
        "before the complainant could raise an alarm",
        "and had already left the area by the time it was reported",
        "before merging into the flow of traffic",
        "and evaded a nearby patrol shortly after",
    ],
    "Assault / hurt": [
        "leaving the complainant with visible bruises",
        "before bystanders intervened and separated the two",
        "resulting in a minor head injury requiring first aid",
        "before the accused was restrained by others present",
        "and fled the scene before the police arrived",
        "leaving the complainant with a swollen eye and torn clothing",
    ],
    "Fraud / cheating": [
        "before becoming unreachable once the payment was made",
        "and the promised goods were never delivered",
        "before the fraudulent nature of the scheme came to light",
        "and the phone number used was later found to be switched off",
        "before the office address given turned out to be non-existent",
        "and repeated follow-ups went unanswered",
    ],
    "Robbery / dacoity": [
        "before fleeing on a waiting two-wheeler",
        "and scattered in different directions afterward",
        "before the complainant could call for help",
        "leaving the complainant shaken but uninjured",
        "before disappearing into a nearby wooded area",
        "and were last seen heading towards the highway",
    ],
    "Domestic violence / 498A": [
        "leading the complainant to seek refuge with relatives",
        "before the complainant approached the police for help",
        "resulting in the complainant leaving the marital home temporarily",
        "leaving the complainant fearful of returning home",
        "prompting intervention from a local women's welfare group",
        "before the situation escalated further in recent weeks",
    ],
    "Cybercrime": [
        "before the account could be secured",
        "and the funds were moved through multiple accounts within minutes",
        "before the fraudulent profile was reported and taken down",
        "and the linked mobile number was changed shortly after",
        "before the complainant could reverse the transaction",
        "and the trail went cold at an unverified account",
    ],
    "Burglary / housebreaking": [
        "before the family returned later that evening",
        "and the theft was discovered only the next morning",
        "before neighbours noticed anything unusual",
        "leaving the premises ransacked",
        "before the caretaker arrived for the morning shift",
        "and no immediate witnesses came forward",
    ],
    "Other": [
        "before the situation was brought under control by others present",
        "resulting in damage that was assessed the following day",
        "before the matter was reported to the local authorities",
        "leaving both parties present at the scene when police arrived",
        "before tempers cooled and the crowd dispersed",
        "and the dispute remains unresolved at the time of filing",
    ],
}


def _build_narrative_core(core_rng: random.Random, category: str, location_name: str,
                           district: str, accused_known: bool, accused_name: str | None) -> str:
    """Boilerplate-free, category-differentiating text for embedding only
    (see the block comments above) - independent of narrative_text's own
    mo_phrase (NARRATIVE_MO_PHRASES, shared `random` stream, unchanged) and
    of narrative_rng (drives narrative_text's own clauses), so this can't
    perturb either. location_name/district/accused_known/accused_name are
    NOT new randomness - they're already-computed values from earlier in
    build_firs's loop, passed in as-is; using them here adds real per-case
    specificity at zero determinism risk to any other field."""
    action = core_rng.choice(MO_ACTION_PHRASES[category])
    method = core_rng.choice(MO_METHOD_PHRASES[category])
    outcome = core_rng.choice(MO_OUTCOME_PHRASES[category])
    location_clause = f"near {location_name} in {district} district"
    if accused_known and accused_name:
        accused_clause = f"the accused, identified as {accused_name}, is known to the complainant"
    else:
        accused_clause = "the accused involved could not be identified at the scene"
    return f"{action}, {method}, {location_clause}. {accused_clause}, {outcome}."

# Added to fix Section 4.7 Level 3 semantic validation failures (all 5 checks
# failed on the first embedding run - same-category 0.94 vs 0.65-0.85 target,
# 2,176/3,720 FIRs flagged as near-duplicates vs a 0-violator target). Root
# cause: these three clauses were each a single fixed string (or, for
# registration_note, a rigid fixed frame) present in nearly every narrative
# regardless of crime category, so they dominated the embedding input over
# the actual category-specific MO vocabulary. UNIDENTIFIED_ACCUSED_TEMPLATES
# and REGISTRATION_NOTE_TEMPLATES are the two clauses named in that
# diagnosis; INVESTIGATION_DETAIL_TEMPLATES is a third instance of the exact
# same defect (100% fixed across literally every FIR, not just the
# unidentified-accused ~45%) surfaced in the same diagnosis's quoted
# evidence, so it's fixed alongside the other two rather than left in place.
# registration_note's variants stay regime-neutral (no IPC/BNS section
# numbers) per Section 4.3 - only the framing phrase varies, not its content.
UNIDENTIFIED_ACCUSED_TEMPLATES = [
    "The accused is presently unidentified; efforts are underway to establish identity.",
    "No description of the accused could be obtained at the time of filing; inquiries into identity are ongoing.",
    "The identity of the accused remains unknown at this stage; local inquiries are being conducted.",
    "The accused could not be identified by the complainant; the matter is under active inquiry.",
    "As of filing, the accused had not been identified; investigators are pursuing available leads.",
    "The complainant was unable to identify the accused; identification efforts are continuing.",
]

INVESTIGATION_DETAIL_TEMPLATES = [
    "Further details of the incident are being verified during investigation.",
    "Investigation into the circumstances of the case is currently in progress.",
    "The matter is being examined further as part of the ongoing investigation.",
    "Additional facts surrounding the incident are being ascertained by investigating officers.",
    "The case is being investigated further to establish the complete sequence of events.",
    "Relevant facts of the case continue to be verified as the investigation proceeds.",
]

REGISTRATION_NOTE_TEMPLATES = [
    "FIR registered on {date} at {station}.",
    "This FIR was registered on {date} at {station}.",
    "The complaint was formally registered on {date} at {station}.",
    "Case registered on {date} at {station}.",
    "Formal registration of this case took place on {date} at {station}.",
]


def _build_narrative(narrative_rng: random.Random, category: str, district: str, location_name: str,
                      complainant: str, accused_known: bool, accused_name: str | None,
                      date_filed: date, police_station: str, has_witness: bool) -> str:
    # mo_phrase/time_str/witness_para draw from the SHARED `random` module,
    # completely unchanged from before this function grew new variation -
    # every other field in build_firs's loop is also drawn from that same
    # shared stream, so touching the number/order of calls here at all would
    # shift the stream and silently change unrelated fields (district, date,
    # crime_type, ...) for every FIR after this one. Confirmed the hard way:
    # an earlier version of this fix moved these three onto a local RNG too
    # and it reproduced FIR #1 exactly but diverged everywhere from FIR #2
    # onward - byte-for-byte proof the stream position matters here.
    #
    # accused_para (unidentified branch)/detail_para/registration_note are
    # NEW variation added by this fix (the old code had zero random calls
    # for them - fixed strings). They draw from `narrative_rng`, a per-FIR
    # local Random seeded independently (see build_firs) that never touches
    # the shared stream - so adding this variation changes narrative_text
    # only, nothing else, by construction rather than by coincidence.
    mo_phrase = random.choice(NARRATIVE_MO_PHRASES.get(category, NARRATIVE_MO_PHRASES["Other"]))
    time_str = f"{random.randint(0, 23):02d}:{random.choice(['00', '15', '30', '45'])}"
    intro = (f"On {date_filed.isoformat()}, at approximately {time_str} hours, at {location_name} "
              f"in {district} district, {complainant} reported that {mo_phrase}, causing loss and "
              f"distress to the complainant.")
    if accused_known and accused_name:
        accused_para = (f"The accused, identified as {accused_name}, was reportedly seen in the "
                         f"vicinity around the time of the incident and is stated to be known to the complainant.")
    else:
        accused_para = narrative_rng.choice(UNIDENTIFIED_ACCUSED_TEMPLATES)
    detail_para = narrative_rng.choice(INVESTIGATION_DETAIL_TEMPLATES)
    witness_para = random.choice(WITNESS_NOTE_TEMPLATES) if has_witness else ""
    registration_note = narrative_rng.choice(REGISTRATION_NOTE_TEMPLATES).format(
        date=date_filed.isoformat(), station=police_station)
    parts = [intro, accused_para, detail_para]
    if witness_para:
        parts.append(witness_para)
    parts.append(registration_note)
    text = " ".join(parts)
    return text


def _pick_event_context(d: date) -> str:
    candidates = [ctx for ctx, months in EVENT_MONTH_WINDOWS.items() if d.month in months]
    if not candidates:
        return "NONE"
    # keep NONE the dominant bucket overall (Section 4.7) even within window months
    if random.random() < 0.55:
        return "NONE"
    return random.choice(candidates)


def build_firs(fake: Faker, count: int, crime_types: list[dict], years: tuple[int, int],
                seed: int) -> list[dict]:
    districts = [d for d, _ in KARNATAKA_DISTRICTS]
    district_weight_map = dict(KARNATAKA_DISTRICTS)
    months = []
    start_year, end_year = years
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            months.append((y, m))

    # weight per (district, month) cell: district weight * seasonal factor
    cells = []
    cell_weights = []
    for district in districts:
        wclass = district_weight_map[district]
        base_w = DISTRICT_WEIGHT[wclass]
        for (y, m) in months:
            seasonal = 1.0
            if wclass == "rural" and m in (10, 11, 12):
                seasonal *= 1.15  # harvest season bump for rural districts
            if m in {8, 9, 10, 11}:
                seasonal *= 1.05  # general festival-season bump
            cells.append((district, y, m))
            cell_weights.append(base_w * seasonal)

    allocations = _largest_remainder_allocation(count, cell_weights)

    # crime category weighted draw, with a mild year-on-year upward trend for
    # Cybercrime / Fraud (Section 4.2: "year-on-year slight upward trend in
    # cybercrime and fraud categories")
    categories = list(CATEGORY_WEIGHTS.keys())
    crime_types_by_category: dict[str, list[dict]] = {}
    for ct in crime_types:
        crime_types_by_category.setdefault(ct["category"], []).append(ct)

    firs = []
    fir_counter = 1
    for (district, y, m), n in zip(cells, allocations):
        if n == 0:
            continue
        days_in_month = 31 if m in (1, 3, 5, 7, 8, 10, 12) else (30 if m != 2 else (29 if y % 4 == 0 else 28))
        year_trend = 1.0 + 0.06 * (y - start_year)  # +6%/yr drift toward cyber/fraud
        cat_weights = []
        for c in categories:
            w = CATEGORY_WEIGHTS[c]
            if c in ("Cybercrime", "Fraud / cheating"):
                w *= year_trend
            cat_weights.append(w)
        for _ in range(n):
            # day-of-month sampled independently per district-month (Section 4.5/4.7 declustering rule)
            day = random.randint(1, days_in_month)
            date_filed = date(y, m, day)
            legal_code = "IPC" if date_filed < BNS_TRANSITION_DATE else "BNS"

            category = random.choices(categories, weights=cat_weights, k=1)[0]
            crime_type = random.choice(crime_types_by_category[category])
            sections_raw = crime_type["ipc_section"] if legal_code == "IPC" else crime_type["bns_section"]
            sections_cited = [s.strip() for s in sections_raw.split(",")]

            incident_offset_days = random.randint(0, 5)
            date_of_incident = date_filed - timedelta(days=incident_offset_days)

            status = random.choices(FIR_STATUSES, weights=[25, 30, 20, 25], k=1)[0]
            investigation_stage = random.choice(INVESTIGATION_STAGES[status])
            event_context = _pick_event_context(date_filed)

            police_station = f"{district} Police Station {random.randint(1, 5)}"
            location_name = random.choice(LOCATION_NAME_TEMPLATES[random.choice(LOCATION_TYPES)]).format(d=district)
            complainant = person_name(fake)
            accused_known = random.random() < 0.55
            accused_name = person_name(fake) if accused_known else None
            has_witness = random.random() < 0.40
            modus_operandi = random.choice(NARRATIVE_MO_PHRASES.get(category, NARRATIVE_MO_PHRASES["Other"]))
            # Local, isolated RNG per FIR - deterministic (seed + fir_counter)
            # but never touches the shared `random` stream every other field
            # in this loop draws from. See _build_narrative's docstring note.
            narrative_rng = random.Random(f"{seed}:{fir_counter}")
            narrative_text = _build_narrative(
                narrative_rng, category, district, location_name, complainant, accused_known,
                accused_name, date_filed, police_station, has_witness,
            )
            # Separate, independently-seeded RNG (distinct seed string from
            # narrative_rng above) - guarantees this Round 2 addition can't
            # perturb narrative_rng's own draw sequence and change narrative_text
            # itself, which must stay byte-identical to Round 1's output.
            core_rng = random.Random(f"{seed}:core:{fir_counter}")
            narrative_core = _build_narrative_core(
                core_rng, category, location_name, district, accused_known, accused_name,
            )

            firs.append({
                "fir_id": f"FIR-{fir_counter:06d}",
                "fir_number": f"{DISTRICT_CODE[district]}/{y}/{fir_counter:06d}",
                "district": district,
                "police_station": police_station,
                "date_filed": date_filed.isoformat(),
                "date_of_incident": datetime.combine(date_of_incident, datetime.min.time()).isoformat(),
                "crime_type": category,
                "legal_code": legal_code,
                "sections_cited": sections_cited,
                "status": status,
                "narrative_text": narrative_text,
                "narrative_core": narrative_core,
                "modus_operandi": modus_operandi,
                "event_context": event_context,
                "investigation_stage": investigation_stage,
                # Internal linkage field, NOT part of Section 5.3's schema -
                # consumed only by weave_relationships.py to create the
                # OF_TYPE relationship without re-deriving crime_type ->
                # CrimeType lookups ambiguously. Flagged, not silently added.
                "_crime_type_id": crime_type["type_id"],
                **audit_fields(),
            })
            fir_counter += 1
    return firs


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AparadhKavach Phase 1 - synthetic entity generation")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", type=Path, default=Path("data/entities"))
    parser.add_argument("--n-locations", type=int, default=1120)
    parser.add_argument("--n-officers", type=int, default=180)
    parser.add_argument("--n-accused", type=int, default=4460)
    parser.add_argument("--n-victims", type=int, default=3900)
    parser.add_argument("--n-witnesses", type=int, default=1490)
    parser.add_argument("--n-vehicles", type=int, default=1300)
    parser.add_argument("--n-phones", type=int, default=2230)
    parser.add_argument("--n-firs", type=int, default=3720)
    parser.add_argument("--year-start", type=int, default=2021)
    parser.add_argument("--year-end", type=int, default=2025)
    args = parser.parse_args()

    random.seed(args.seed)
    fake = Faker("en_IN")  # kn_IN doesn't exist in Faker - see module docstring
    Faker.seed(args.seed)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[generate_entities] seed={args.seed} out_dir={args.out_dir}")

    crime_types = build_crime_types()
    locations = build_locations(fake, args.n_locations)
    officers = build_officers(fake, args.n_officers, args.seed)
    accused = build_accused(fake, args.n_accused)
    victims = build_victims(fake, args.n_victims)
    witnesses = build_witnesses(fake, args.n_witnesses)
    vehicles = build_vehicles(args.n_vehicles)
    phones = build_phone_numbers(args.n_phones)
    firs = build_firs(fake, args.n_firs, crime_types, (args.year_start, args.year_end), args.seed)

    outputs = {
        "crime_types.json": crime_types,
        "locations.json": locations,
        "officers.json": officers,
        "accused.json": accused,
        "victims.json": victims,
        "witnesses.json": witnesses,
        "vehicles.json": vehicles,
        "phone_numbers.json": phones,
        "firs.json": firs,
    }
    for filename, data in outputs.items():
        path = args.out_dir / filename
        with path.open("w") as f:
            json.dump(data, f, indent=2)
        print(f"  wrote {len(data):>5} records -> {path}")

    manifest = {
        "seed": args.seed,
        "generated_at": now_iso(),
        "counts": {k: len(v) for k, v in outputs.items()},
    }
    with (args.out_dir / "manifest.json").open("w") as f:
        json.dump(manifest, f, indent=2)

    print("[generate_entities] done.")


if __name__ == "__main__":
    main()
