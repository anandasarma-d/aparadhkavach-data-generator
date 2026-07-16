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
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Offense:
    """One FIR an accused appears in — the shape a Neo4j ACCUSED_IN join would supply."""

    fir_id: str
    date_of_incident: date
    district_id: str
    crime_type: str
    modus_operandi: str


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

    crime_type_severity: CrimeType -> severity_level (1-5) lookup: no documented mapping
    exists yet (see module docstring) — caller-supplied.
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
            (crime_type_severity.get(o.crime_type, 1) for o in offenses), default=1
        )

        district_spread = len({o.district_id for o in offenses})

        days_since_last_offense = (reference_date - dates[-1]).days if dates else None

        modus_operandi_consistency = (
            1 if len({o.modus_operandi for o in offenses}) <= 1 else 0
        )

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
    """The subset of a firs.csv row the hotspot forecaster's feature set actually reads."""

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
