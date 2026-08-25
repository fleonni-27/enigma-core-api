# Forward-Test Report V2

## Objective

`forward_test_report_v2` is the read-only evaluation layer for the immutable Enigma Core forward-test ledger. It combines strategy economics, closing-line quality and probability quality without changing decisions, thresholds, predictions, settlement or CLV records.

Endpoint:

`GET /research/forward-test/report-v2`

Optional filters: `start_date`, `end_date`, `league`, `bookmaker`, `decision`, `source`, `max_records`.

Date filters are interpreted in `America/Sao_Paulo` and converted to UTC for storage queries.

## Populations

The report intentionally keeps different denominators separate:

- ROI: settled `BET` records only, one hypothetical unit per bet.
- CLV: `BET` records with finalized `CLVRecord` only.
- Multiclass Brier / Log Loss / Accuracy: all settled records with valid raw 1/X/2 probabilities.
- Favorite calibration: all settled records with valid calibrated favorite confidence and a team-favorite selection.

This avoids mixing strategy selection performance with model probability quality.

## Metrics

### Economics

`ROI = sum(hypothetical_pnl_units) / settled_bet_count`

Because settlement uses a fixed one-unit hypothetical stake, `settled_bet_count` is also stake units.

### CLV

The report consumes the immutable V1 CLV layer:

`CLV odds = decision_odd / closing_odd - 1`

The exact same bookmaker, market and selection are required by the CLV engine. The report exposes average CLV %, median CLV %, positive-CLV rate, probability-point CLV and CLV coverage.

### Probability quality

Multiclass Brier:

`mean(sum((p_class - y_class)^2 for class in [1, X, 2]))`

Log Loss:

`mean(-ln(p_actual_result))`

Stored rounded 1/X/2 probabilities must pass the existing 0.98-1.02 sum validity gate before a tiny normalization for rounding drift. Invalid rows are excluded and counted in coverage.

### Favorite calibration

The promoted favorite-confidence calibrator is evaluated as a binary forecast:

- prediction = `calibrated_favorite_confidence`
- outcome = 1 when the selected team favorite wins, otherwise 0 (draw is a favorite failure)

The report exposes binary Brier, average confidence, observed favorite success rate, calibration gap, ECE and MCE.

ECE is the confidence-bucket weighted absolute calibration gap. MCE is the maximum absolute bucket gap.

## Fixed buckets

Edge buckets are percentage points:

- `<0pp`
- `0-<2.5pp`
- `2.5-<5pp`
- `5-<7.5pp`
- `7.5-<10pp`
- `>=10pp`

Confidence buckets:

- `<45%`
- `45-<50%`
- `50-<55%`
- `55-<60%`
- `60-<65%`
- `>=65%`

The same aggregate scorecard is produced for edge bucket, confidence bucket, league and bookmaker.

## Guardrails

The report is diagnostic only. It does not mutate `DecisionRecord` or `CLVRecord`, does not enable real-money execution and does not automatically retune thresholds. If the selected scope exceeds `max_records`, the request fails instead of returning silently partial metrics.
