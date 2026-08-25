# Enigma Rating V2 Evaluation V1

`enigma_rating_v2_evaluation_v1` is the first research-only temporal challenger evaluation layer for Enigma Core.

It does **not** promote a new production model and does not mutate Prediction, Decision, Ledger, J1 or settlement state.

## Goal

Compare the promoted STANDARD baseline with independent research probability signals on a strict temporal holdout:

- `STANDARD` — `baseline_1x2_temporal_v1`, STANDARD family, 36 features;
- `POISSON_GOALS_ONLY`;
- `POISSON_XG_XGA` — the current 65% xG/xGA + 35% goals rate blend;
- `DIXON_COLES_GOALS_ONLY`;
- `DIXON_COLES_XG_XGA`;
- `ELO_DAVIDSON`.

The test partition is the primary final holdout. Validation is reported separately so future parameter selection can use validation without silently tuning on test.

## Endpoint

`GET /research/enigma-rating-v2/evaluation-v1`

Required query parameters:

- `start_date=YYYY-MM-DD`
- `end_date=YYYY-MM-DD`

Useful optional parameters:

- `leagues=Serie A&leagues=La Liga`
- `lookback_matches=5`
- `min_history_matches=3`
- `max_rows=1000`
- `dixon_coles_rho=-0.08`
- `elo_k_factor=20`
- `elo_home_advantage=65`
- `elo_draw_parameter=0.70`
- `poisson_home_multiplier=1.08`
- `elo_warmup_days=1460`
- `include_rows=false`

`include_rows=true` is intended only for bounded research inspection because it can return one row per validation/test target.

## Temporal contract

STANDARD is built with the existing canonical baseline pipeline:

- chronological train/validation/test split;
- identical kickoff timestamps never cross split boundaries;
- imputer, scaler and classifier fit on train only;
- no target-match postgame feature use.

Challenger context is then evaluated in one chronological fixture stream.

For each kickoff timestamp:

1. all targets at that timestamp are scored from state created only by strictly earlier fixtures;
2. only after every target at that timestamp is scored are those match outcomes allowed to update rolling history and Elo for future timestamps.

This prevents leakage between simultaneous matches.

Historical target postgame snapshots may exist in storage, but the evaluator does not use a target outcome to score that target. Target outcomes enter rolling state only after scoring and therefore can affect only later fixtures.

## Rolling evidence

Goals/xG/xGA/form context uses the last 10 same-canonical-league observations per team.

Rate signals require at least 3 historical matches per side.

The xG/xGA challenger requires all four evidence channels to have at least 3 observations:

- home xG-for;
- home xG-against;
- away xG-for;
- away xG-against.

xGA is the opponent's historical xG in the same historical fixture. Missing xG is never interpreted as zero.

## Elo policy in Evaluation V1

Evaluation V1 uses an **expanding pre-target Elo state initialized at the evaluation warmup boundary**. The default warmup is 1460 days before the earliest validation/test target.

This is temporally safe and efficient, but it is intentionally described separately from the single-fixture Rating V2 context, whose `elo_history_days` contract can rebuild Elo over a per-target rolling horizon.

Before any future Elo promotion, this semantic difference must either be standardized or explicitly retained as the chosen evaluation/training policy.

## Metrics

Each model reports:

- coverage;
- multiclass Brier score;
- Log Loss;
- accuracy;
- average probability assigned to the actual result;
- Brier and Log Loss skill versus uniform 1/3 probabilities;
- empirical climatology and skill versus climatology;
- predicted-class calibration curve;
- ECE and MCE.

The test holdout also reports:

- results by canonical league;
- results by calendar month;
- paired challenger-minus-STANDARD deltas on identical fixtures.

For paired deltas, **negative is better** because lower Brier and lower Log Loss are better.

## xG/xGA ablation

Evaluation V1 contains a direct paired ablation:

- `POISSON_XG_XGA` vs `POISSON_GOALS_ONLY`;
- `DIXON_COLES_XG_XGA` vs `DIXON_COLES_GOALS_ONLY`.

Only fixtures where both variants are available enter the paired delta. This prevents coverage differences from masquerading as model improvement.

## Form-10

Exact form-10 is reported as an evidence/readiness slice. Evaluation V1 deliberately does **not** invent a probability mapping from points-per-match difference.

A real form-10 ablation requires a learned predictive specification trained with and without form-10 inputs on the training partition. That is a later V2 Evaluation milestone.

## Dixon-Coles terminology

The current challenger is `dixon_coles_low_score_adjustment` applied to expected-goal rates derived from historical goals or the xG/xGA blend.

It is not yet a fully fitted Dixon-Coles attack/defence model with jointly estimated team parameters and rho.

## Promotion policy

Evaluation V1 cannot promote a model automatically.

Future promotion requires, at minimum:

1. enough temporal holdout volume;
2. stable Brier and Log Loss improvement versus STANDARD on common coverage;
3. acceptable calibration;
4. league/time stability rather than one isolated aggregate gain;
5. parameter tuning on training/validation only;
6. a final untouched temporal test after tuning;
7. separate forward-test/CLV confirmation.

Until then `baseline_1x2_temporal_v1` remains the promoted production model.
