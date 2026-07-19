# Phase 0 — Remaining Live/Console Steps Runbook

**Status as of 16 Jul 2026.** This is a single, ordered, copy-paste-ready sequence of every
remaining live/console action needed to take AparadhKavach from "`aparadhkavach-dev` fully
correct" to "staging and prod fully provisioned and populated." Sources: "Catalyst DataStore —
Table Creation & Population Runbook" (Notion, corrected 16 Jul), "External Services —
Provisioning & Account Setup Guide" (Notion), Section 12.1/12.2/12.3/12.5/12.9 (Notion), the
live Defect Tracker database (queried fresh, not just pages that reference it), and this
project's own Pre-Debug Hardening Pass + accused_persons research findings (16 Jul 2026).

**Nothing in this document has been executed by Claude Code.** Every command/console step
below is for Anand to run himself.

---

## 0. Pre-flight — read this before running anything

- **Catalyst DataStore's Bulk Write free tier is 5,000 insertions/month, account-wide** —
  not per-project. This project's `aparadhkavach-dev` imports already used ~12,291 rows this
  month (districts 31 + firs 3,720 + accused_persons 4,460 + victims 3,900 + officers 181),
  and hit this cap once already during the `accused_persons` re-import (16 Jul 2026 Defect
  Tracker entry). **A payment method is now active on the account**, so staging's ~12,291 rows
  and prod's ~12,291 rows (Section 3 below) should go through — but if billing ever lapses,
  every `ds:import` in this runbook will fail with `HTTP 400: exhausted free tier allowance`,
  not a data or script bug. Check the Catalyst console's billing page before Section 3 if
  there's any doubt.
- **`ds:import`'s CSV upload is create-only against Stratus** (the staging bucket it uses
  before writing rows) — re-uploading a file with a filename already used in that *specific
  project's* bucket fails with `409 key_already_exists`. This bit the `accused_persons`
  re-import into `aparadhkavach-dev` (needed a renamed file). It should **not** recur for
  staging/prod in Section 3 below, since those are brand-new projects with empty Stratus
  buckets — the original filenames will be new to them. Only rename a file if you get a 409.
- **Never pipe stdin into `catalyst ds:export --table <name> ...`** — on this CLI version,
  piped input gets consumed as the `--table` value itself, not the later download-report
  prompt, silently producing a 404 against a table literally named `n`. Run `ds:export`
  interactively.
- All `catalyst` commands below assume you're in the `aparadhkavach-data-generator` repo root
  with the correct project selected (`catalyst project:use <name>` or `-p <name>`, or having
  run `catalyst init` for that project — see Section 3).

---

## 1. ✅ Already done — officers Super Admin seed row

Historical record only, no action needed. Fixed and live-verified 16 Jul 2026
(`feature/backfill-offense-history`, PR #2, `aparadhkavach-data-generator`).

```
catalyst ds:import --table officers data/catalyst_datastore/officers_superadmin_seed.csv
```

`officers_superadmin_seed.csv` (single row, `OFF-SUPERADMIN-001`) was imported additively
against the existing 180 `aparadhkavach-dev` officer rows — no truncate needed, since it was a
net-new row, not a correction to existing ones.

## 2. ✅ Already done — accused_persons offense-history re-import

Historical record only, no action needed. Live-verified 16 Jul 2026: 4,460/4,460 rows, 0
failures, `prior_offense_count` non-zero (1 or 2) for ~15% of rows as expected.

The mechanism (confirmed via research, then actually executed): `catalyst ds:import` is
insert-only with silent duplicate-rejection on `accused_id`'s unique key — a straight
re-import would have rejected all 4,460 rows and left the stale zero-values in place with no
error surfaced. Actual sequence used:
1. Catalyst console → Data Store → `accused_persons` table → ellipsis (⋮) → **Truncate** →
   type `TRUNCATE` → **Confirm**
2. `catalyst ds:import --table accused_persons <renamed-copy-of-accused_persons.csv>`
   (renamed because the original filename had already been used against this project's
   Stratus bucket — see the gotcha in Section 0)
3. Verified via `catalyst ds:export --table accused_persons`, not just the import job's own
   report

---

## 3. IaC: export `aparadhkavach-dev`'s schema, import as staging, import as prod

**Why now, not earlier:** IaC export/import carries **schema only, never row data** — so this
step was never blocked by data correctness. It *was* blocked by schema correctness: if
`firs.created_by` were still mistyped as `DateTime` instead of `VARCHAR` (the original Day 4
bug) at export time, that wrong column type would replicate into both staging and prod,
requiring the same manual console fix twice more. That bug was fixed and verified 15 Jul 2026
(Defect Tracker: "firs table / created_by column type" — Resolved), so it's safe to export now.

```
# 1. Export aparadhkavach-dev's schema (run from aparadhkavach-dev's active project context)
catalyst iac:export
```
This produces a project-template ZIP (schema + Foreign Key definitions for all 5 tables, no
row data). Note the output path.

```
# 2. Import that ZIP as a brand-new project, once for staging...
catalyst iac:import <path-to-exported-zip> --name aparadhkavach-staging

# 3. ...and again as a separate brand-new project for prod
catalyst iac:import <path-to-exported-zip> --name aparadhkavach-prod
```

Both `iac:import` calls can also be done via console: **General Settings → Infrastructure as
Code → IaC Imports → Import New Project.**

**Note:** IaC import always lands the schema in that new project's internal *Development*
environment, not *Production* (a per-project Catalyst concept, distinct from our external
`-dev`/`-staging`/`-prod` project naming) — confirmed against Zoho's own docs
("You will not be able to import a project directly into the production environment"). This
doesn't block anything here; it matters once Pipelines/AppSail promotion (Section 6) is set up.

**Re-run `catalyst init`** from this repo root once each project exists, to link the CLI
session to it before running any `ds:import`/`ds:export` against it:
```
catalyst init   # select aparadhkavach-staging
# ...then later, when ready for prod:
catalyst init   # select aparadhkavach-prod
```

### 3a. Populate staging's and prod's DataStore

Two-pass population, same pattern as `aparadhkavach-dev`'s original build (Runbook Phase 2),
run once against staging, then again against prod:

```
1. catalyst ds:import --table districts data/catalyst_datastore/districts.csv
2. catalyst ds:export --table districts
   # build a district_id -> ROWID mapping from the export — ROWIDs differ per project
3. Regenerate firs.csv, accused_persons.csv, victims.csv, officers.csv so their district
   FK columns hold *this project's* ROWIDs, not aparadhkavach-dev's
4. catalyst ds:import --table firs data/catalyst_datastore/firs.csv
5. catalyst ds:import --table accused_persons data/catalyst_datastore/accused_persons.csv
6. catalyst ds:import --table victims data/catalyst_datastore/victims.csv
7. catalyst ds:import --table officers data/catalyst_datastore/officers.csv
   (officers.csv now already includes the Super Admin row — 181 rows — Section 1 above)
8. Verify row counts: districts 31, firs ~3,720, accused_persons ~4,460, victims ~3,900,
   officers 181
```
`run_pipeline.py` automates steps 1–8 for a given environment's config, per Section 12.9 — use
it if available rather than running each command by hand.

---

## 4. Neo4j AuraDB — two separate accounts (staging, prod)

**Confirmed: two separate accounts with different emails, not one account for both** —
genuine data isolation, and so Track 6's deliberate failure-injection testing on staging can't
touch what judges see in prod.

Repeat this twice, once per account/email. **Keep clear notes on which account owns which
environment** — two same-looking Aura consoles are easy to mix up.

1. Go to the Neo4j Aura console, sign up (email/password or Google SSO) — no credit card
   required for the Free tier
2. Click **Create Instance** → select **AuraDB Free** → choose a region → **Create**
3. Aura generates a password **shown exactly once** — copy or download it immediately, it
   cannot be recovered later
4. From the instance page, note:
   - Connection URI (`neo4j+s://xxxx.databases.neo4j.io`) → `NEO4J_URI`
   - Username (`neo4j` by default) → `NEO4J_USERNAME`
   - The generated password → `NEO4J_PASSWORD`

### 4a. Populate + validate each Neo4j instance

Per Section 12.9's repeatable steps (run once for staging's instance, once for prod's,
pointing at each one's own URI/credentials):
```
1. python neo4j_populate.py   # bulk load from data/entities/*.json + data/relationships/*.csv
2. Run Section 4.7's Cypher validation queries (repeat-offender count, cross-district count,
   isolated-nodes check, hotspot-location check, crime-type coverage)
```

---

## 5. Supabase — one account, two projects (staging, prod)

**Confirmed: one Supabase account/org covers both** — the Free plan allows 2 active
projects/account, which exactly matches this need (unlike Neo4j Aura, no second email needed).

1. Go to `supabase.com`, sign up, create an organization (Personal, Free plan)
2. Click **New Project** → name it `aparadhkavach-staging` → set a strong database password
   (a Postgres password, separate from your Supabase login) → choose a region → wait for
   provisioning
3. Repeat once more, same account, second project named `aparadhkavach-prod`
4. **For each project individually:** go to **Database → Extensions** in the sidebar → search
   "vector" → click to enable the `vector` extension. **This is not on by default and doesn't
   fail loudly if skipped** — Spring AI's `PgVectorStore` will simply fail at first use against
   a project where this step was missed, not at provisioning time.
5. **For each project individually:** get the connection string from **Project Settings →
   Database** (the "Connect" button shows per-client connection strings) — this becomes
   `PGVECTOR_JDBC_URL` (a standard JDBC string, e.g. `jdbc:postgresql://host:5432/postgres`),
   with `PGVECTOR_USERNAME`/`PGVECTOR_PASSWORD` alongside it. **Not** `PGVECTOR_URL`/
   `PGVECTOR_API_KEY` — that was an older, since-resolved naming (13 Jul 2026) in the
   Provisioning Guide; Section 12.5 is the current, correct source and only defines the
   JDBC-based variables.
6. Free-tier projects auto-pause after 7 days of inactivity — not a blocker, worth knowing
   before a demo if either project has been idle.

### 5a. Populate + validate each PgVector instance

Per Section 12.9, once per project, pointed at that project's own connection string:
```
1. python embedding_ingestion.py   # local/manual script, not a Catalyst Circuits job
   # Voyage AI rate limit note (confirmed empirically): an account with no payment method is
   # throttled to ~2.5 req/min safe pace, not the token allotment's implied rate — ingesting
   # ~3,720 FIRs takes ~75 minutes per environment at that pace, not ~10 minutes
2. python semantic_validation.py   # Section 4.7 Level 3 similarity spot checks
```

---

## 6. Connect Catalyst Pipelines — staging and prod projects only, not dev

**Confirmed from the Provisioning Guide:** Pipelines connects within the staging and prod
projects specifically — this step is not needed for (and was never done for) `aparadhkavach-dev`.

Per Zoho's own docs (confirmed, not guessed):
1. In each project's console: **Catalyst DevOps → Repositories → Git → Integrate GitHub**
2. Click **Agree** on the terms of use/privacy policy
3. Sign in with your GitHub username/email and password
4. On the **Authorize Zoho Catalyst** popup, click **Authorize ZohoCorporation**
5. You'll be redirected back to the Catalyst console once authorized

Then, per Zoho's docs, create a pipeline from the **Catalyst Pipelines** service, choosing
GitHub as the Git provider and selecting the repo/branch to connect —
**the exact button-by-button steps for pipeline creation itself weren't available in what I
could confirm from Zoho's docs** (the Quick Start Guide references "a separate help page" for
this without inlining it). Check the console directly for this specific part rather than
trusting a guess here.

Repeat for all **three deployed repos** (`aparadhkavach-services`, `aparadhkavach-client`,
`aparadhkavach-stt-service`) — `aparadhkavach-data-generator` has no pipeline (Section 12.3).
Each connects to `stage`/`main` branches per Section 12.2's branch model.

**Branch protection note (Section 12.2):** GitHub Rulesets (branch-name enforcement, blocking
direct pushes to `stage`/`main`) require GitHub Pro/Team/Enterprise Cloud for a *private* repo
— confirm the repos' plan/visibility before relying on Rulesets as the sole enforcement; the
CI-level branch-name-gate fallback (Section 12.3's pipeline gates table) works on any plan
regardless.

---

## 7. Populate the 27 environment variables — staging first, then prod

Per Section 12.5 (the current, correct source — 27 variables total). Set these as Catalyst
AppSail environment variables per project (staging project gets its own full set, prod gets
its own full set — values differ per environment where noted).

| Variable | Sensitivity | Staging value/source | Prod value/source |
|---|---|---|---|
| `APP_ENV` | Low | `staging` | `production` |
| `LOG_LEVEL` | Low | `DEBUG` | `INFO` |
| `SERVICE_NAME` | Low | per-service, e.g. `aparadhkavach-orchestration-service` (same literal both environments — this identifies the *service*, not the environment) | same as staging |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Low | Catalyst APM's OTLP endpoint for the staging project — get from that project's APM console | same, for the prod project |
| `JWT_PRIVATE_KEY` | **Critical** | generate a fresh signing key for staging (Auth Service) — do not reuse dev's | generate a separate fresh key for prod |
| `JWT_PUBLIC_KEY` | Medium | paired with staging's private key | paired with prod's private key |
| `JWT_EXPIRY_HOURS` | Low | `1` (Section 12.5 default) | `1` |
| `NEO4J_URI` | Medium | Section 4's staging Aura instance URI | prod Aura instance URI |
| `NEO4J_USERNAME` | Medium | `neo4j` (staging instance) | `neo4j` (prod instance) |
| `NEO4J_PASSWORD` | **Secret** | staging Aura's generated password | prod Aura's generated password |
| `PGVECTOR_JDBC_URL` | Medium | staging Supabase project's JDBC connection string | prod Supabase project's JDBC connection string |
| `PGVECTOR_USERNAME` | Medium | staging Supabase project's DB username | prod Supabase project's DB username |
| `PGVECTOR_PASSWORD` | **Secret** | staging Supabase project's DB password | prod Supabase project's DB password |
| `ANTHROPIC_API_KEY` | **Secret** | a separate key recommended per environment (Provisioning Guide) — create a staging key in the Anthropic console | separate prod key |
| `CLAUDE_MODEL` | Low | `claude-sonnet-4-6` | `claude-sonnet-4-6` |
| `VOYAGE_API_KEY` | **Secret** | Voyage AI key (can reuse the same key both environments unless separate tracking is wanted) | same or separate, your call |
| `EMBEDDING_MODEL` | Low | `voyage-3-large` | `voyage-3-large` |
| `EMBEDDING_DIMENSIONS` | Low | `1024` | `1024` |
| `SARVAM_API_KEY` | **Secret** | trial key (Section 12.1: staging uses trial key) | production key (Section 12.1: prod uses prod key — confirm this exists/is provisioned) |
| `QUICKML_ENDPOINT` | Medium | staging project's QuickML endpoint | prod project's QuickML endpoint |
| `QUICKML_API_KEY` | **Secret** | staging QuickML credential | prod QuickML credential |
| `VECTOR_TOP_K` | Low | `5` | `5` |
| `GRAPH_TRAVERSAL_DEPTH` | Low | `3` | `3` |
| `STT_CONFIDENCE_THRESHOLD_HIGH` | Low | `0.85` | `0.85` |
| `STT_CONFIDENCE_THRESHOLD_LOW` | Low | `0.70` | `0.70` |
| `CIRCUIT_BREAKER_THRESHOLD` | Low | `3` | `3` |
| `HOTSPOT_ALERT_THRESHOLD` | Low | `0.70` | `0.70` |

Where this table says "confirm"/"your call" rather than a fixed value, that's because Section
12.5/the Provisioning Guide doesn't pin an exact value for that field — don't guess one, look
it up in the relevant console at the time.

**Secret rotation reminder (Section 12.5):** if any of the above is ever regenerated later,
the procedure is: generate new credential → update the Catalyst AppSail env var → trigger a
redeploy → verify `/health` passes → revoke the old credential. Zero code changes, zero repo
commits, in every case.

---

## 8. Run `feature_builder.py` against real data, then trigger Day 7 QuickML

**Why after Sections 3–7, not before:** `feature_builder.py` (Prompt 1, `aparadhkavach-data-generator`,
already merged) was deliberately built and unit-tested against a small local fixture, since the
real committed `accused_persons.csv` had zero usable repeat-offender data at the time — that's
now fixed (Section 2), but the QuickML pipelines still need real Neo4j (`co_accused_count` via
`ASSOCIATED_WITH`) and DataStore data to run against, which only exists once Sections 3–5 are
done for whichever environment you're running this against.

```
1. Run feature_builder.py against the target environment's live firs/accused_persons data
   (Neo4j for co_accused_count, DataStore for the rest) to build accused_features /
   hotspot_features
2. Trigger the repeat offender risk scorer pipeline via the QuickML Pipeline module
3. Wait ~5 minutes; verify risk_scores populated (~4,460 records) and Neo4j
   Accused.risk_score properties updated
4. Trigger the hotspot forecaster pipeline
5. Wait ~10 minutes; verify hotspot_forecasts populated
```
(Section 12.9, Task 2 — same sequence Anand already knows from the original Day 7 plan; listed
here only so this runbook covers every remaining step end-to-end, not because anything about
it changed.)

---

## Sources

- Catalyst DataStore — Table Creation & Population Runbook (Notion, corrected 16 Jul 2026)
- External Services — Provisioning & Account Setup Guide (Notion)
- Section 12.1 (Environment Strategy), 12.2 (Branch Strategy), 12.3 (CI/CD Pipeline Design),
  12.5 (Environment Variable Inventory), 12.9 (Data Pipeline Deployment) — Notion
- Live Defect Tracker database (queried fresh, not cached) — officers Super Admin, `firs`/
  `accused_persons` re-import mechanics, DataStore billing quota, Runbook staleness entries
- `catalyst --help` / `catalyst ds:import --help` / `catalyst iac:import --help` (this
  machine's installed CLI, v1.26.2)
- Zoho Catalyst public docs: DataStore table truncate, GitHub integration steps (both fetched
  and quoted directly, not guessed)
