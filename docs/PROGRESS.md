# Enigma Core — Progress Checkpoint

Last checkpoint: **2026-08-25**

## Current validated architecture

Historical/data layer:

- Historical Controller v2
- Quality Batch v2
- Feature Profile v1
- Repair Incomplete v1
- Upstream Exception v1
- database-state checkpoint/resume strategy
- leakage-safe training/inference pipeline already promoted into operational J1 use

Operational/prediction layer:

- Daily Operations ingestion
- J1 timing and pending selector hardening
- STANDARD 36-feature inference runtime with dataset + fit cache
- Decision Engine V2
- immutable DecisionRecord/forward-test ledger
- Closing/CLV V1
- Forward-Test Report V2/V3
- Performance Observatory V1
- J1 capacity rollout 5 -> 10 -> 20
- PostgreSQL per-fixture work claiming
- three-instance Render worker Blueprint
- fail-safe batch/producer execution gate

## Canonical J1 entrypoints

The operational command contract is centralized in `docs/j1-operations-v1.md`.

- Web: `uvicorn app.main_v017:app --host 0.0.0.0 --port $PORT`
- Cron: `python -m app.j1_scheduler`
- Worker: `python -m app.j1_claim_worker`
- Producer implementation: `app.j1_work_producer`, delegated by the cron when `J1_EXECUTION_MODE=producer`

`app.j1_scheduler_v2` is no longer part of the architecture. Keeping one cron entrypoint prevents Render command drift.

## J1 Horizontal V1

Implemented:

- `j1_work_items` PostgreSQL queue;
- unique fixture + snapshot-window work identity;
- `PENDING / CLAIMED / RETRY / COMPLETED / FAILED / EXPIRED` lifecycle;
- `FOR UPDATE SKIP LOCKED` claiming;
- leases + heartbeat + UUID claim token;
- stale-worker completion protection;
- bounded retry before kickoff;
- canonical Prediction/Decision/Ledger logic reused inside the worker;
- process-local persistent inference runtime per worker;
- desired Render topology of three Starter worker instances;
- secret references from `enigma-core-api` rather than copied secret values;
- read-only queue status/recent endpoints.

The production cron remains deliberately in `J1_EXECUTION_MODE=batch` until the actual Render background-worker service exists and three healthy instances are observed. Only then should the cron be changed to `producer`.

## Capacity and performance

- Current production J1 cap: `J1_MAX_FIXTURES=20`.
- Hard ceiling: 20.
- J1 upstream concurrency: 4.
- Synthetic CI matrix validates 5, 10 and 20 fixtures.
- Sportmonks transport pooling, batched ledger lookups and inference fit caching remain active.

No real dense 20-fixture horizontal production cycle has yet been used as closure evidence because the worker fleet still needs to be materialized in Render.

## Forward Test / CLV

Forward-Test V3 includes:

- multiclass Brier and Log Loss;
- accuracy and average probability assigned to the actual result;
- uniform and climatology baselines and skill;
- favorite-decision reliability;
- predicted-class and classwise 1/X/2 calibration;
- fixed 10pp bins, ECE/MCE and binary Brier;
- CLV coverage, missing/finalized counts and distributions;
- readiness thresholds for diagnostic interpretation.

Closing/CLV remains isolated so a temporary closing-data failure cannot invalidate a valid J1 Prediction/Decision/Ledger cycle.

Historical `closing_not_ready` records must never be fabricated or backfilled from post-kickoff/current odds.

## Settlement

Settlement V1 exists as an authenticated GitHub Actions workflow, but operational closure is still pending. It should be integrated/validated in the primary operations architecture after the J1 horizontal cutover is proven in production.

## CI and operational contract

J1 CI now has two responsibilities:

1. regression safety — compile, migration-chain validation and automatic discovery of all `tests/test_*.py` tests;
2. architecture safety — `scripts/validate_j1_operational_contract.py` verifies Render service commands, worker count, secret refs, fail-safe mode, retired entrypoints and canonical documentation.

The CI workflow is triggered by changes under application code, tests, migrations, scripts, docs, Render configuration and relevant GitHub workflows so new tests and operational drift cannot silently bypass the gate.

## Current next milestones

1. Materialize `enigma-j1-worker` in Render and confirm three healthy instances.
2. Change only `J1_EXECUTION_MODE` from `batch` to `producer`.
3. Validate the first real `producer -> enqueue -> claim -> completed` due-fixture cycle and immutable Prediction/Decision/Ledger persistence.
4. Observe a meaningful multi-fixture horizontal cycle, ideally approaching the configured 20-fixture ceiling.
5. Close Settlement V1 operationally and integrate it with the primary operations lifecycle without duplicating business logic.

## Operational stack

- API/backend: FastAPI/Python repository `fleonni-27/enigma-core-api`
- Source control: GitHub, default branch `main`
- Deployment: Render web + cron; background-worker Blueprint prepared
- Database: Supabase/PostgreSQL
- Football data source: Sportmonks
- Production API wrapper: `app.main_v017`, version `0.49.0`

This document is the current engineering checkpoint. Operational command details belong in `docs/j1-operations-v1.md`; this file should summarize validated state and remaining closure work rather than duplicate runbooks.
