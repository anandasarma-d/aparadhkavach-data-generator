"""Unit tests for feature_builder.py. No live Catalyst/Neo4j connection — small local
fixtures only, per this task's explicit scope (feature_builder.py only needs live data to
run for real, which is out of scope here).

The fixtures below deliberately include repeat offenders and multi-district accused, unlike
the real committed accused_persons.csv (100% prior_offense_count == 0 — see feature_builder.py's
module docstring for why) — this is what a real Neo4j ACCUSED_IN join would actually look like.
"""

from datetime import date

from feature_builder import (
    AccusedCase,
    FirRecord,
    Offense,
    build_accused_features,
    build_hotspot_features,
)

CRIME_TYPE_SEVERITY = {
    # Illustrative only for these tests — no documented mapping exists (see module docstring).
    "Theft": 2,
    "Robbery / dacoity": 4,
    "Assault / hurt": 3,
    "Cybercrime": 2,
}


def test_single_offense_accused():
    case = AccusedCase(
        accused_id="ACC-00001",
        offenses=(
            Offense(
                fir_id="FIR-000001",
                date_of_incident=date(2024, 1, 10),
                district_id="42963000000035012",
                crime_type="Theft",
                modus_operandi="Snatch and run",
            ),
        ),
        co_accused_count=0,
    )

    [row] = build_accused_features(
        [case], CRIME_TYPE_SEVERITY, reference_date=date(2024, 2, 10)
    )

    assert row["accused_id"] == "ACC-00001"
    assert row["offense_count"] == 1
    assert row["recidivism_interval_avg"] is None  # only one offense, no interval
    assert row["crime_type_severity_max"] == 2
    assert row["district_spread"] == 1
    assert row["co_accused_count"] == 0
    assert row["days_since_last_offense"] == 31
    assert row["modus_operandi_consistency"] == 1


def test_repeat_offender_across_districts_and_crime_types():
    case = AccusedCase(
        accused_id="ACC-00002",
        offenses=(
            Offense(
                fir_id="FIR-000010",
                date_of_incident=date(2023, 1, 1),
                district_id="DIST-A-ROWID",
                crime_type="Theft",
                modus_operandi="Snatch and run",
            ),
            Offense(
                fir_id="FIR-000020",
                date_of_incident=date(2023, 4, 1),
                district_id="DIST-B-ROWID",
                crime_type="Robbery / dacoity",
                modus_operandi="Armed threat",
            ),
            Offense(
                fir_id="FIR-000030",
                date_of_incident=date(2023, 7, 30),
                district_id="DIST-A-ROWID",
                crime_type="Assault / hurt",
                modus_operandi="Physical altercation",
            ),
        ),
        co_accused_count=2,
    )

    [row] = build_accused_features(
        [case], CRIME_TYPE_SEVERITY, reference_date=date(2023, 12, 31)
    )

    assert row["offense_count"] == 3
    # intervals: Jan1->Apr1 = 90 days, Apr1->Jul30 = 120 days -> avg 105
    assert row["recidivism_interval_avg"] == 105
    assert row["crime_type_severity_max"] == 4  # Robbery / dacoity
    assert row["district_spread"] == 2  # DIST-A, DIST-B
    assert row["co_accused_count"] == 2
    assert row["days_since_last_offense"] == (date(2023, 12, 31) - date(2023, 7, 30)).days
    assert row["modus_operandi_consistency"] == 0  # 3 different MOs


def test_unknown_crime_type_defaults_severity_to_one():
    case = AccusedCase(
        accused_id="ACC-00003",
        offenses=(
            Offense(
                fir_id="FIR-000040",
                date_of_incident=date(2024, 5, 5),
                district_id="DIST-A-ROWID",
                crime_type="Some Unmapped Category",
                modus_operandi="Unknown",
            ),
        ),
        co_accused_count=0,
    )

    [row] = build_accused_features(
        [case], CRIME_TYPE_SEVERITY, reference_date=date(2024, 5, 6)
    )
    assert row["crime_type_severity_max"] == 1


def test_multiple_accused_produce_one_row_each():
    cases = [
        AccusedCase("ACC-A", (), 0),
        AccusedCase("ACC-B", (), 1),
    ]
    rows = build_accused_features(cases, CRIME_TYPE_SEVERITY, reference_date=date(2024, 1, 1))
    assert [r["accused_id"] for r in rows] == ["ACC-A", "ACC-B"]
    assert rows[0]["offense_count"] == 0
    assert rows[0]["days_since_last_offense"] is None
    assert rows[0]["recidivism_interval_avg"] is None


def test_hotspot_single_group_no_history():
    firs = [
        FirRecord("FIR-1", "DIST-A", "Theft", date(2024, 3, 5), "NONE"),
        FirRecord("FIR-2", "DIST-A", "Theft", date(2024, 3, 20), "NONE"),
    ]
    rows = build_hotspot_features(firs)
    assert len(rows) == 1
    row = rows[0]
    assert row["district"] == "DIST-A"
    assert row["crime_type"] == "Theft"
    assert row["year"] == 2024
    assert row["month"] == 3
    assert row["fir_count"] == 2
    assert row["event_context_flag"] == 0
    assert row["event_type"] == "NONE"
    # No Jan/Feb 2024 data for this group -> rolling avg over [Jan=0, Feb=0, Mar=2]
    assert row["rolling_3m_avg"] == (0 + 0 + 2) / 3
    # No 2023-03 data -> yoy_delta = fir_count - 0
    assert row["yoy_delta"] == 2


def test_hotspot_event_context_and_yoy_delta():
    firs = [
        FirRecord("FIR-1", "DIST-A", "Theft", date(2023, 3, 5), "NONE"),
        FirRecord("FIR-2", "DIST-A", "Theft", date(2023, 3, 6), "NONE"),
        FirRecord("FIR-3", "DIST-A", "Theft", date(2024, 1, 10), "NONE"),
        FirRecord("FIR-4", "DIST-A", "Theft", date(2024, 2, 15), "NONE"),
        FirRecord("FIR-5", "DIST-A", "Theft", date(2024, 3, 1), "Dasara"),
        FirRecord("FIR-6", "DIST-A", "Theft", date(2024, 3, 12), "Dasara"),
        FirRecord("FIR-7", "DIST-A", "Theft", date(2024, 3, 20), "NONE"),
    ]
    rows = build_hotspot_features(firs)
    march_2024 = next(r for r in rows if r["year"] == 2024 and r["month"] == 3)

    assert march_2024["fir_count"] == 3
    assert march_2024["event_context_flag"] == 1
    assert march_2024["event_type"] == "Dasara"
    # Jan 2024 (1) + Feb 2024 (1) + Mar 2024 (3) -> avg 5/3
    assert march_2024["rolling_3m_avg"] == (1 + 1 + 3) / 3
    # March 2023 had 2 FIRs -> yoy_delta = 3 - 2 = 1
    assert march_2024["yoy_delta"] == 1
