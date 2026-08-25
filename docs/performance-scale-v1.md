# Performance & Scale V1

## Goal

Reduce network latency, repeated database queries and connection setup in the Enigma Core operational hot paths without changing model probabilities, J1 timing, Decision Engine thresholds or forward-test semantics.

## Sportmonks transport pooling

`SportmonksClient` supports cycle-local connection pooling through `async with SportmonksClient()`.

Managed cycles share one `httpx.AsyncClient` with bounded connections and preserved per-request timeouts. Legacy callers remain compatible, while transport telemetry reports request count and pooling behavior.

## J1 upstream prefetch

For fixtures not already recorded in the forward-test ledger:

1. one batched ledger lookup identifies already-completed fixture/window pairs;
2. enriched-fixture/lineup and prematch-odds requests are prefetched concurrently;
3. request concurrency is bounded to 4;
4. database persistence, inference, decision and ledger writes remain deterministic;
5. one upstream failure does not cancel unrelated fixture fetches.

The runner exposes `performance` telemetry with cycle time, batched ledger lookup, prefetch timing/concurrency and Sportmonks transport metrics.

## Capacity rollout

The former hard limit of five J1 fixtures was replaced by explicit rollout stages **5 -> 10 -> 20**. Production is configured with `J1_MAX_FIXTURES=20`, while the hard ceiling remains 20.

Synthetic CI load tests cover 5, 10 and 20 fixtures and verify that upstream concurrency never exceeds four requests in flight.

## Horizontal J1 execution

J1 Horizontal V1 adds a PostgreSQL work queue and **three horizontal claim workers** without changing the canonical prediction/decision logic.

- producer writes one idempotent queue row per fixture + snapshot window;
- workers claim with `FOR UPDATE SKIP LOCKED`;
- leases and claim tokens prevent stale completion;
- retries are bounded and stop before kickoff;
- each worker processes one claim at a time and reuses its own inference runtime across jobs;
- immutable Prediction/DecisionRecord constraints remain the final duplicate-write defense.

The canonical production commands and cutover procedure are in `docs/j1-operations-v1.md`.

The Render cron remains `python -m app.j1_scheduler`. `J1_EXECUTION_MODE=batch` is the safe compatibility mode; `producer` activates queue-only discovery after real workers are healthy.

## Daily Operations scaling

Daily odds refresh:

- uses one pooled Sportmonks transport for the cycle;
- fetches odds concurrently with a limit of 5 requests;
- persists fetched odds sequentially after network completion;
- isolates individual upstream failures.

`GET /operations/today` avoids the former N+1 query pattern by resolving odds and prediction aggregates in grouped queries.

## Guardrails

Performance and scale changes do not alter:

- `baseline_1x2_temporal_v1`;
- STANDARD 36 features;
- Inference Runtime V2 dataset/fit-cache semantics;
- the exact V1 `training_sha256` rule;
- J1 = kickoff - 45 minutes;
- Decision Engine V2 gates and thresholds;
- immutable Prediction or DecisionRecord behavior;
- lineup exclusion from current STANDARD features;
- research-only / no automatic real-money execution.

## Current scale boundary

The current declared production ceiling is **20 J1 fixtures per scheduler discovery cycle** with three background worker instances in the desired Render topology. Horizontal V1 code and Blueprint are implemented, but the operational cutover remains incomplete until the real `enigma-j1-worker` service is provisioned, all three instances are healthy, and the cron is switched from batch to producer mode.

API version: `0.49.0`.
