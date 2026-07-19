#!/usr/bin/env python3
"""AparadhKavach synthetic dataset — Level 3 Semantic Validation (PgVector).

Spec source: Notion Section 4.7 ("Level 3 — Semantic Validation (PgVector
similarity spot checks)"), plus the "Cross-regime narrative similarity"
row of Section 4.7's Level 1 table (deferred there because it needs
embeddings that don't exist until Phase 4 - guardrail_validator.py reports
it as SKIP with that explanation), fetched fresh 2026-07-15.

Runs against the fir_embeddings table populated by embedding_ingestion.py.
Computes exact cosine similarity in-process (numpy) over the full
~3,720 x ~3,720 pairwise matrix rather than querying through the IVFFlat
index - IVFFlat is an *approximate* nearest-neighbor structure (intended
for production query latency, per Phase 4), and these are correctness
checks against fixed pass criteria, so approximate recall would make the
reported numbers a weaker (and non-reproducible across index rebuilds)
proxy for the true similarity distribution. At this row count (~3,720),
an exact in-memory computation is fast and simpler than working around
ANN approximation error.

This script reports raw computed numbers for all 5 checks - it does not
reduce the result to a pass/fail summary. Compare the printed numbers
against Section 4.7's stated pass criteria yourself.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

N_QUERIES = 10
TOP_K = 5
GRADIENT_TOP_N = 10
NEAR_DUPLICATE_THRESHOLD = 0.95
BNS_TRANSITION_DATE = "2024-07-01"  # Section 4.4 / ADR-015
RNG_SEED = 42


def connect(args) -> psycopg.Connection:
    conn = psycopg.connect(
        host=os.environ.get("PGVECTOR_HOST", "localhost"),
        port=os.environ.get("PGVECTOR_PORT", "5432"),
        dbname=os.environ.get("PGVECTOR_DB", "aparadhkavach"),
        user=os.environ.get("PGVECTOR_USER", "postgres"),
        password=os.environ.get("PGVECTOR_PASSWORD"),
    )
    register_vector(conn)
    return conn


def load_all(conn: psycopg.Connection):
    rows = conn.execute(
        "SELECT fir_id, district, crime_type, date_filed, status, embedding "
        "FROM fir_embeddings ORDER BY fir_id;"
    ).fetchall()
    fir_ids = [r[0] for r in rows]
    crime_types = np.array([r[2] for r in rows])
    date_filed = np.array([str(r[3]) for r in rows])
    # pgvector-python (>=0.4) returns a pgvector.Vector wrapper from register_vector,
    # not a raw ndarray - .to_numpy() unwraps it (float32; upcast to float64 for the
    # matrix multiply below).
    embeddings = np.stack([r[5].to_numpy().astype(np.float64) for r in rows])
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    normalized = embeddings / norms
    return fir_ids, crime_types, date_filed, normalized


def cosine_matrix(normalized: np.ndarray) -> np.ndarray:
    return normalized @ normalized.T


# ---------------------------------------------------------------------------
# Level 3 checks
# ---------------------------------------------------------------------------

def check_same_category_similarity(sim, crime_types, query_idx):
    print("-" * 88)
    print(f"1. Same-category similarity ({N_QUERIES} within-category queries, top-{TOP_K})")
    print("   Pass criterion: average top-5 cosine similarity 0.65-0.85")
    print("-" * 88)
    per_query = []
    for q in query_idx:
        category = crime_types[q]
        candidates = np.where((crime_types == category) & (np.arange(len(crime_types)) != q))[0]
        scores = np.sort(sim[q, candidates])[::-1][:TOP_K]
        avg = scores.mean()
        per_query.append(avg)
        print(f"   query={q:>5} category={category:<28} top-5={np.round(scores, 4).tolist()} avg={avg:.4f}")
    overall = float(np.mean(per_query))
    print(f"   => AVERAGE OF {N_QUERIES} QUERY-LEVEL TOP-5 AVERAGES: {overall:.4f}")
    return overall, per_query


def check_cross_category_dissimilarity(sim, crime_types, query_idx, rng):
    print("-" * 88)
    print(f"2. Cross-category dissimilarity ({N_QUERIES} cross-category queries, top-{TOP_K})")
    print("   Pass criterion: average top-5 cosine similarity < 0.50")
    print("-" * 88)
    categories = sorted(set(crime_types.tolist()))
    per_query = []
    for q in query_idx:
        own_category = crime_types[q]
        other_categories = [c for c in categories if c != own_category]
        other_category = other_categories[int(rng.integers(0, len(other_categories)))]
        candidates = np.where(crime_types == other_category)[0]
        scores = np.sort(sim[q, candidates])[::-1][:TOP_K]
        avg = scores.mean()
        per_query.append(avg)
        print(f"   query={q:>5} {own_category:<28} vs {other_category:<28} "
              f"top-5={np.round(scores, 4).tolist()} avg={avg:.4f}")
    overall = float(np.mean(per_query))
    print(f"   => AVERAGE OF {N_QUERIES} QUERY-LEVEL TOP-5 AVERAGES: {overall:.4f}")
    return overall, per_query


def check_similarity_gradient(sim, query_idx):
    print("-" * 88)
    print(f"3. Similarity gradient existence (rank-1 vs rank-{GRADIENT_TOP_N}, global top-{GRADIENT_TOP_N})")
    print("   Pass criterion: rank-1 score > rank-10 score by at least 0.15")
    print("-" * 88)
    per_query_gap = []
    for q in query_idx:
        candidates = np.where(np.arange(sim.shape[0]) != q)[0]
        scores = np.sort(sim[q, candidates])[::-1][:GRADIENT_TOP_N]
        rank1, rank10 = scores[0], scores[GRADIENT_TOP_N - 1]
        gap = rank1 - rank10
        per_query_gap.append(gap)
        print(f"   query={q:>5} rank1={rank1:.4f} rank10={rank10:.4f} gap={gap:.4f}")
    overall = float(np.mean(per_query_gap))
    print(f"   => AVERAGE GAP ACROSS {N_QUERIES} QUERIES: {overall:.4f}")
    return overall, per_query_gap


def check_no_near_duplicates(sim, fir_ids):
    print("-" * 88)
    print(f"4. No near-duplicate narratives (full {sim.shape[0]}x{sim.shape[0]} pairwise, top-1 per FIR)")
    print(f"   Pass criterion: no FIR has similarity > {NEAR_DUPLICATE_THRESHOLD} with any other FIR")
    print("-" * 88)
    n = sim.shape[0]
    sim_no_self = sim.copy()
    np.fill_diagonal(sim_no_self, -1.0)
    top1 = sim_no_self.max(axis=1)
    top1_partner = sim_no_self.argmax(axis=1)
    max_overall = float(top1.max())
    violators = np.where(top1 > NEAR_DUPLICATE_THRESHOLD)[0]
    print(f"   max top-1 similarity found anywhere in the dataset: {max_overall:.4f}")
    print(f"   FIRs with top-1 similarity > {NEAR_DUPLICATE_THRESHOLD}: {len(violators)}/{n}")
    if len(violators):
        worst = sorted(violators.tolist(), key=lambda i: -top1[i])[:10]
        for i in worst:
            j = top1_partner[i]
            print(f"     {fir_ids[i]} <-> {fir_ids[j]}  sim={top1[i]:.4f}")
    return max_overall, len(violators)


def check_cross_regime_similarity(sim, crime_types, date_filed, rng):
    print("-" * 88)
    print(f"5. Cross-regime narrative similarity ({N_QUERIES} pre/post-{BNS_TRANSITION_DATE} "
          f"same-category pairs, top-{TOP_K})")
    print("   Pass criterion: average similarity in the same 0.65-0.85 band as "
          "same-regime same-category pairs (check 1)")
    print("-" * 88)
    pre_mask = date_filed < BNS_TRANSITION_DATE
    post_mask = ~pre_mask
    categories = sorted(set(crime_types.tolist()))
    pre_by_category = {c: np.where(pre_mask & (crime_types == c))[0] for c in categories}
    eligible_categories = [c for c in categories if len(pre_by_category[c]) > 0]

    per_query = []
    picked = 0
    cat_cycle = list(rng.permutation(eligible_categories))
    ci = 0
    while picked < N_QUERIES:
        category = cat_cycle[ci % len(cat_cycle)]
        ci += 1
        pre_candidates = pre_by_category[category]
        q = pre_candidates[int(rng.integers(0, len(pre_candidates)))]
        post_candidates = np.where(post_mask & (crime_types == category))[0]
        if len(post_candidates) < TOP_K:
            print(f"   skipping category={category} - only {len(post_candidates)} post-regime FIRs")
            continue
        scores = np.sort(sim[q, post_candidates])[::-1][:TOP_K]
        avg = scores.mean()
        per_query.append(avg)
        print(f"   query={q:>5} category={category:<28} (pre-{BNS_TRANSITION_DATE}) "
              f"top-5 vs post-regime same-category={np.round(scores, 4).tolist()} avg={avg:.4f}")
        picked += 1
    overall = float(np.mean(per_query))
    print(f"   => AVERAGE OF {N_QUERIES} QUERY-LEVEL TOP-5 AVERAGES: {overall:.4f}")
    return overall, per_query


def main():
    parser = argparse.ArgumentParser(description="AparadhKavach Section 4.7 Level 3 semantic validation")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    load_dotenv(dotenv_path=args.env_file)
    if not os.environ.get("PGVECTOR_PASSWORD"):
        print("ERROR: PGVECTOR_PASSWORD is not set (via .env or the environment). "
              "See .env.example.", file=sys.stderr)
        sys.exit(1)

    conn = connect(args)
    fir_ids, crime_types, date_filed, normalized = load_all(conn)
    conn.close()

    n = len(fir_ids)
    print("=" * 88)
    print(f"AparadhKavach semantic_validation.py - Section 4.7 Level 3 ({n} embeddings loaded)")
    print("=" * 88)
    if n == 0:
        print("ERROR: fir_embeddings is empty - run embedding_ingestion.py first.", file=sys.stderr)
        sys.exit(1)

    sim = cosine_matrix(normalized)
    rng = np.random.default_rng(RNG_SEED)
    query_idx = rng.choice(n, size=N_QUERIES, replace=False)

    results = {}
    results["same_category"] = check_same_category_similarity(sim, crime_types, query_idx)
    results["cross_category"] = check_cross_category_dissimilarity(sim, crime_types, query_idx, rng)
    results["gradient"] = check_similarity_gradient(sim, query_idx)
    results["near_duplicates"] = check_no_near_duplicates(sim, fir_ids)
    results["cross_regime"] = check_cross_regime_similarity(sim, crime_types, date_filed, rng)

    print("=" * 88)
    print("SUMMARY - raw computed numbers (compare against Section 4.7 pass criteria yourself)")
    print("=" * 88)
    print(f"1. Same-category similarity avg top-5:      {results['same_category'][0]:.4f}  (criterion: 0.65-0.85)")
    print(f"2. Cross-category dissimilarity avg top-5:  {results['cross_category'][0]:.4f}  (criterion: < 0.50)")
    print(f"3. Similarity gradient (rank1-rank10) avg:  {results['gradient'][0]:.4f}  (criterion: >= 0.15)")
    print(f"4. Max top-1 similarity / violator count:   {results['near_duplicates'][0]:.4f} / "
          f"{results['near_duplicates'][1]} FIRs > {NEAR_DUPLICATE_THRESHOLD}  (criterion: 0 violators)")
    print(f"5. Cross-regime same-category avg top-5:    {results['cross_regime'][0]:.4f}  (criterion: 0.65-0.85, "
          f"same band as check 1)")
    print("=" * 88)


if __name__ == "__main__":
    main()
