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

Research/modeling layer:

- Enigma Rating V1 transparent evidence score
- Enigma Rating V2 research signal stack
- independent Poisson 1X2
- Dixon-Coles low-score correction
- Elo + Davidson three-way probabilities
- leakage-safe historical xG/xGA context
- exact 10-match form context
- explicit lineup/absence-impact contract with no guessed player values

## Promoted model versus research signals

The promoted prediction model remains `baseline_1x2_temporal_v1`:

- multinomial logistic regression;
- STANDARD family;
- 36 model features;
- default 5-match target-history lookback;
- strict pre-target temporal cutoff;
- no target-match lineup contribution;
- no xG feature in the promoted STANDARD vector.

Enigma Rating V2 does not silently replace this model. Poisson, Dixon-Coles, Elo, xG/xGA, form-10 and lineup impact are exposed as research signals so they can be evaluated against the current baseline with Brier, Log Loss, calibration and CLV before promotion.

## Enigma Rating V2

Canonical documentation: `docs/enigma-rating-v2.md`.

V2 component budget:

- model confidence: 15;
- market edge: 15;
- Poisson: 12;
- Dixon-Coles: 12;
- Elo: 10;
- xG/xGA: 12;
- exact form-10: 10;
- lineup impact: 9;
- home advantage: 5.

Missing components are not imputed. Coverage is explicit and weights are renormalized only over observed/auditable evidence.

`GET /rating/context-v2/{sportmonks_fixture_id}` builds research context automatically from historical database state strictly before the target kickoff. It derives goals, xG, xGA, exact 10-match form and same-league Elo without changing the STANDARD feature contract.

J1 lineup snapshots are observable in the V2 context. Lineup presence is not treated as strength. Numeric absence impact is only accepted when an auditable player/expected-XI value model supplies retained-strength or absent-value inputs.

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

CI automatically discovers all `tests/test_*.py` modules and validates compile, migration chain and the J1 operational contract. Enigma Rating V2 probability, xG/xGA, lineup and route tests therefore enter the same regression gate automatically.

## Current next milestones

1. Establish an out-of-sample Rating V2 evaluation dataset and compute Brier/Log Loss/calibration for Poisson, Dixon-Coles and Elo individually versus `baseline_1x2_temporal_v1`.
2. Add ablation analysis for xG/xGA and exact form-10 before choosing any ensemble/blend weights.
3. Persist a trustworthy injury/suspension availability feed and design a learned/auditable player contribution model before activating lineup absence scores automatically.
4. Fit Dixon-Coles rho and Elo hyperparameters only on training history with temporal validation.
5. Materialize `enigma-j1-worker` in Render, confirm three healthy instances and complete the horizontal cutover.
6. Close Settlement V1 operationally without duplicating business logic.

## Operational stack

- API/backend: FastAPI/Python repository `fleonni-27/enigma-core-api`
- Source control: GitHub, default branch `main`
- Deployment: Render web + cron; background-worker Blueprint prepared
- Database: Supabase/PostgreSQL
- Football data source: Sportmonks
- Production API wrapper: `app.main_v017`, version `0.50.0`

This document is the current engineering checkpoint. Operational command details belong in `docs/j1-operations-v1.md`; Rating V2 model details belong in `docs/enigma-rating-v2.md`.
