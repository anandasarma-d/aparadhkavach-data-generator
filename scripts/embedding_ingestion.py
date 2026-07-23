#!/usr/bin/env python3
"""AparadhKavach synthetic dataset — Phase 4: PgVector Embedding Ingestion.

Spec source: Notion Section 4.5 Phase 4 ("PgVector Population") and ADR-025
(Voyage AI voyage-3-large, output_dimension=1024), fetched fresh 2026-07-15.

For each FIR (read from data/entities/firs.json, the Phase 1 output — this
script does not touch Catalyst DataStore, which is held pending the staging
project per the 15 Jul 2026 decision):
  content = narrative_text + " " + crime_type + " " + modus_operandi
  vector = Voyage AI voyage-3-large, output_dimension=1024
  PgVector.store(vector, metadata={fir_id, district, crime_type, date_filed, status})

IVFFlat (cosine) index is created only after all embeddings are loaded, per
Phase 4's explicit ordering ("IVFFlat index created after all embeddings
loaded"). lists=61 = ceil(sqrt(3720)) per this repo's AGENTS.md - recompute
if the FIR volume changes, don't hardcode a different number.

Connection is read from .env (PGVECTOR_HOST/PORT/DB/USER/PASSWORD) and
VOYAGE_API_KEY via python-dotenv, same convention as neo4j_populate.py -
this script runs manually against local PgVector (Docker, Section 12.1) or
a hosted instance unmodified.

Idempotent / resumable: fir_ids already present in fir_embeddings are
skipped on re-run (e.g. after a transient Voyage API failure partway
through), rather than re-embedding and re-spending API calls.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import psycopg
import voyageai
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

EMBEDDING_MODEL = "voyage-3-large"
EMBEDDING_DIMENSIONS = 1024
BATCH_SIZE = 20
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
# This Voyage account has no payment method on file, which caps it at 3 RPM /
# 10K TPM (confirmed via a live RateLimitError, not documentation) rather than
# the standard tier's limits. Content averages ~139 tokens/FIR (measured via
# client.count_tokens), so BATCH_SIZE=20 (~2,780 tokens/request) keeps token
# usage well under 10K/min. Pacing at exactly 3.0 req/min (20s spacing) still
# tripped the limiter in practice after ~18 requests - hugging the boundary
# leaves no room for clock/request-latency jitter pushing a request just
# inside the prior window. 2.5 req/min (24s spacing) is the empirically
# adjusted default; embed_batch's rate-limit-specific backoff is the
# fallback if this still isn't enough margin.
REQUESTS_PER_MINUTE = 2.5


def load_firs(entities_dir: Path) -> list[dict]:
    with (entities_dir / "firs.json").open() as f:
        return json.load(f)


def build_content(fir: dict) -> str:
    return f"{fir['narrative_text']} {fir['crime_type']} {fir['modus_operandi']}"


def connect_pgvector(args) -> psycopg.Connection:
    conn = psycopg.connect(
        host=os.environ.get("PGVECTOR_HOST", "localhost"),
        port=os.environ.get("PGVECTOR_PORT", "5432"),
        dbname=os.environ.get("PGVECTOR_DB", "aparadhkavach"),
        user=os.environ.get("PGVECTOR_USER", "postgres"),
        password=os.environ.get("PGVECTOR_PASSWORD"),
        autocommit=False,
    )
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    conn.commit()
    register_vector(conn)
    return conn


def ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS fir_embeddings (
            fir_id TEXT PRIMARY KEY,
            embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL,
            district TEXT NOT NULL,
            crime_type TEXT NOT NULL,
            date_filed DATE NOT NULL,
            status TEXT NOT NULL
        );
    """)
    conn.commit()


def already_loaded(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT fir_id FROM fir_embeddings;").fetchall()
    return {r[0] for r in rows}


def embed_batch(client: voyageai.Client, texts: list[str]) -> list[list[float]]:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = client.embed(
                texts,
                model=EMBEDDING_MODEL,
                input_type="document",
                output_dimension=EMBEDDING_DIMENSIONS,
            )
            return result.embeddings
        except Exception as e:  # noqa: BLE001 - Voyage SDK raises several distinct exception types
            last_err = e
            if attempt < MAX_RETRIES:
                # A 429 means the account's per-minute budget is exhausted right now -
                # a short escalating backoff (RETRY_BACKOFF_SECONDS) can't fix that,
                # only waiting out the window can. Non-rate-limit errors still use the
                # short backoff, since those are more likely transient network blips.
                is_rate_limit = type(e).__name__ == "RateLimitError"
                wait = 65 if is_rate_limit else RETRY_BACKOFF_SECONDS * attempt
                print(f"  [warn] Voyage embed call failed (attempt {attempt}/{MAX_RETRIES}): {e}. "
                      f"Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"Voyage embed call failed after {MAX_RETRIES} attempts") from last_err


def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def create_ivfflat_index(conn: psycopg.Connection, lists: int) -> None:
    conn.execute("DROP INDEX IF EXISTS fir_embeddings_ivfflat_cosine;")
    conn.execute(f"""
        CREATE INDEX fir_embeddings_ivfflat_cosine
        ON fir_embeddings
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = {lists});
    """)
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="AparadhKavach Phase 4 - PgVector embedding ingestion")
    parser.add_argument("--entities-dir", type=Path, default=Path("data/entities"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--requests-per-minute", type=float, default=REQUESTS_PER_MINUTE,
                         help="proactive pacing to stay under the Voyage account's rate limit")
    parser.add_argument("--skip-index", action="store_true",
                         help="skip IVFFlat index creation (e.g. to resume ingestion later)")
    args = parser.parse_args()

    load_dotenv(dotenv_path=args.env_file)
    voyage_key = os.environ.get("VOYAGE_API_KEY")
    if not voyage_key:
        print("ERROR: VOYAGE_API_KEY is not set (via .env or the environment). "
              "See .env.example. Get a key from Voyage AI's dashboard "
              "(External Services Provisioning Guide, Tier 1).", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("PGVECTOR_PASSWORD"):
        print("ERROR: PGVECTOR_PASSWORD is not set (via .env or the environment). "
              "See .env.example.", file=sys.stderr)
        sys.exit(1)

    firs = load_firs(args.entities_dir)
    print(f"[embedding_ingestion] {len(firs)} FIRs loaded from {args.entities_dir / 'firs.json'}")

    conn = connect_pgvector(args)
    ensure_table(conn)

    done = already_loaded(conn)
    pending = [f for f in firs if f["fir_id"] not in done]
    print(f"[embedding_ingestion] {len(done)} already embedded, {len(pending)} pending")

    client = voyageai.Client(api_key=voyage_key)

    min_interval = 60.0 / args.requests_per_minute if args.requests_per_minute > 0 else 0.0
    total_batches = math.ceil(len(pending) / args.batch_size) if pending else 0
    if min_interval:
        eta_min = total_batches * min_interval / 60.0
        print(f"[embedding_ingestion] pacing at {args.requests_per_minute} req/min "
              f"({total_batches} batches, ETA ~{eta_min:.1f} min)")

    inserted = 0
    last_request_time = None
    for batch in chunked(pending, args.batch_size):
        if min_interval and last_request_time is not None:
            elapsed = time.monotonic() - last_request_time
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
        texts = [build_content(f) for f in batch]
        last_request_time = time.monotonic()
        vectors = embed_batch(client, texts)
        with conn.cursor() as cur:
            for fir, vector in zip(batch, vectors):
                cur.execute(
                    """
                    INSERT INTO fir_embeddings (fir_id, embedding, district, crime_type, date_filed, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (fir_id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        district = EXCLUDED.district,
                        crime_type = EXCLUDED.crime_type,
                        date_filed = EXCLUDED.date_filed,
                        status = EXCLUDED.status;
                    """,
                    (fir["fir_id"], vector, fir["district"], fir["crime_type"],
                     fir["date_filed"], fir["status"]),
                )
        conn.commit()
        inserted += len(batch)
        print(f"  embedded {inserted}/{len(pending)} pending FIRs "
              f"({len(done) + inserted}/{len(firs)} total)")

    total = conn.execute("SELECT count(*) FROM fir_embeddings;").fetchone()[0]
    print(f"[embedding_ingestion] fir_embeddings now has {total} rows")

    if not args.skip_index:
        if total < len(firs):
            print(f"[embedding_ingestion] WARNING: {total}/{len(firs)} FIRs loaded - "
                  f"index not created (use --skip-index to load an index later once complete)",
                  file=sys.stderr)
        else:
            lists = math.ceil(math.sqrt(total))
            print(f"[embedding_ingestion] creating IVFFlat (cosine) index, lists={lists} "
                  f"(ceil(sqrt({total})))")
            create_ivfflat_index(conn, lists)
            print("[embedding_ingestion] index created: fir_embeddings_ivfflat_cosine")

    conn.close()
    print("[embedding_ingestion] done.")


if __name__ == "__main__":
    main()
