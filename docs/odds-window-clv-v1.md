# Odds Window + Closing + CLV Engine V1

## Objective

Add an operational market timeline around each target fixture without changing model probabilities, J1 thresholds, Decision Engine semantics, or the immutable Forward-Test ledger.

## Windows

- **Opening**: earliest complete 1X2 market actually observed by Enigma Core. This is an observed-opening proxy, not a claim about the bookmaker/exchange true market open.
- **J0**: the existing `daily_YYYYMMDD` same-match-day odds stream.
- **J1**: the existing `j1_45m_YYYYMMDD` stream used by the J1 decision.
- **Closing**: a `closing_YYYYMMDD` stream captured by the primary minute scheduler during the final five minutes before kickoff. Repeated unchanged prices are collapsed by Odds Quote Dedupe V1; genuine movements remain separate states.

All 1X2 comparisons require a complete bookmaker/market triplet and remove bookmaker margin with no-vig normalization.

## CLV

CLV is stored in a separate immutable `clv_records` table. `decision_records` are never mutated.

For the exact bookmaker, market and selection used by the decision:

- `CLV odds decimal = decision_odd / closing_odd - 1`
- `CLV odds % = CLV odds decimal * 100`
- `CLV probability points = closing_no_vig_probability - decision_no_vig_probability`
- `model edge vs closing = calibrated_confidence - closing_no_vig_probability`

Positive odds CLV means Enigma Core obtained a better decimal price than the observed closing price. Positive probability CLV means the no-vig market moved toward the selected side after the J1 decision.

## Operational behavior

The Render J1 cron remains the primary minute scheduler. After the J1 cycle completes, it runs the Closing/CLV cycle. Closing failures are isolated from J1 correctness: a temporary Sportmonks, schema-release or closing-data failure is reported but cannot invalidate a successful Prediction/Decision/Ledger cycle.

CLV is finalized only after kickoff, using the latest coherent complete 1X2 state captured before kickoff. If the exact bookmaker/market closing triplet is missing, no synthetic CLV is persisted; the decision remains eligible for a later retry if closing data becomes available.

## API

- `GET /operations/odds-window/fixture/{sportmonks_fixture_id}`
- `GET /operations/clv`
- `GET /operations/clv?decision=BET`

## Research guardrails

- Research only; no real-money execution.
- No model, calibration, threshold or J1 timing changes.
- No cross-bookmaker substitution for CLV.
- No post-kickoff quote may be used as closing evidence.
- Opening is explicitly labeled as earliest observed, not true exchange/bookmaker opening.
