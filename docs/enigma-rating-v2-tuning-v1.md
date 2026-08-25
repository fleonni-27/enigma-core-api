# Enigma Rating V2 — Tuning V1

## Purpose

Tune research-only challenger parameters without using an observed test partition for selection and without changing the promoted STANDARD model, Decision Engine, Prediction persistence, or Ledger.

## Selection data

- Default window: 2026-01-01 through 2026-08-24.
- Default leagues: Serie A, Serie B, Copa Libertadores, La Liga.
- Only the temporal `validation` partition is used to select parameters.
- The existing observed `test` partition is not an optimization objective.
- Any tuning request whose end date is on or after 2026-08-25 is rejected.

## Objective

1. Primary: multiclass Brier score on validation; lower is better.
2. Tie-break: multiclass Log Loss on validation; lower is better.
3. Accuracy and calibration remain diagnostics and cannot override the primary proper scoring rule.

## Elo-Davidson grid

- K factor: 10, 15, 20, 25, 30.
- Home advantage: 35, 50, 65, 80, 95 Elo points.
- Davidson draw parameter: 0.50, 0.60, 0.70, 0.80, 0.90.
- Total: 125 candidates.
- Elo warmup remains 1460 days by default.

## Dixon-Coles grid

- rho: -0.20, -0.16, -0.12, -0.08, -0.04, 0.00, 0.04, 0.08, 0.12.
- Selection is based on the goals-only validation arm because it has broad coverage.
- The xG/xGA arm is reported as a secondary diagnostic and cannot drive rho selection while its coverage is partial.

## Future confirmation holdout

- Frozen start: 2026-08-25.
- End: open until the confirmation sample is mature.
- Minimum eligible targets before confirmation: 100.
- No parameter tuning may use fixtures on or after 2026-08-25.
- Once the winners are selected, their values are committed as a frozen parameter artifact.
- Confirmation must use those exact frozen values without retuning.
- Any parameter change after the holdout starts creates a new research version and requires a new untouched future holdout.

## Endpoint

`GET /research/enigma-rating-v2/tuning-v1`

The endpoint returns the grid definition, winning candidates, top candidates, validation audit, and holdout policy. `include_grid=true` exposes all candidates for audit.

## Promotion rule

Tuning success does not promote a model. Promotion can only be considered after the untouched future holdout reaches the minimum sample and the frozen challenger demonstrates acceptable probabilistic quality, calibration, coverage, and operational behavior.
