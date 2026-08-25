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
- three live Render worker instances
- production J1 cut over to `J1_EXECUTION_MODE=producer`
- Settlement V1 scheduled automation
- Daily Analysis V1 scheduled Render cron

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

Enigma Rating V2 does not silently replace this model. Poisson, Dixon-Coles, Elo, xG/xGA, form-10 and lineup impact remain research signals pending out-of-sample evaluation against the current baseline with Brier, Log Loss, calibration and CLV.

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

`GET /rating/context-v2/{sportmonks_fixture_id}` builds research context automatically from historical database state strictly before target kickoff. J1 lineup presence is observable but is not treated as strength without an auditable player-value model.

## J1 Horizontal V1

Canonical operational contract: `docs/j1-operations-v1.md`.

Implemented and operationally materialized:

- `j1_work_items` PostgreSQL queue;
- `PENDING / CLAIMED / RETRY / COMPLETED / FAILED / EXPIRED` lifecycle;
- `FOR UPDATE SKIP LOCKED` claiming;
- leases + heartbeat + UUID claim token;
- bounded retry before kickoff;
- canonical Prediction/Decision/Ledger execution inside the worker;
- one Render `enigma-j1-worker` service with three live Starter instances in Virginia;
- cron entrypoint `python -m app.j1_scheduler` preserved;
- production cron cut over from `batch` to `producer` after all three workers were verified healthy;
- queue backend PostgreSQL and Closing/CLV preserved after cutover.

The remaining horizontal closure evidence is the first real due J1 fixture traversing `enqueue -> PENDING -> CLAIMED -> COMPLETED -> Prediction -> Decision -> Ledger`. Infrastructure and cutover are complete; no synthetic fixture is used to fake this evidence.

## Capacity and performance

- Current production J1 cap: `J1_MAX_FIXTURES=20`.
- Hard ceiling: 20.
- J1 upstream concurrency: 4.
- Synthetic CI matrix validates 5, 10 and 20 fixtures.
- Sportmonks transport pooling, batched ledger lookups and inference fit caching remain active.

## Forward Test / CLV

Forward-Test V3 includes multiclass Brier, Log Loss, calibration, skill baselines, CLV coverage and diagnostic readiness. Closing/CLV remains isolated so a temporary closing-data failure cannot invalidate a valid J1 Prediction/Decision/Ledger cycle.

Historical `closing_not_ready` records must never be fabricated or backfilled from post-kickoff/current odds.

## Settlement V1

Operational closure completed on 2026-08-25.

- scheduled GitHub Actions workflow `forward-test-settlement-runner-v1.yml` remains active every 30 minutes at `:07` and `:37`;
- workflow calls authenticated `POST /research/forward-test/settle/pending?limit=25`;
- production API logs have confirmed automatic settlement-route execution with HTTP 200;
- settlement is idempotent, requires a finished fixture and never overwrites settled records;
- Fulltime Result / 1X2 settles on regulation time, excluding extra time and penalties;
- Daily Analysis also runs `settle_pending_records(limit=25)` before generating each daily report, providing a second daily settlement pass.

## Daily Analysis V1

Operational closure completed on 2026-08-25.

- Render cron: `enigma-daily-analysis-report`;
- command: `python -m app.daily_analysis_report`;
- region/plan: Virginia / Starter;
- final schedule: `30 4 * * *` = 01:30 America/Sao_Paulo;
- first scheduled validation run completed automatically at 2026-08-25 11:15 BRT;
- the run executed Settlement V1 first, generated Forward-Test Report V3 and persisted `daily_analysis_reports` row `report_id=1` for business date 2026-08-24;
- validation run reported 8 settled records and 0 unsettled records for the target date;
- temporary five-minute bootstrap cadence was removed immediately after validation.

## CI and operational contract

CI automatically discovers all `tests/test_*.py` modules and validates compile, migration chain and the J1 operational contract. Enigma Rating V2 probability, xG/xGA, lineup and route tests remain in the same regression gate.

## Current next milestones

1. Observe the first real J1 horizontal fixture cycle through `enqueue -> claim -> Prediction -> Decision -> Ledger`.
2. Establish an out-of-sample Rating V2 evaluation dataset and compute Brier/Log Loss/calibration for STANDARD, Poisson, Dixon-Coles and Elo.
3. Add ablation analysis for xG/xGA and exact form-10 before choosing ensemble/blend weights.
4. Persist a trustworthy injury/suspension availability feed and design a learned/auditable player contribution model before activating lineup absence scores automatically.
5. Fit Dixon-Coles rho and Elo hyperparameters only on training history with temporal validation.

## Operational stack

- API/backend: FastAPI/Python repository `fleonni-27/enigma-core-api`
- Source control: GitHub, default branch `main`
- Deployment: Render web + J1 producer cron + three-instance background worker + Daily Analysis cron
- Database: Supabase/PostgreSQL
- Football data source: Sportmonks
- Production API wrapper: `app.main_v017`, version `0.51.4`

This document is the current engineering checkpoint. Operational command details belong in `docs/j1-operations-v1.md`; Rating V2 model details belong in `docs/enigma-rating-v2.md`.
