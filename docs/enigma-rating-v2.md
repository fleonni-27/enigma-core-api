# Enigma Rating V2 — Research Signal Stack

## Objective

Enigma Rating V2 adds football-specific model signals that were intentionally absent from Rating V1: independent Poisson, Dixon-Coles, Elo, rolling xG/xGA, exact 10-match form and lineup/absence impact.

V2 is a **research layer**, not a replacement for the promoted prediction model. The production prediction remains `baseline_1x2_temporal_v1`, using the existing STANDARD 36-feature multinomial logistic-regression pipeline. Decision Engine thresholds and immutable forward-test records are unchanged.

## Why V2 is separate from the promoted model

The Enigma Core must be able to measure whether a new signal improves probability quality before the signal changes a production decision. V2 therefore exposes its components, probabilities, data quality and coverage explicitly while preserving the current baseline for clean Brier, Log Loss, calibration and CLV comparison.

`rating` is a 0-100 evidence score. It is **not** a 1X2 probability.

## Component weights

| Component | Weight | Evidence |
| --- | ---: | --- |
| Model confidence | 15 | calibrated Enigma selection probability |
| Market edge | 15 | calibrated probability vs no-vig market probability |
| Poisson | 12 | independent score-grid 1X2 probability |
| Dixon-Coles | 12 | low-score corrected Poisson 1X2 probability |
| Elo | 10 | same-league pre-target Elo with Davidson draw extension |
| xG/xGA | 12 | rolling xG-for minus xG-against strength |
| Recent form 10 | 10 | exact 10-match points per match |
| Lineup impact | 9 | auditable expected-XI value retained after absences |
| Home advantage | 5 | explicit home-field prior |

Missing components are never silently imputed. The weighted score is renormalized over available evidence and `coverage_pct` reports how much of the 100-point evidence budget was actually present.

## Expected-goals input

Poisson and Dixon-Coles share an expected-goals input built from attack and opponent defensive concession.

When both xG and observed goals are available, V2 uses a transparent research blend:

`rate = 0.65 * xG + 0.35 * goals`

When xG is unavailable, observed goals can be used as a fallback and the result is explicitly labelled `GOALS_ONLY`. Partial xG coverage is labelled `MIXED_XG_GOALS`; complete xG/xGA support is labelled `FULL_XG_XGA`.

The home expected-goals rate receives a research home multiplier of `1.08` by default. No xG absence is interpreted as zero.

## Independent Poisson

`app.football_probability_models.poisson_1x2` builds a score grid from independent home and away Poisson distributions and sums the grid into normalized `1`, `X`, `2` probabilities.

Default score-grid ceiling: 10 goals per side.

This is an independent research probability and is not blended into `Prediction.p_home/p_draw/p_away`.

## Dixon-Coles

`app.football_probability_models.dixon_coles_1x2` applies the Dixon-Coles low-score correction to the `0-0`, `0-1`, `1-0` and `1-1` cells before normalizing the 1X2 distribution.

Default research parameter: `rho = -0.08`.

The parameter is currently explicit/configurable, not fitted from the production forward-test sample. Parameter fitting must be evaluated temporally before promotion.

## Elo + Davidson draw model

The fixture context builds Elo only from completed historical fixtures:

- same canonical competition as the target;
- strictly before target kickoff;
- default history horizon: 1460 days;
- initial rating: 1500;
- K factor: 20;
- home advantage: 65 Elo points.

`elo_davidson_1x2` converts home and away strengths into a three-way distribution using a Davidson-style draw term. Default draw parameter: `0.70`.

The target match is never used to update its own pre-match Elo.

## xG and xGA

The previous FULL_XG model-dataset family exposed rolling xG-for but did not expose xGA as a model feature.

V2 context derives both sides without altering the STANDARD 36-feature contract:

- `xg_for` = the team's historical xG in the match;
- `xg_against` = the opponent's historical xG in that same match.

The builder reports separate xG and xGA history counts so partial upstream coverage remains visible.

## Exact 10-match form

The historical dataset layer already supports lookback values up to 10. V2 context requests exactly 10 historical matches and exposes rolling points per match as `home_points_per_match_10` and `away_points_per_match_10`.

The production STANDARD model remains at its existing default 5-match lookback. V2 does not silently change that model contract.

## Lineups and absences

J1 already persists announced lineups in `prematch_context_snapshots`. V2 context reads the latest target fixture snapshot and exposes observed starter rows/player IDs when available.

Lineup **presence** and lineup **impact** are deliberately separate concepts. A starting-XI count is not treated as player strength.

`lineup_impact_v1` only scores lineup impact when an auditable player-value/expected-XI model supplies:

- expected XI total value;
- confirmed absent value;
- or an equivalent bounded retained-strength input.

If those values are unavailable, V2 reports:

`impact_scored = false`

with reason `PLAYER_ABSENCE_VALUE_MODEL_NOT_AVAILABLE`.

No injury, suspension or missing player is assigned an invented weight.

## Fixture context

`GET /rating/context-v2/{sportmonks_fixture_id}`

Builds research inputs from the database and returns:

- rolling goals for/against;
- rolling xG/xGA and coverage;
- exact form-10 PPM;
- pre-target same-league Elo;
- latest persisted J1 lineup evidence;
- leakage and provenance policy.

The endpoint is read-only and does not persist a prediction or decision.

## Rating endpoints

`POST /rating/enigma-v2`

Builds V2 from explicit audited inputs.

`POST /rating/enigma-v2/fixture/{sportmonks_fixture_id}`

Builds historical context automatically and accepts only decision-time inputs such as selection, calibrated probability, market probability/edge and optional auditable lineup values. Database-derived historical inputs cannot be overridden by the request.

POST endpoints remain subject to Enigma Core internal mutating-request authentication even though this V2 path is research-only.

## Promotion policy

V2 must not become the production probability model or alter Decision Engine gates solely because the component stack exists.

Before promotion, measure the new probability signals against `baseline_1x2_temporal_v1` using at minimum:

1. multiclass Brier Score and Brier skill;
2. Log Loss;
3. calibration/ECE by confidence bucket;
4. accuracy only as a secondary metric;
5. CLV coverage and CLV distribution for BET decisions;
6. temporal/league breakdowns;
7. ablation tests for Poisson, Dixon-Coles, Elo, xG/xGA, form-10 and lineup impact;
8. sufficient out-of-sample observations to avoid choosing weights from noise.

Until that evidence exists, `enigma_rating_v2_research_v1` remains diagnostic and cannot override the Decision Engine.

## Remaining V2 research gaps

Not yet scored:

- competition-stage/context effects;
- head-to-head signal with leakage-safe decay;
- similarity-conditioned historical Brier/CLV;
- persisted injury/suspension availability feed;
- learned player contribution / expected-XI valuation;
- fitted Dixon-Coles rho and optimized Elo hyperparameters;
- validated probability ensemble/blending rule.
