"""Unit tests for Auto/v1.0/06 thin FIR case spine helpers."""

from __future__ import annotations

import random

from faker import Faker

from generate_entities import (
    CASE_CATEGORY_FIR,
    GRAVITY_HEINOUS,
    GRAVITY_NON_HEINOUS,
    brief_facts_from_fir,
    build_complainant,
    fir_act_section_rows,
    gravity_for_crime_category,
)


def test_gravity_heinous_vs_not():
    assert gravity_for_crime_category("Robbery / dacoity") == GRAVITY_HEINOUS
    assert gravity_for_crime_category("Theft") == GRAVITY_NON_HEINOUS


def test_fir_act_section_rows_orders_sections():
    rows = fir_act_section_rows("FIR-000001", "IPC", ["379", "380"])
    assert len(rows) == 2
    assert rows[0]["act_code"] == "IPC"
    assert rows[0]["section_code"] == "379"
    assert rows[0]["section_order"] == 1
    assert rows[1]["section_order"] == 2


def test_build_complainant_has_no_caste_religion():
    fake = Faker("en_IN")
    c = build_complainant(fake, "CMP-000001", "Test Name", random.Random(1))
    assert c["complainant_id"] == "CMP-000001"
    assert "caste" not in c and "religion" not in c
    assert CASE_CATEGORY_FIR == "FIR"


def test_brief_facts_prefers_narrative_core():
    fir = {
        "narrative_core": "Short core.",
        "narrative_text": "A much longer narrative that should not win.",
    }
    assert brief_facts_from_fir(fir) == "Short core."
