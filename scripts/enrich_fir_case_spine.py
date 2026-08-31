#!/usr/bin/env python3
"""Backfill thin FIR case spine onto an existing entities dir (Auto/v1.0/06).

Does **not** reshuffle narratives or FIR ids. Adds:
  - firs: case_category, gravity, complainant_id, brief_facts
  - complainants.json
  - fir_act_sections.json

Complainant full_name is synthetic (deterministic per fir_id).

Usage:
  .venv/bin/python scripts/enrich_fir_case_spine.py --entities-dir data/entities
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from faker import Faker

from generate_entities import (
    CASE_CATEGORY_FIR,
    brief_facts_from_fir,
    build_complainant,
    fir_act_section_rows,
    gravity_for_crime_category,
    person_name,
)

_FIR_NUM = re.compile(r"FIR-(\d+)$", re.I)


def _fir_ordinal(fir_id: str) -> int:
    m = _FIR_NUM.match(fir_id.strip())
    if not m:
        raise ValueError(f"unexpected fir_id: {fir_id}")
    return int(m.group(1))


def enrich(entities_dir: Path, seed: int) -> None:
    firs_path = entities_dir / "firs.json"
    firs = json.loads(firs_path.read_text())

    complainants = []
    fir_act_sections = []
    for fir in firs:
        fir_id = fir["fir_id"]
        n = _fir_ordinal(fir_id)
        # Per-FIR deterministic RNG / Faker — no cross-FIR stream coupling.
        local_seed = seed * 1_000_003 + n
        random.seed(local_seed)
        fake = Faker("en_IN")
        Faker.seed(local_seed)
        full_name = person_name(fake)
        cmp_rng = random.Random(local_seed + 17)

        complainant_id = f"CMP-{n:06d}"
        complainants.append(build_complainant(fake, complainant_id, full_name, cmp_rng))
        fir_act_sections.extend(
            fir_act_section_rows(fir_id, fir["legal_code"], fir.get("sections_cited") or [])
        )

        fir["case_category"] = fir.get("case_category") or CASE_CATEGORY_FIR
        fir["gravity"] = fir.get("gravity") or gravity_for_crime_category(fir["crime_type"])
        fir["complainant_id"] = complainant_id
        fir["brief_facts"] = fir.get("brief_facts") or brief_facts_from_fir(fir)

    firs_path.write_text(json.dumps(firs, indent=2) + "\n")
    (entities_dir / "complainants.json").write_text(json.dumps(complainants, indent=2) + "\n")
    (entities_dir / "fir_act_sections.json").write_text(
        json.dumps(fir_act_sections, indent=2) + "\n"
    )

    manifest_path = entities_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        counts = manifest.setdefault("counts", {})
        counts["firs.json"] = len(firs)
        counts["complainants.json"] = len(complainants)
        counts["fir_act_sections.json"] = len(fir_act_sections)
        manifest["case_spine"] = {"version": "v1.0/06", "seed": seed}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    print(
        f"[enrich_fir_case_spine] firs={len(firs)} complainants={len(complainants)} "
        f"fir_act_sections={len(fir_act_sections)} -> {entities_dir}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entities-dir", type=Path, default=Path("data/entities"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    enrich(args.entities_dir, args.seed)


if __name__ == "__main__":
    main()
