"""Unit tests for feature_builder.py. No live Catalyst/Neo4j connection — small local
fixtures only, per this task's explicit scope (feature_builder.py only needs live data to
run for real, which is out of scope here).

The fixtures below deliberately include repeat offenders and multi-district accused, unlike
the real committed accused_persons.csv (100% prior_offense_count == 0 — see feature_builder.py's
module docstring for why) — this is what a real Neo4j ACCUSED_IN join would actually look like.
"""

import csv
from datetime import date

import pytest

from feature_builder import (
    AccusedCase,
    FirRecord,
    Offense,
    build_accused_features,
    build_hotspot_features,
    load_fir_records,
    write_feature_csv,
)

# Header + rows copied verbatim from the committed data/catalyst_datastore/firs.csv, which is
# already in live/post-two-pass-FK-load shape (district_id holds a Catalyst ROWID, not
# "DIST-01") -- this is what proves load_fir_records works against real export shape, not
# just an invented fixture.
LIVE_SHAPED_FIRS_HEADER = (
    "fir_id,fir_number,district_id,police_station,date_filed,date_of_incident,crime_type,"
    "legal_code,sections_cited,status,narrative_text,modus_operandi,event_context,"
    "investigation_stage,created_at,updated_at,created_by,updated_by"
)
LIVE_SHAPED_FIRS_ROWS = [
    'FIR-000001,01/2021/000001,42963000000035012,Bagalkot Police Station 2,2021-01-25,'
    '2021-01-25 00:00:00,Assault / hurt,IPC,394,UNDER_INVESTIGATION,"Narrative text.",'
    "Physical altercation,NONE,FIR_REGISTERED,2026-07-15 14:36:44,2026-07-15 14:36:44,"
    "SYSTEM_SEED,SYSTEM_SEED",
    'FIR-000002,01/2021/000002,42963000000035012,Bagalkot Police Station 3,2021-03-25,'
    '2021-01-12 00:00:00,Vehicle theft / snatching,IPC,"379,411",CHARGESHEETED,'
    '"Narrative text.",Snatch and run,UGADI,CHARGESHEET_FILED,2026-07-15 14:36:44,'
    "2026-07-15 14:36:44,SYSTEM_SEED,SYSTEM_SEED",
]

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


def test_modus_operandi_none_yields_unknown_consistency():
    # Neo4j-sourced offenses: modus_operandi is None (no such property on :FIR — see
    # feature_builder.py module docstring). Must not silently read as "1 (consistent)".
    case = AccusedCase(
        accused_id="ACC-NEO4J-1",
        offenses=(
            Offense("FIR-1", date(2024, 1, 1), "Bagalkot", "Theft", modus_operandi=None),
            Offense("FIR-2", date(2024, 3, 1), "Bagalkot", "Theft", modus_operandi=None),
        ),
        co_accused_count=0,
    )
    [row] = build_accused_features([case], {}, reference_date=date(2024, 4, 1))
    assert row["modus_operandi_consistency"] is None


def test_modus_operandi_present_still_computes_normally():
    # Backward-compat guard: real MO strings (the pre-Neo4j fixture path) must be unaffected.
    case = AccusedCase(
        accused_id="ACC-FIXTURE-1",
        offenses=(
            Offense("FIR-1", date(2024, 1, 1), "Bagalkot", "Theft", modus_operandi="Snatch and run"),
            Offense("FIR-2", date(2024, 3, 1), "Bagalkot", "Theft", modus_operandi="Snatch and run"),
        ),
        co_accused_count=0,
    )
    [row] = build_accused_features([case], {}, reference_date=date(2024, 4, 1))
    assert row["modus_operandi_consistency"] == 1


def test_per_offense_crime_type_severity_takes_precedence_over_dict():
    # Same crime_type category ("Domestic violence / 498A"), two different real severities
    # per-FIR (HIGH vs CRITICAL) — exactly the case a category-keyed dict alone can't represent.
    case = AccusedCase(
        accused_id="ACC-SEV-1",
        offenses=(
            Offense(
                "FIR-1", date(2024, 1, 1), "Bagalkot", "Domestic violence / 498A",
                modus_operandi=None, crime_type_severity=3,  # HIGH
            ),
            Offense(
                "FIR-2", date(2024, 3, 1), "Bagalkot", "Domestic violence / 498A",
                modus_operandi=None, crime_type_severity=4,  # CRITICAL
            ),
        ),
        co_accused_count=0,
    )
    # Dict says 1 for this category — must be ignored since both offenses set their own value.
    [row] = build_accused_features(
        [case], {"Domestic violence / 498A": 1}, reference_date=date(2024, 4, 1)
    )
    assert row["crime_type_severity_max"] == 4


def test_crime_type_severity_falls_back_to_dict_when_unset():
    case = AccusedCase(
        accused_id="ACC-SEV-2",
        offenses=(Offense("FIR-1", date(2024, 1, 1), "Bagalkot", "Theft"),),
        co_accused_count=0,
    )
    [row] = build_accused_features([case], {"Theft": 2}, reference_date=date(2024, 4, 1))
    assert row["crime_type_severity_max"] == 2


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


def test_load_fir_records_from_live_shaped_csv(tmp_path):
    csv_path = tmp_path / "firs.csv"
    csv_path.write_text(
        LIVE_SHAPED_FIRS_HEADER + "\n" + "\n".join(LIVE_SHAPED_FIRS_ROWS) + "\n"
    )

    records = load_fir_records(csv_path)

    assert len(records) == 2
    assert records[0] == FirRecord(
        fir_id="FIR-000001",
        district_id="42963000000035012",  # ROWID, not "DIST-01" -- see FirRecord docstring
        crime_type="Assault / hurt",
        date_filed=date(2021, 1, 25),
        event_context="NONE",
    )
    assert records[1].event_context == "UGADI"
    assert records[1].date_filed == date(2021, 3, 25)


def test_load_fir_records_feeds_build_hotspot_features(tmp_path):
    csv_path = tmp_path / "firs.csv"
    csv_path.write_text(
        LIVE_SHAPED_FIRS_HEADER + "\n" + "\n".join(LIVE_SHAPED_FIRS_ROWS) + "\n"
    )

    rows = build_hotspot_features(load_fir_records(csv_path))

    assert len(rows) == 2  # 2021-01 and 2021-03, different crime_type each
    assert {r["district"] for r in rows} == {"42963000000035012"}


def test_load_fir_records_missing_required_column_raises(tmp_path):
    csv_path = tmp_path / "firs.csv"
    csv_path.write_text("fir_id,district_id,date_filed,event_context\nFIR-1,D1,2021-01-01,NONE\n")

    with pytest.raises(ValueError, match="missing required firs column"):
        load_fir_records(csv_path)


def test_load_fir_records_bad_date_format_raises(tmp_path):
    csv_path = tmp_path / "firs.csv"
    csv_path.write_text(
        "fir_id,district_id,crime_type,date_filed,event_context\n"
        "FIR-1,D1,Theft,25/01/2021,NONE\n"
    )

    with pytest.raises(ValueError, match="FIR-1.*unparseable date_filed"):
        load_fir_records(csv_path)


def test_write_feature_csv_round_trip(tmp_path):
    firs = [
        FirRecord("FIR-1", "DIST-A", "Theft", date(2024, 3, 5), "NONE"),
        FirRecord("FIR-2", "DIST-A", "Theft", date(2024, 3, 20), "NONE"),
    ]
    rows = build_hotspot_features(firs)
    out_path = tmp_path / "hotspot_features.csv"

    write_feature_csv(rows, out_path)

    with open(out_path, newline="") as f:
        read_back = list(csv.DictReader(f))
    assert read_back[0]["district"] == "DIST-A"
    assert read_back[0]["fir_count"] == "2"  # csv round-trips everything as strings
    assert list(read_back[0].keys()) == list(rows[0].keys())  # column order preserved


def test_write_feature_csv_empty_rows_raises(tmp_path):
    with pytest.raises(ValueError, match="no rows to write"):
        write_feature_csv([], tmp_path / "empty.csv")
