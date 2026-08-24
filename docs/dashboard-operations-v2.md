# Dashboard Operations V2

Dashboard Operations V2 is the live operational view for the Daily Prediction Runner V1.

## URLs

- HTML: `/dashboard/operations-v2`
- JSON: `/dashboard/api/operations-v2/today`

## What it shows

For every target-league fixture on the current America/Sao_Paulo calendar date:

- fixture and Sportmonks ID
- kickoff and J1 due time (kickoff - 45 minutes)
- daily odds row count and latest refresh
- J1 lineup capture state and player row count
- J1-specific odds row count
- J1 prediction state and probabilities
- Decision Engine state, BET/NO_BET, confidence, odd, edge and EV
- forward-test ledger persistence state

## Operational states

- `WAITING_J1`: J1 is still in the future
- `J1_DUE`: J1 time has arrived and processing has not persisted context yet
- `PROCESSING_J1`: prematch context exists and the rest of the pipeline is still progressing
- `PROCESSING_PREDICTION`: J1 odds exist but prediction is not yet persisted
- `PROCESSING_DECISION`: prediction exists but ledger decision is not yet persisted
- `J1_COMPLETE`: decision is persisted in the forward-test ledger
- `J1_WINDOW_MISSED`: the 20-minute scheduler grace period ended without a ledger record
- `J1_NOT_RECORDED_BEFORE_KICKOFF`: kickoff passed without a J1 ledger record

## Refresh

The page refreshes its read-only JSON data every 60 seconds. It never triggers predictions, decisions or bets itself.

The current STANDARD model remains the promoted 36-feature model. J1 lineups are displayed as operational context but are not yet consumed by the model.
