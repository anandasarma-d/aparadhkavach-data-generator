"""Builds ACCUSED_FEATURES (Section 7.5.1) and HOTSPOT_FEATURES (Section 7.5.2) for Day 7's
QuickML pipelines — the repeat-offender risk scorer and the crime hotspot forecaster.

Offline script (ADR-007's justified deviation) — not a deployed service.

Input contract note: `accused_persons.csv`'s `prior_offense_count` is initialized to 0 at
entity-generation time (Section 4.5 Phase 1 step 4) and is never backfilled after Phase 2's
relationship weaving creates the actual ACCUSED_IN repeat-offender links — confirmed by
checking the committed CSV directly (100% of rows have prior_offense_count == 0). Neither
`firs.csv` nor any of the 5 Catalyst DataStore CSVs carries an accused-to-FIR join at all;
that relationship exists only in Neo4j. So `build_accused_features` takes each accused's
offense history as a pre-joined input (what a live Neo4j `MATCH (a:Accused)-[:ACCUSED_IN]->
(f:FIR)` query would supply), not a raw accused_persons.csv row — the real DataStore CSV
alone cannot supply this feature set as documented. Flagged for Anand; not fixed here, since
this session's scope is read-only against the committed dataset.

`crime_type_severity` (CrimeType.severity_level, 1-5 scale) has no documented mapping in any
Notion page fetched for this task — callers must supply their own. The values used in this
module's own tests are illustrative only, not an authoritative business decision.

Hardening pass (18 Jul 2026): `load_fir_records` and `write_feature_csv` below make this
script runnable against a real `catalyst ds:export --table firs` CSV and produce a CSV
QuickML can ingest as a Local File System Dataset. Confirmed against both the Catalyst
DataStore Table Creation & Population Runbook's Section 5.4 firs schema and the actual
committed `data/catalyst_datastore/firs.csv` (already in live/post-two-pass-FK-load shape,
not the pre-import synthetic-generator shape): `firs.district_id` holds Catalyst's
auto-generated ROWID (e.g. `42963000000035012`), never a `"DIST-01"`-style business key —
`build_hotspot_features` already treats it as an opaque grouping key, so no logic change was
needed there, only this docstring correction so nobody "fixes" it back to a string assumption
later. `date_filed` is confirmed Date-only (`YYYY-MM-DD`, no time component) in that same
committed file. Still genuinely unconfirmed: whether a real `catalyst ds:export` run emits
the exact same Date format `catalyst_datastore_transform.py` wrote — the Runbook documents
the DateTime gotcha (ISO-8601 `T`/`Z` rejected on *import*) but says nothing about export
formatting for Date columns specifically. `load_fir_records` fails fast with the offending
`fir_id` and raw value on a date it can't parse, rather than silently misreading it, so this
is safe to try against a real export first — flagged for Anand to confirm on the first live
run, not fixed here.

Neo4j-driver hardening pass (19 Jul 2026, `scripts/neo4j_accused_features_driver.py`):
confirmed against Section 5.4 and the live local graph's actual property keys (not assumed)
that Neo4j's `:FIR` node carries no `modus_operandi` property at all — `neo4j_populate.py`'s
own module docstring already documents this as a deliberate Section 5.4 field-projection
decision, not an oversight. So a Neo4j-sourced `Offense` can never supply
`modus_operandi_consistency`; `Offense.modus_operandi` is now Optional (default `None`), and
`build_accused_features` outputs `None` (unknown) for that one feature rather than treating
the resulting single-value set `{None}` as trivially "consistent" — which would silently
overstate confidence for every Neo4j-sourced accused. Existing fixture tests supply real MO
strings and are unaffected.

Also confirmed live: `CrimeType.severity_level` is a 4-value ordinal string
(`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`, from `generate_entities.py`'s taxonomy) — not the "1-5
scale" Section 7.5.1 describes — and a `crime_type` category can span CrimeType nodes of
*different* severities (e.g. "Domestic violence / 498A" has both a HIGH and a CRITICAL
subcategory), so the original category-keyed `crime_type_severity` dict can't losslessly
represent per-FIR severity. `Offense.crime_type_severity` (Optional, default `None`) now
carries the exact severity resolved per-FIR via that FIR's own `OF_TYPE` edge when available;
`build_accused_features` prefers it over the `crime_type_severity` dict lookup when set. The
dict-lookup path is untouched and still exercised by every existing fixture test.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Offense:
    """One FIR an accused appears in — the shape a Neo4j ACCUSED_IN join would supply.

    modus_operandi: None when unavailable — Neo4j's :FIR node has no modus_operandi property
    (see module docstring); build_accused_features reports modus_operandi_consistency as
    unknown (None) rather than guessing in that case.
    crime_type_severity: the exact severity resolved for THIS FIR via its own OF_TYPE edge,
    when known (see module docstring on why a crime_type-keyed dict alone isn't precise
    enough). None falls back to the crime_type_severity dict lookup in
    build_accused_features, preserving old behavior for existing (dict-only) callers.
    """

    fir_id: str
    date_of_incident: date
    district_id: str
    crime_type: str
    modus_operandi: str | None = None
    crime_type_severity: int | None = None


@dataclass(frozen=True)
class AccusedCase:
    """One accused's full offense history plus their Neo4j-sourced co-accused count."""

    accused_id: str
    offenses: tuple[Offense, ...]
    co_accused_count: int


def build_accused_features(
    cases: list[AccusedCase],
    crime_type_severity: dict[str, int],
    reference_date: date,
) -> list[dict]:
    """Section 7.5.1 — 7 features per accused for the repeat-offender risk scorer.

    crime_type_severity: CrimeType -> severity_level lookup, used only as a fallback for
    offenses where Offense.crime_type_severity is None (see module docstring for why a
    per-FIR value, when available, takes precedence). No documented category-level mapping
    exists — caller-supplied; illustrative only in this module's own tests, not authoritative.
    reference_date: "today" for days_since_last_offense — passed in, not read from the
    system clock, so this stays deterministic and testable.
    """
    rows = []
    for case in cases:
        offenses = sorted(case.offenses, key=lambda o: o.date_of_incident)
        dates = [o.date_of_incident for o in offenses]

        offense_count = len(offenses)

        if offense_count >= 2:
            intervals = [
                (dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)
            ]
            recidivism_interval_avg = statistics.mean(intervals)
        else:
            recidivism_interval_avg = None

        crime_type_severity_max = max(
            (
                o.crime_type_severity
                if o.crime_type_severity is not None
                else crime_type_severity.get(o.crime_type, 1)
                for o in offenses
            ),
            default=1,
        )

        district_spread = len({o.district_id for o in offenses})

        days_since_last_offense = (reference_date - dates[-1]).days if dates else None

        mo_values = {o.modus_operandi for o in offenses}
        if offense_count > 0 and None in mo_values:
            modus_operandi_consistency = None  # unavailable, not "trivially consistent"
        else:
            modus_operandi_consistency = 1 if len(mo_values) <= 1 else 0

        rows.append(
            {
                "accused_id": case.accused_id,
                "offense_count": offense_count,
                "recidivism_interval_avg": recidivism_interval_avg,
                "crime_type_severity_max": crime_type_severity_max,
                "district_spread": district_spread,
                "co_accused_count": case.co_accused_count,
                "days_since_last_offense": days_since_last_offense,
                "modus_operandi_consistency": modus_operandi_consistency,
            }
        )
    return rows


@dataclass(frozen=True)
class FirRecord:
    """The subset of a firs.csv row the hotspot forecaster's feature set actually reads.

    district_id is a Catalyst ROWID string (e.g. "42963000000035012") in a live
    ds:export/committed firs.csv, not a "DIST-01"-style business key — see module docstring.
    Only used here as an opaque grouping key, so this is a labelling note, not a behavior
    constraint.
    """

    fir_id: str
    district_id: str
    crime_type: str
    date_filed: date
    event_context: str  # "NONE" when not event-correlated, per Section 4.2/4.5


def build_hotspot_features(firs: list[FirRecord]) -> list[dict]:
    """Section 7.5.2 — 9 features per (district, crime_type, year, month) group for the
    hotspot forecaster: district, crime_type, year, month, event_context_flag, event_type,
    fir_count, rolling_3m_avg, yoy_delta.
    """
    groups: dict[tuple[str, str, int, int], list[FirRecord]] = {}
    for fir in firs:
        key = (fir.district_id, fir.crime_type, fir.date_filed.year, fir.date_filed.month)
        groups.setdefault(key, []).append(fir)

    fir_count_by_key = {key: len(v) for key, v in groups.items()}

    def month_offset(year: int, month: int, offset: int) -> tuple[int, int]:
        total = year * 12 + (month - 1) - offset
        return total // 12, total % 12 + 1

    rows = []
    for (district_id, crime_type, year, month), group in groups.items():
        event_contexts = [f.event_context for f in group]
        event_context_flag = 1 if any(ec != "NONE" for ec in event_contexts) else 0
        non_none = [ec for ec in event_contexts if ec != "NONE"]
        event_type = statistics.mode(non_none) if non_none else "NONE"

        fir_count = len(group)

        rolling_counts = []
        for offset in range(3):
            y, m = month_offset(year, month, offset)
            rolling_counts.append(fir_count_by_key.get((district_id, crime_type, y, m), 0))
        rolling_3m_avg = statistics.mean(rolling_counts)

        prev_year_key = (district_id, crime_type, year - 1, month)
        yoy_delta = fir_count - fir_count_by_key.get(prev_year_key, 0)

        rows.append(
            {
                "district": district_id,
                "crime_type": crime_type,
                "year": year,
                "month": month,
                "event_context_flag": event_context_flag,
                "event_type": event_type,
                "fir_count": fir_count,
                "rolling_3m_avg": rolling_3m_avg,
                "yoy_delta": yoy_delta,
            }
        )
    return rows


REQUIRED_FIRS_COLUMNS = ("fir_id", "district_id", "crime_type", "date_filed", "event_context")


def load_fir_records(csv_path: str | Path) -> list[FirRecord]:
    """Reads a `catalyst ds:export --table firs` (or equivalently-shaped, e.g. the committed
    `data/catalyst_datastore/firs.csv`) CSV into `FirRecord`s for `build_hotspot_features`.

    Reads by column name (`csv.DictReader`), not position — the Runbook's column order and a
    real export's column order aren't guaranteed to match, and this way they don't need to.
    Fails fast on a missing required column or an unparseable date_filed, naming the exact
    column or fir_id/raw value at fault, rather than silently building a wrong feature table.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or ())
        missing = [c for c in REQUIRED_FIRS_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError(
                f"{csv_path}: missing required firs column(s) {missing} — "
                f"found columns: {sorted(fieldnames)}"
            )

        records = []
        for row in reader:
            raw_date = row["date_filed"]
            try:
                date_filed = date.fromisoformat(raw_date)
            except ValueError as e:
                raise ValueError(
                    f"{csv_path}: fir_id={row['fir_id']!r} has an unparseable date_filed "
                    f"{raw_date!r} (expected YYYY-MM-DD) — confirm this matches what a real "
                    f"catalyst ds:export actually emits before trusting this feature table"
                ) from e

            records.append(
                FirRecord(
                    fir_id=row["fir_id"],
                    district_id=row["district_id"],
                    crime_type=row["crime_type"],
                    date_filed=date_filed,
                    event_context=row["event_context"],
                )
            )
    return records


def write_feature_csv(rows: list[dict], output_path: str | Path) -> None:
    """Writes `build_accused_features`/`build_hotspot_features` output to a CSV shaped for
    upload as a QuickML Local File System Dataset. Column order follows each row dict's own
    key order (Section 7.5.1/7.5.2 feature declaration order), so callers get a deterministic,
    reviewable column layout without needing to pass fieldnames separately.
    """
    if not rows:
        raise ValueError("no rows to write — refusing to produce an empty QuickML dataset CSV")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
