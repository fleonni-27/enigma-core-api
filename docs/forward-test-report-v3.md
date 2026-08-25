# Forward-Test Report V3

Endpoint: `GET /research/forward-test/report-v3`

V3 preserves `/research/forward-test/report-v2` and extends the diagnostic layer without changing prediction, decision, settlement or CLV persistence.

## Scorecard

`overview.scorecard` surfaces the compact operational view:

- ROI on settled BET records only.
- Average odds CLV and finalized CLV coverage on BET records whose kickoff is already due.
- Multiclass Brier and Log Loss.
- Skill scores versus a uniform 1/X/2 baseline.
- ECE for raw predicted-class probabilities.
- ECE for calibrated favorite decision confidence.

## Probability quality

`overview.probability_quality` reports:

- Multiclass Brier (same unnormalized 0..2 definition used by V2).
- Log Loss.
- Accuracy.
- Average probability assigned to the realized outcome.
- Uniform 1/X/2 baselines.
- Brier and Log Loss skill scores versus uniform.
- Empirical-climatology baselines and skill scores.

A positive skill score means the model beats the stated baseline for that scoring rule. A negative score means it underperforms the baseline.

## Calibration

V3 separates calibration into three diagnostics:

1. `favorite_decision_confidence`: the existing calibrated favorite confidence against whether the selected favorite won.
2. `predicted_class_raw_probability`: the maximum raw 1/X/2 probability against whether the argmax class was correct.
3. `classwise_raw_probability`: each of 1, X and 2 evaluated against observed class frequency using fixed 10 percentage-point bins.

The raw-probability reliability curves use fixed bins so results remain comparable across report runs.

## CLV quality

`overview.clv` separates:

- total BET records;
- CLV-due BET records (kickoff <= report generation time);
- finalized CLV count;
- missing finalized CLV count;
- finalized coverage rate;
- odds CLV distribution (mean, median, P25, P75, min, max and positive rate);
- probability CLV distribution;
- closing quote lead-time distribution and any post-kickoff closing evidence.

Future bets are excluded from the CLV coverage denominator, so a scheduled match does not look like missing CLV before its closing window exists.

## Diagnostic readiness

`overview.diagnostic_readiness` is an observability gate only. It is not a significance test and it is not an auto-betting gate.

Current directional diagnostics use:

- 30 settled records as an early-sample floor;
- 30 settled bets for ROI review;
- 30 probability-score observations;
- 30 calibration observations;
- 80% finalized CLV coverage on CLV-due bets.

Possible statuses are `DATA_NOT_READY`, `PARTIAL_DATA`, `EARLY_SAMPLE`, and `READY_FOR_DIRECTIONAL_REVIEW`.
