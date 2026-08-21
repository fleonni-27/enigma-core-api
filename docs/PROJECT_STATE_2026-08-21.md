# Enigma Core — Project State

Snapshot date: 2026-08-21

This document is the operational source of truth for the current Enigma Core backend and historical-data pipeline.

## 1. Product identity

- Project: Enigma Core
- Domain requested: `enigmacore.com.br`
- Instagram: `@core_enigma`
- X: `coreenigma_`
- YouTube: `Enigma Core` / `@core_enigma`

## 2. Platforms and infrastructure

- GitHub repository: `fleonni-27/enigma-core-api`
- Production hosting: Render
- Production API: `https://enigma-core-api.onrender.com`
- API docs: `https://enigma-core-api.onrender.com/docs`
- Database: PostgreSQL / Supabase
- Football-data provider: Sportmonks
- Backend: FastAPI + SQLAlchemy + Psycopg + HTTPX
- Deployment flow: GitHub `main` -> Render Auto-Deploy

## 3. Current API version

Current backend version: **0.12.0**

Main validated endpoints:

- `GET /health`
- `GET /registry/leagues`
- `GET /audit/fixture/{sportmonks_fixture_id}`
- `GET /quality/fixture/{sportmonks_fixture_id}`
- `GET /quality/batch`
- `GET /quality/features`
- `GET /quality/features/{sportmonks_fixture_id}`
- `GET /coverage/data`
- `GET /coverage/fixtures`
- `GET /fixtures/today`
- `GET /fixtures/date/{target_date}`
- `POST /ingest/fixtures/date/{target_date}`
- `POST /ingest/data/fixture/{sportmonks_fixture_id}`
- `POST /ingest/odds/fixture/{sportmonks_fixture_id}`
- `POST /backfill/fixtures`
- `POST /backfill/data`
- `POST /backfill/monthly`

## 4. Historical-data architecture

Current validated flow:

`Sportmonks -> FastAPI -> PostgreSQL/Supabase -> RAW FixtureDataSnapshot -> Audit -> Data Quality Gate -> Quality Batch v2 -> Feature Profile v1 -> training eligibility`

RAW snapshots are preserved. Quality and feature classification are computed above the raw layer instead of mutating provider data.

## 5. Validated components

### Monthly controlled backfill

Implemented and tested with:

- automatic month splitting;
- target-league filters;
- request/data limits;
- `skip_existing` support;
- controlled fixture enrichment;
- lineups, statistics and xG ingestion where available.

### Audit endpoint

`GET /audit/fixture/{sportmonks_fixture_id}` exposes the latest stored snapshot and audits lineups, statistics, xG, nulls, duplicates and structural content.

### Data Quality Gate v1

Per-fixture quality output includes:

- `quality_score`;
- `decision`;
- `approved_for_training`;
- blockers;
- warnings;
- team/participant consistency;
- critical-stat coverage;
- xG availability and internal consistency;
- duplicate/null checks.

Important policy: **missing xG is not xG = 0**.

### Quality Batch v2

Separates ingestion coverage from actual snapshot quality.

Core metrics:

- fixtures in scope;
- enriched fixtures;
- missing snapshots;
- coverage rate;
- training eligibility rate among snapshots;
- clean approval rate;
- warning rate;
- rejection rate after snapshot;
- snapshot quality-score distribution;
- lineups/statistics/xG coverage among snapshots.

### Feature Profile v1

Profiles are fixture-based, not permanently league-based:

- `FULL_XG`: lineups + statistics + xG;
- `STANDARD_NO_XG`: lineups + statistics, xG unavailable;
- `INCOMPLETE`: snapshot exists but an essential structural layer is missing;
- `NO_SNAPSHOT`: fixture exists but has not yet been enriched.

Policy: xG absence remains missing and is never zero-imputed.

## 6. April 2026 validation results

### Serie A

- 30 enriched snapshots tested
- 30/30 training-eligible
- 0 structural rejections
- profile on enriched sample: `FULL_XG`

### La Liga

- 25 enriched snapshots tested
- 25/25 training-eligible
- 0 structural rejections
- profile on enriched sample: `FULL_XG`

### Premier League

- 25 enriched snapshots tested
- 25/25 training-eligible
- 0 structural rejections
- Quality Batch v2: 100% training eligibility, 92% clean approval, 8% warning, 0% snapshot rejection, 99.8 average snapshot quality score
- profile on enriched sample: `FULL_XG`

### Copa Libertadores

- 25 enriched snapshots tested
- 25/25 training-eligible
- 0 structural rejections
- Quality Batch v2: 100% training eligibility, 80% clean approval, 20% warning, 0% snapshot rejection, 99.6 average snapshot quality score
- profile on enriched sample: `FULL_XG`

### Serie B

- 25 enriched snapshots tested
- 25/25 training-eligible
- 0 structural rejections
- lineups coverage among snapshots: 100%
- statistics coverage among snapshots: 100%
- xG coverage among snapshots: 0%
- Quality Batch v2 average snapshot quality score: 94.3
- Feature Profile v1: 25/25 `STANDARD_NO_XG`

### Consolidated validation

Across the five tested competitions:

- **130 enriched snapshots tested**
- **130/130 training-eligible**
- **0 structural rejections after snapshot**

For the four xG-covered competitions (Serie A, La Liga, Premier League, Libertadores), the transverse Feature Profile test found:

- 166 fixtures in scope;
- 105 enriched;
- 61 `NO_SNAPSHOT`;
- 105/105 enriched snapshots = `FULL_XG`;
- 0 `INCOMPLETE`;
- 105/105 training-eligible.

Serie B was separately validated as `STANDARD_NO_XG` for all 25 enriched snapshots.

## 7. Important data rules

1. Never convert unavailable xG to zero.
2. `FULL_XG` means feature-layer presence, not perfect data quality.
3. Training eligibility comes from the Quality Gate, not Feature Profile alone.
4. Missing snapshots reduce ingestion coverage; they do not reduce the quality score of existing snapshots.
5. RAW provider data must remain preserved and auditable.
6. Backfills should use idempotent/skip-existing behavior where appropriate.
7. Warnings such as `critical_null_fields` and `critical_statistics_missing` must remain visible even when a fixture is still training-eligible.

## 8. Current league registry focus

Validated/target competitions include:

- Serie A
- Serie B
- Copa Libertadores
- La Liga
- Premier League

The registry also contains other target competitions for later expansion, including Copa do Brasil, Sudamericana and Champions League.

## 9. Current state of April 2026 coverage

The historical store is not yet complete for every fixture in April. Existing `NO_SNAPSHOT` fixtures are expected because controlled test backfills intentionally enriched only limited batches.

The structural validation phase is considered successful. The next problem is controlled scale, not proof of basic ingestion quality.

## 10. Next technical milestone

**Backfill Historical Controller v1**

Required capabilities:

- month-by-month execution;
- league-by-league execution;
- checkpoints/progress state;
- safe resume after interruption;
- idempotency;
- provider/request limits;
- enrich only missing work;
- execution/failure reporting;
- automatic Quality Gate after each batch;
- automatic Feature Profile after each batch;
- explicit completion/coverage status;
- pilot on one complete month across the five validated leagues before multi-season scaling.

## 11. Security and operations

- `.env` and secrets must not be committed.
- `DATABASE_URL` and `SPORTMONKS_API_TOKEN` belong in Render environment variables/secrets.
- GitHub `main` is the canonical code branch for production Auto-Deploy.
- Render is deployment runtime, not the canonical source of code.
- Supabase/PostgreSQL is the canonical data store for ingested fixtures/snapshots.
- Sportmonks is an external source provider, not a backup platform.

## 12. Source-of-truth rule

For continuity, do not rely on chat memory as the only record of the project. The repository, production configuration and database are the durable sources of truth.
