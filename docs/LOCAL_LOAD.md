# Local load — Neo4j + PgVector

The generated dataset (`data/entities/*.json`, `data/relationships/*.csv`) is committed to this repo — canonical output of `generate_entities.py` + `weave_relationships.py`, already passed through `guardrail_validator.py`. You do **not** need to regenerate it to load locally.

**Do NOT run `generate_entities.py` or `weave_relationships.py`** unless you are deliberately producing a new dataset version (different seed → different data).

## 1. Prerequisites

- Docker (Desktop or Engine) — Neo4j and PgVector as local containers  
- Python 3.x + venv + `pip install -r requirements.txt`  
- Your own `VOYAGE_API_KEY` (do not share keys across teammates)

## 2. Start containers

```bash
docker run -d --name aparadhkavach-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/localdevpassword \
  neo4j:5-community

docker run -d --name aparadhkavach-pgvector \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=localdevpassword \
  -e POSTGRES_DB=aparadhkavach \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

`embedding_ingestion.py` runs `CREATE EXTENSION IF NOT EXISTS vector` on first connect.

## 3. Configure `.env`

Copy `.env.example` → `.env`. Variables used by the loaders:

| Variable | Read by | Typical local value |
| --- | --- | --- |
| `NEO4J_URI` | `neo4j_populate.py` | `bolt://localhost:7687` |
| `NEO4J_USERNAME` | `neo4j_populate.py` | `neo4j` |
| `NEO4J_PASSWORD` | `neo4j_populate.py` | `localdevpassword` |
| `PGVECTOR_HOST` | `embedding_ingestion.py` | `localhost` |
| `PGVECTOR_PORT` | `embedding_ingestion.py` | `5432` |
| `PGVECTOR_DB` | `embedding_ingestion.py` | `aparadhkavach` |
| `PGVECTOR_USER` | `embedding_ingestion.py` | `postgres` |
| `PGVECTOR_PASSWORD` | `embedding_ingestion.py` | required (no default) |
| `VOYAGE_API_KEY` | `embedding_ingestion.py` | your key |

**Aura:** username may be the instance id; database name is often the instance id — use `SHOW DATABASES` and `neo4j_populate.py --database <name>`.

## 4. Run order

From repo root:

```bash
python3 scripts/neo4j_populate.py
python3 scripts/embedding_ingestion.py
```

`embedding_ingestion.py` is idempotent/resumable (skips FIRs already embedded). Free-tier Voyage rate limits can make a full ~3,720 FIR run take on the order of an hour.

## 5. Verify

- **Neo4j (structural):** `neo4j_populate.py` prints Level-2 checks unless `--skip-validation`.  
- **PgVector (semantic):** `python3 scripts/semantic_validation.py`  

Pass criteria for semantic thresholds are maintained in the design docs (Section 4.7); re-fetch from Notion if comparing to a dated printout.
