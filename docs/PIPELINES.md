# Pipelines — DataStore, features, QuickML (MVP-1)

These scripts turn the committed graph/FIR corpus into tables and CSVs that Catalyst DataStore and QuickML consume. Exact table names and import jobs are operated in the Catalyst console; this page maps **which script produces what**.

## DataStore-oriented CSVs

| Script | Output (typical) | Notes |
| --- | --- | --- |
| `catalyst_datastore_transform.py` | `data/catalyst_datastore/*.csv` | Accused, FIRs, districts, officers, victims, … |
| `feature_builder.py` | Feature frames for risk ML | Flat + engineered columns |
| `neo4j_accused_features_driver.py` | Graph-backed accused features | Needs populated Neo4j |
| `generate_synthetic_risk_label.py` | Risk labels for training/scoring | Synthetic label path |

## Scoring / forecast CSVs

| Script | Output | Notes |
| --- | --- | --- |
| `quickml_scorer.py` | `risk_scores`-shaped CSV | Calls live QuickML HTTP scoring; supports resume |
| `seed_hotspot_forecasts.py` | `hotspot_forecasts` seed CSV | MVP-1 pragmatic seed when a full live hotspot endpoint path is not used |

Import CSVs into Catalyst DataStore with the project’s `ds:import` / console import flow (operators only — not automated from CI in MVP-1).

## Validation helpers

| Script | Role |
| --- | --- |
| `guardrail_validator.py` | Gate generate/weave quality |
| `verify_repeat_offender_rate.py` | Corpus rate sanity |
| `verify_accused_features_cross_validation.py` | Feature cross-checks |

## Tests

```bash
pytest tests/
```

Covers feature builder and QuickML scorer contracts (HTTP-mocked where applicable).
