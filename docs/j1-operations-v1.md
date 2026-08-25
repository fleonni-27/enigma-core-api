# J1 Operations V1

This document is the canonical operational contract for the Enigma Core J1 pipeline. It describes the production entrypoints, Render topology, cutover state and rollback rules. Other J1 documents may explain model or data semantics, but operational command ownership lives here.

## Canonical production entrypoints

| Role | Command | Responsibility |
| --- | --- | --- |
| Web API | `uvicorn app.main_v017:app --host 0.0.0.0 --port $PORT` | HTTP API, read-only J1 queue observability and operational routes |
| J1 cron | `python -m app.j1_scheduler` | Single Render cron entrypoint; routes execution by `J1_EXECUTION_MODE` |
| J1 worker | `python -m app.j1_claim_worker` | Long-running per-fixture claim consumer |

`app.j1_work_producer` is the canonical producer implementation, but it is invoked through `app.j1_scheduler` when `J1_EXECUTION_MODE=producer`. Render should not need a second cron command for producer mode.

`app.j1_scheduler_v2` is retired. Reintroducing a second scheduler entrypoint would recreate command drift and is blocked by CI.

## Execution modes

### Safe compatibility mode

`J1_EXECUTION_MODE=batch`

This is the fail-safe default. The Render cron executes the existing J1 Prediction -> Decision -> Ledger cycle directly and then runs Closing/CLV. Use this mode whenever the background-worker fleet is absent, unhealthy or being provisioned.

### Horizontal producer mode

`J1_EXECUTION_MODE=producer`

The same Render cron entrypoint delegates to `app.j1_work_producer`. The producer:

1. acquires the same J1 advisory lock used by batch mode;
2. discovers due fixtures under the current J1 timing/capacity policy;
3. expires past-kickoff work;
4. inserts idempotent `(fixture_id, snapshot_window)` work rows;
5. runs Closing/CLV separately;
6. does not execute Prediction/Decision/Ledger itself.

Workers claim queued fixtures with PostgreSQL `FOR UPDATE SKIP LOCKED`, leases, claim tokens and bounded retries.

## Render topology

Desired J1 Horizontal V1 topology:

- one `enigma-core-api` web service in Virginia;
- one `enigma-j1-runner` cron service every minute;
- one `enigma-j1-worker` background-worker service with **3 instances**;
- all services on branch `main`;
- worker command `python -m app.j1_claim_worker`;
- cron command `python -m app.j1_scheduler`;
- `DATABASE_URL` and `SPORTMONKS_API_TOKEN` for cron/worker referenced from `enigma-core-api` through Render `fromService.envVarKey` wiring;
- worker shutdown drain at least 180 seconds; Blueprint currently declares 300 seconds.

Horizontal worker scaling does not partition fixtures manually. Each instance competes safely for the next eligible queue row.

### Worker-only bootstrap Blueprint

`render.worker.yaml` is the provisioning-only Blueprint for the currently missing worker service. It intentionally contains **only** `enigma-j1-worker`, so provisioning the worker cannot adopt, duplicate or overwrite the already-live web API or J1 cron configuration.

The bootstrap worker must remain identical to the canonical worker declaration in `render.yaml` for runtime, region, branch, plan, instance count, drain time, build/start commands and environment-variable wiring. CI validates this equality.

Provision it in Render using a new Blueprint linked to the existing repository/`main` branch and set the Blueprint Path to `render.worker.yaml`. `DATABASE_URL` and `SPORTMONKS_API_TOKEN` resolve from the existing `enigma-core-api` service through `fromService.envVarKey`; no secret value is copied into GitHub.

After provisioning, the bootstrap Blueprint owns only `enigma-j1-worker`. Do not attach the same worker to a second Blueprint.

## Capacity and timing

- J1 target: kickoff minus 45 minutes.
- Maximum lateness: 20 minutes after J1 becomes due.
- Operational capacity stages: 5 -> 10 -> 20.
- Production cap: `J1_MAX_FIXTURES=20`.
- J1 upstream request concurrency inside the canonical runner remains bounded to 4.

Changing worker count does not change model probabilities, Decision Engine thresholds, J1 timing, immutable Prediction semantics or forward-test ledger semantics.

## Worker contract

The worker is deliberately single-claim-at-a-time per process. A claimed fixture is executed through the canonical Daily Prediction Runner logic rather than a second Prediction/Decision/Ledger implementation.

Claim safety:

- queue uniqueness on fixture + snapshot window;
- lease expiry allows safe reclamation;
- a UUID claim token prevents a stale worker from completing a reclaimed job;
- lease heartbeat continues while synchronous model fitting blocks the event loop;
- retries are bounded and are never scheduled beyond kickoff;
- immutable Prediction/Ledger constraints remain the final duplicate-write defense.

## Cutover procedure

The cron must remain in `batch` until a real Render worker service exists and all three instances are healthy.

1. Provision `enigma-j1-worker` using the worker-only `render.worker.yaml` Blueprint.
2. Confirm the service runs `python -m app.j1_claim_worker`.
3. Confirm three distinct healthy Render worker instances and successful database polling.
4. Confirm there is no crash loop or missing-secret/database error.
5. Change only the cron environment variable to `J1_EXECUTION_MODE=producer`.
6. Confirm the next cron payload reports producer mode.
7. On the first due fixture, confirm `PENDING -> CLAIMED -> COMPLETED` and exactly one immutable Prediction/Decision/Ledger result.
8. Confirm Closing/CLV still runs from the producer cron.

Only after these checks is J1 Horizontal V1 operationally closed.

## Rollback

Rollback does not require changing commands or code:

1. set `J1_EXECUTION_MODE=batch` on `enigma-j1-runner`;
2. confirm the next cron cycle reports batch mode;
3. investigate or scale down workers only after the cron is back in batch mode.

Do not leave producer mode active with zero healthy consumers.

## Observability

Read-only queue endpoints:

- `GET /operations/j1-work/status`
- `GET /operations/j1-work/recent?limit=50`

The recent-work API must never expose claim tokens.

Operational evidence should also include Render cron/worker logs, distinct worker instance IDs and the immutable DecisionRecord/Ledger rows for a due fixture.

## GitHub Actions fallback

`.github/workflows/daily-prediction-runner-v1.yml` remains an HTTP fallback, not the primary scheduler. It runs every five minutes during the configured match window and uses the authenticated operational route. The Render cron remains the primary once-per-minute scheduler.

The fallback currently requests a conservative maximum of five fixtures. It does not define the production `J1_MAX_FIXTURES=20` capacity of the Render path.

## CI contract

`.github/workflows/j1-hardening-checks.yml` must:

- compile application, migrations and scripts;
- validate the Alembic chain;
- run `python scripts/validate_j1_operational_contract.py`;
- run test discovery across every `tests/test_*.py` module;
- trigger when application, test, migration, script, Render, workflow or documentation contracts change.

The validator fails CI if entrypoints, Render topology, secret references, worker count, fail-safe mode, worker bootstrap Blueprint or canonical documentation drift apart.

## Current operational state — 2026-08-25

Code, migration, work queue, claim worker, three-instance canonical Blueprint declaration, worker-only bootstrap Blueprint, secret-reference wiring and the batch/producer gate are implemented. The Render cron is explicitly kept in `J1_EXECUTION_MODE=batch` until the real `enigma-j1-worker` service has been provisioned and three healthy instances are observed.
