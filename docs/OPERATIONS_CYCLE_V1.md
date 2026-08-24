# Operations Cycle V1

## Purpose

Provide a safe server-side research operations cycle without calling public mutation endpoints.

The cycle executes, in order:

1. pending forward-test settlement;
2. future batch ingestion, inference, odds evaluation, decision evaluation and immutable ledger persistence.

The implementation remains `RESEARCH_ONLY`. It does not enable stake sizing, bet placement or real-money execution.

## Internal runner

Run directly inside a Render cron job or shell:

```bash
python -m app.operations_cycle
```

The runner calls Python functions directly. It does not call the public API over HTTP.

Default parameters:

- settlement limit: 10 fixtures;
- future horizon: 3 days;
- future batch size: 3 fixtures;
- minimum lead: 60 minutes;
- `skip_existing_fixtures=True` is always enforced by the cycle.

Optional environment overrides:

- `ENIGMA_CYCLE_SETTLEMENT_LIMIT` (1..25)
- `ENIGMA_CYCLE_DAYS_AHEAD` (0..7)
- `ENIGMA_CYCLE_MAX_FIXTURES` (1..5)
- `ENIGMA_CYCLE_MIN_LEAD_MINUTES` (0..1440)

The process prints one JSON result and exits non-zero when the cycle is partial or failed so Render can surface operational failures.

## HTTP mutation protection

The following production POST operations are protected by `X-Enigma-Operations-Token`:

- `/research/future-batch/run`
- `/research/forward-test/settle/pending`
- `/research/forward-test/settle/fixture/{sportmonks_fixture_id}`

The expected secret is read from `ENIGMA_OPERATIONS_TOKEN`.

Production is fail-closed: when `APP_ENV=production` and the token is missing, protected routes return 503 with `OPERATIONS_TOKEN_NOT_CONFIGURED`. Invalid or missing supplied tokens return 401.

The internal cycle does not require this token because it does not cross the HTTP boundary.

## Deployment sequencing

Do not merge the security middleware before `ENIGMA_OPERATIONS_TOKEN` exists on the Render web service if manual Swagger use of the protected routes must remain available.

Recommended sequence:

1. create `ENIGMA_OPERATIONS_TOKEN` as a secret environment variable on the web service;
2. merge and deploy the operations security branch;
3. validate that an unauthenticated protected POST returns 401;
4. validate the same POST with the correct header succeeds;
5. configure a Render cron service to run `python -m app.operations_cycle` on the agreed UTC schedule.

Render cron should receive the same `DATABASE_URL`, `SPORTMONKS_API_TOKEN`, `SPORTMONKS_BASE_URL` and `APP_ENV` values required by the application code. The operations HTTP token is not required by the cron runner itself.

## Guardrails

- Research only.
- No real-money execution.
- Settlement remains idempotent and never overwrites settled records.
- Automated future batch uses one fixture per forward-test sample by default.
- Forward-test records remain immutable.
- Cron should execute the internal runner, not the public HTTP endpoints.
