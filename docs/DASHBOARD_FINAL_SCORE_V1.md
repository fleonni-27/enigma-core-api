# Dashboard Final Score V1

## Goal

Show the confirmed final regulation-time score for settled forward-test records without mutating the immutable pre-match decision ledger.

## Data flow

1. Outcome Settlement reads the finished fixture result from Sportmonks.
2. `outcome_score_capture` persists the regulation-time score in `fixture_result_records`.
3. The record is immutable after first persistence; conflicting later values are surfaced and never overwrite the stored final score.
4. Dashboard records read the stored fixture result.
5. Older settled records created before this feature may use the latest final `FixtureDataSnapshot` as a read-only fallback when its goals agree with the ledger `actual_result`.

## Dashboard fields

Settled records expose:

- `final_score`
- `home_goals`
- `away_goals`
- `score_source`

The UI shows a `Placar` column beside the match and decision context.

## Safety

- No change to prediction or decision semantics.
- No change to BET/NO_BET thresholds.
- No change to odds, calibration, P&L, or settlement formulas.
- Settled DecisionRecord fields are never rewritten to add a score.
- Regulation-time 1X2 score semantics remain unchanged for AET/penalty fixtures.
