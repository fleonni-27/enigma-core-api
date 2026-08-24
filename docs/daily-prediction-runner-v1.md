# Daily Prediction Runner V1

The Daily Prediction Runner closes the operational gap between daily fixture/odds ingestion and the forward-test ledger.

## J1 policy

- J1 target: **45 minutes before kickoff**.
- The runner never executes before kickoff - 45m.
- GitHub Actions checks every 15 minutes during the normal target-league match window (07:00-23:59 America/Sao_Paulo).
- Because scheduled GitHub jobs can be delayed, a fixture remains eligible for up to 20 minutes after J1 becomes due.
- The response records `j1_due_at`, `minutes_to_kickoff`, and `minutes_after_j1_due` so timing is auditable.

## Per-fixture flow

1. Find due target-league fixtures already ingested by Daily Operations.
2. Fetch the enriched fixture and persist the announced lineups in `prematch_context_snapshots`.
3. Refresh Sportmonks prematch odds into a J1-specific snapshot window (`j1_45m_YYYYMMDD`).
4. Generate an immutable prediction under `prediction_window=j1_45m_v1`.
5. Evaluate the Decision Engine with the J1 odds snapshot.
6. Persist the pre-kickoff BET/NO_BET decision in the forward-test ledger.

## Lineup safety

J1 lineups are stored in a **separate prematch table**. They are not written into `fixture_data_snapshots`, because that table is used by historical postgame quality/training logic and a pregame lineup-only snapshot could incorrectly become the latest training snapshot.

The current promoted STANDARD model still uses the existing 36 historical features and **does not yet consume the target match lineup**. J1 lineup capture is therefore audit/context infrastructure for a future lineup-aware model/Enigma Rating version; it does not silently change the calibrated model.

## Endpoints

- `POST /operations/daily-prediction-runner`
- `GET /operations/daily-prediction-runner/status`

The runner remains research-only and never places a real bet automatically.
