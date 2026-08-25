# Daily Prediction Runner V1

The Daily Prediction Runner closes the operational gap between daily fixture/odds ingestion and the immutable forward-test ledger. Its prediction, decision and ledger semantics remain canonical even as J1 execution moves from one batch process to per-fixture claim workers.

Canonical operational commands and cutover rules are documented in `docs/j1-operations-v1.md`.

## J1 policy

- J1 target: **45 minutes before kickoff**.
- The runner never executes before kickoff - 45m.
- A fixture remains eligible for up to 20 minutes after J1 becomes due.
- The response records J1 timing fields so execution remains auditable.
- Production capacity is controlled independently by `J1_MAX_FIXTURES`; the current Render cap is 20.

## Scheduling

The **Render cron is the primary scheduler** and runs every minute through:

`python -m app.j1_scheduler`

The cron has two explicit modes:

- `J1_EXECUTION_MODE=batch`: fail-safe compatibility mode; the cron directly executes the canonical runner and Closing/CLV.
- `J1_EXECUTION_MODE=producer`: the same entrypoint delegates discovery/enqueue to `app.j1_work_producer`; background workers then execute one claimed fixture at a time through the same canonical runner logic.

The GitHub Actions workflow `.github/workflows/daily-prediction-runner-v1.yml` is an authenticated HTTP fallback, not the primary scheduler. It checks every five minutes during the configured match window and currently requests at most five fixtures per fallback call.

## Per-fixture flow

1. Find a due target-league fixture not already recorded for the J1 snapshot window.
2. Fetch the enriched fixture and persist announced lineups in `prematch_context_snapshots`.
3. Refresh Sportmonks prematch odds into the J1-specific snapshot window.
4. Generate an immutable prediction under `prediction_window=j1_45m_v1`.
5. Evaluate Decision Engine V2 with the J1 odds snapshot.
6. Persist the pre-kickoff BET/NO_BET decision in the forward-test ledger.

In producer mode, queue ownership changes only *who invokes this flow*. The business logic above is not duplicated in the worker.

## Lineup safety

J1 lineups are stored in a **separate prematch table**. They are not written into `fixture_data_snapshots`, because that table is used by historical postgame quality/training logic and a pregame lineup-only snapshot could incorrectly become the latest training snapshot.

The current promoted STANDARD model still uses the existing 36 historical features and **does not yet consume the target match lineup**. J1 lineup capture remains audit/context infrastructure for a future lineup-aware model/Enigma Rating version; it does not silently change the calibrated model.

## HTTP fallback endpoints

- `POST /operations/daily-prediction-runner`
- `GET /operations/daily-prediction-runner/status`

Queue observability for horizontal execution is exposed separately under `/operations/j1-work/*`.

The runner remains research-only and never places a real bet automatically.
