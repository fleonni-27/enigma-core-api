# Performance & Scale V1

## Goal

Reduce network latency, repeated database queries and connection setup in the Enigma Core operational hot paths without changing model probabilities, J1 timing, Decision Engine thresholds or forward-test semantics.

## Sportmonks transport pooling

`SportmonksClient` now supports cycle-local connection pooling through `async with SportmonksClient()`.

Managed cycles share one `httpx.AsyncClient` with:

- max connections: 12;
- max keep-alive connections: 6;
- keep-alive expiry: 30 seconds;
- per-request timeouts preserved by endpoint.

Legacy callers remain compatible. If the client is not used as a context manager, the public method owns and closes a temporary HTTP session.

The client exposes a transport audit containing request count, pooled request count and temporary-session count.

## J1 upstream prefetch

The J1 runner no longer performs lineup request, odds request, inference and decision fully serially per fixture.

For fixtures not already recorded in the forward-test ledger:

1. a single batched ledger lookup identifies already-completed fixture/window pairs;
2. lineup/enriched-fixture and prematch-odds requests are prefetched concurrently;
3. request concurrency is bounded to 4;
4. database persistence, inference, decision and ledger writes remain sequential and deterministic.

A failure in one upstream request does not cancel other fixture requests.

The runner response now exposes `performance` with total cycle time, batched ledger lookup, upstream prefetch timing/concurrency and Sportmonks transport metrics.

## Daily Operations scaling

Daily odds refresh now:

- uses one pooled Sportmonks transport for the cycle;
- fetches odds concurrently with a limit of 5 requests;
- persists fetched odds sequentially after network completion;
- isolates individual upstream failures.

`GET /operations/today` removes the former N+1 query pattern. Odds count/latest timestamp and prediction count are now resolved with two grouped aggregate queries for all target fixtures instead of three queries per fixture.

## Guardrails

This change does not alter:

- `baseline_1x2_temporal_v1`;
- STANDARD 36 features;
- Inference Runtime V2 dataset/fit cache semantics;
- the exact V1 `training_sha256` rule;
- J1 = kickoff - 45 minutes;
- Decision Engine V2 gates and thresholds;
- immutable Prediction or DecisionRecord behavior;
- lineup exclusion from current STANDARD features;
- research-only / no automatic real-money execution.

## Current scale boundaries

Performance & Scale V1 deliberately keeps the existing J1 maximum fixtures per run unchanged. The new telemetry should be observed in production before increasing that safety limit.

API version: `0.37.0`.
