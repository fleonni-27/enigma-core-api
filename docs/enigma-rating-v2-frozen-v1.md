# Enigma Rating V2 — Frozen Tuning V1

## Frozen research hypothesis

Selection used only the temporal validation partition from 2026-01-01 through 2026-08-24 across Serie A, Serie B, Copa Libertadores, and La Liga. The previously observed test partition was not used for parameter selection.

Final frozen values after one coarse grid and exactly one boundary refinement stage:

- Elo initial: 1500
- Elo K factor: 45
- Elo home advantage: 110
- Davidson draw parameter: 0.50
- Elo warmup: 1460 days
- Dixon-Coles rho: 0.24
- Poisson home multiplier: 1.08 (not tuned in this protocol)

Selection provenance SHA-256:

`3d7a37a3c81cf383f08057e6ecfa1b8cf18abe5a2a7421698fdcf31acb736dcc`

The final Elo winner produced validation Brier 0.564545 and Log Loss 0.955463 on 100 covered targets out of 102. The final Dixon-Coles goals-only winner produced Brier 0.594636 and Log Loss 0.995306 on all 102 validation targets.

Both Elo K=45 and Dixon-Coles rho=0.24 landed on the upper edge of the single refinement grid. They are intentionally frozen anyway. This is treated as a hypothesis to be judged by future confirmation, not evidence that those values are globally optimal.

## Confirmation holdout

- Start: 2026-08-25
- End: open until the sample matures
- Minimum eligible targets: 100
- No performance peeking before the minimum target count
- No retuning with holdout data
- Confirmation must use the exact frozen parameter manifest and selection SHA
- Any parameter change creates a new research version and requires a new future untouched holdout

## Production isolation

This freeze remains research-only. STANDARD remains the promoted production model. Decision Engine, Prediction persistence, J1 execution, and Ledger are unchanged.

## Read-only manifest

`GET /research/enigma-rating-v2/frozen-v1`
