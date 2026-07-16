# aparadhkavach-data-generator
Python repo to generate synthetic data for AparadhKavach

# Notion integration MCP
Copy .cursor.mcp.json.example to .cursor/mcp.json and fill in your own read-only Notion token (see External Services guide). Never commit .cursor/mcp.json itself.

# Loading the dataset into your own local Neo4j + PgVector

The generated dataset (`data/entities/*.json`, `data/relationships/*.csv`) is committed to
this repo — it's the canonical output of `generate_entities.py` + `weave_relationships.py`,
already passed through `guardrail_validator.py`'s gate. You do not need to regenerate it,
and you do not need an AI agent to run this for you — `neo4j_populate.py` and
`embedding_ingestion.py` are plain, deterministic Python scripts. Anyone on the team can
point them at their own local Neo4j/PgVector and get the identical dataset loaded.

**Do NOT run `generate_entities.py` or `weave_relationships.py`.** Those regenerate the
dataset (with a different random seed, producing different data) — only run them if you're
deliberately producing a new dataset version, not to load the existing one locally.

## 1. Prerequisites

- Docker (Desktop or Engine) — both Neo4j and PgVector run as local containers per
  [Section 12.1](https://app.notion.com/p/38717f7e17c081c7be4be1a100d9f51d)'s Development
  environment row. No Homebrew install needed for either — this repo's local setup uses
  Docker for both, not a mix.
- Python 3.x, then from the repo root:
  ```
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```
- Your own Voyage AI API key (`VOYAGE_API_KEY`). Sign up at Voyage AI's dashboard — free
  tier, no billing required to start (see the External Services Provisioning Guide, Tier 1).
  **Get your own key, don't share one across teammates** — keys are tied to individual
  accounts/rate limits and Voyage tracks usage per key.

## 2. Start local Neo4j and PgVector containers

```bash
# Neo4j (Bolt on 7687, browser UI on 7474)
docker run -d --name aparadhkavach-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/localdevpassword \
  neo4j:5-community

# PgVector (Postgres 16 + pgvector extension, on the standard 5432)
docker run -d --name aparadhkavach-pgvector \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=localdevpassword \
  -e POSTGRES_DB=aparadhkavach \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

`embedding_ingestion.py` enables the `vector` extension automatically on first connect
(`CREATE EXTENSION IF NOT EXISTS vector;`) — no manual step needed, but you can verify it
yourself with:
```bash
docker exec aparadhkavach-pgvector psql -U postgres -d aparadhkavach -c "\dx"
```

## 3. Configure `.env`

Copy `.env.example` to `.env` and fill in your own `VOYAGE_API_KEY`. The scripts read
these exact variables (nothing else, no other names or aliases):

| Variable | Read by | Notes |
|---|---|---|
| `NEO4J_URI` | `neo4j_populate.py` | `bolt://localhost:7687` for the container above |
| `NEO4J_USERNAME` | `neo4j_populate.py` | `neo4j` |
| `NEO4J_PASSWORD` | `neo4j_populate.py` | `localdevpassword` for the container above |
| `PGVECTOR_HOST` | `embedding_ingestion.py` | `localhost` (defaults to this if unset) |
| `PGVECTOR_PORT` | `embedding_ingestion.py` | `5432` (defaults to this if unset) |
| `PGVECTOR_DB` | `embedding_ingestion.py` | `aparadhkavach` (defaults to this if unset) |
| `PGVECTOR_USER` | `embedding_ingestion.py` | `postgres` (defaults to this if unset) |
| `PGVECTOR_PASSWORD` | `embedding_ingestion.py` | no default — script exits with an error if unset |
| `VOYAGE_API_KEY` | `embedding_ingestion.py` | your own personal key, never commit it |

## 4. Run order

```bash
# 1. Neo4j: MERGE nodes + relationships, create indexes, run Level 2 validation
python3 neo4j_populate.py

# 2. PgVector: embed every FIR's narrative_text + crime_type + modus_operandi via
#    Voyage voyage-3-large (1024-dim, ADR-025), create the IVFFlat cosine index once
#    all ~3,720 rows are loaded. Idempotent/resumable - safe to re-run if it's
#    interrupted partway (e.g. a Voyage rate-limit error), it skips FIRs already
#    embedded rather than re-embedding and re-spending API calls.
python3 embedding_ingestion.py
```

Note: on a free/no-billing-method Voyage account, expect a strict rate limit (observed:
3 requests/min, 10K tokens/min) rather than the standard tier's limits — a full ~3,720-FIR
run can take on the order of an hour under that cap. Adding a payment method in Voyage's
dashboard (still covered by the free token allotment) removes this cap if you want it
faster.

## 5. Verify your local instance matches

Don't just check that the scripts exited 0 — confirm the actual data matches:

- **Neo4j (Level 2 - structural):** `neo4j_populate.py` runs
  [Section 4.7](https://app.notion.com/p/38717f7e17c081a1959fd4ed3f644ccd)'s Level 2 Cypher
  validation queries automatically at the end (unless run with `--skip-validation`) and
  prints actual vs. expected counts (isolated nodes, repeat offenders, cross-district
  accused, hotspot locations, CrimeType coverage).
- **PgVector (Level 3 - semantic):** run `python3 semantic_validation.py` for the 5 checks
  from Section 4.7 Level 3 (same-category similarity, cross-category dissimilarity,
  similarity gradient, no near-duplicates, cross-regime narrative similarity). It prints
  the actual computed numbers, not just pass/fail.
- Fetch [Section 4.7](https://app.notion.com/p/38717f7e17c081a1959fd4ed3f644ccd) from
  Notion for the current pass criteria before comparing — thresholds are versioned there,
  not duplicated here, so this doesn't go stale if they change.