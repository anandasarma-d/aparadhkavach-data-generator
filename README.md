# AparadhKavach Data Generator

Python tooling and **canonical synthetic corpus** for AparadhKavach (KSP Datathon 2026 MVP-1).

**Judge checkout:** default branch `main`, or tag `mvp1-submission-2026-07-26`  
**Feeds:** Catalyst DataStore imports · Neo4j Aura · PgVector (`fir_embeddings`) · QuickML training/scoring CSVs

```text
generate / weave  ──►  data/entities + data/relationships   (committed)
        │
        ├── neo4j_populate.py          → Neo4j (graph)
        ├── embedding_ingestion.py     → PgVector (Voyage embeddings)
        ├── catalyst_datastore_transform.py → DataStore CSVs
        ├── feature_builder.py + neo4j_accused_features_driver.py → accused_features
        └── quickml_scorer.py / seed_hotspot_forecasts.py → risk_scores / hotspot_forecasts CSVs
```

---

## What this repo is (and is not)

| Is | Is not |
| --- | --- |
| Schema-faithful **synthetic** KSP-scale data | Production police data |
| Reproducible loaders + validators | The live Slate UI ([client](https://github.com/anandasarma-d/aparadhkavach-client)) |
| QuickML CSV / seed pipelines | The AppSail Java services ([services](https://github.com/anandasarma-d/aparadhkavach-services)) |

Committed outputs under `data/` are the **canonical** generate+weave result (guardrail-validated). Prefer loading that corpus over regenerating unless you intentionally want a new dataset version.

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill Neo4j, PgVector, VOYAGE_API_KEY as needed
```

**Do not** run `scripts/generate_entities.py` or `scripts/weave_relationships.py` just to “get data locally” — that reshuffles the corpus. Use the committed `data/` tree + populate/embed scripts instead.

---

## MVP-1 script map

| Script | Purpose |
| --- | --- |
| `neo4j_populate.py` | Load entities + relationships into Neo4j; Level-2 structural checks |
| `embedding_ingestion.py` | Voyage `voyage-3-large` embeddings → `fir_embeddings` (idempotent) |
| `enrich_fir_case_spine.py` | **v1.0/06** — backfill complainants + fir_act_sections + fir case fields without reshuffling narratives |
| `semantic_validation.py` | Level-3 semantic checks on embeddings |
| `catalyst_datastore_transform.py` | Flat CSVs for Catalyst DataStore import |
| `feature_builder.py` | Accused feature engineering |
| `neo4j_accused_features_driver.py` | Pull graph-derived accused features from Neo4j |
| `generate_synthetic_risk_label.py` | Risk label helpers for ML |
| `quickml_scorer.py` | Call QuickML scoring endpoint → `risk_scores` CSV |
| `seed_hotspot_forecasts.py` | Pragmatic hotspot forecast seed CSV (MVP-1 path) |
| `guardrail_validator.py` | Dataset gate before accepting a weave |

Full load walkthrough: [docs/LOCAL_LOAD.md](docs/LOCAL_LOAD.md)  
Pipelines & DataStore: [docs/PIPELINES.md](docs/PIPELINES.md)

---

## Related repos

| Repo | Role |
| --- | --- |
| [aparadhkavach-client](https://github.com/anandasarma-d/aparadhkavach-client) | React UI on Slate |
| [aparadhkavach-services](https://github.com/anandasarma-d/aparadhkavach-services) | AppSail Gateway + domain services |

Live demo: [https://aparadhkavach.onslate.in/](https://aparadhkavach.onslate.in/)

---

## Notion MCP (contributors)

Copy `.cursor.mcp.json.example` → `.cursor/mcp.json` with a read-only Notion token. Never commit `.cursor/mcp.json`.
