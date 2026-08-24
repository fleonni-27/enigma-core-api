# J1 Pending Fixture Selector V2

## Problem fixed

The previous J1 selection path applied the per-cycle `max_fixtures` limit before checking the immutable forward-test ledger. When more fixtures were simultaneously due than the limit, already-recorded fixtures could repeatedly occupy every slot and prevent later due fixtures from reaching Prediction/Decision before the J1 grace window expired.

Example with limit 5 and eight due fixtures:

- cycle 1: fixtures A-E are selected and recorded;
- old cycle 2: A-E are selected again, then identified as already recorded;
- fixtures F-H never reach the runner selection.

## V2 selection order

V2 uses this order:

1. load the complete bounded J1 due window;
2. keep target competitions only;
3. batch-read `DecisionRecord` fixture/window pairs for the J1 ledger source;
4. remove already-recorded fixture/window pairs;
5. preserve kickoff/id ordering;
6. apply the per-cycle `max_fixtures` safety limit to pending fixtures only.

This preserves the existing J1 timing policy and batch limit while preventing completed work from consuming batch capacity.

## Production installation

- HTTP/GitHub Actions fallback installs Selector V2 through `j1_scheduler_routes` before exposing the mutable runner route.
- Render cron runs `python -m app.j1_scheduler_v2`, which installs Selector V2 and then delegates to the existing advisory-lock scheduler.
- The scheduler advisory lock, J1 = kickoff -45 minutes, max lateness policy, Decision Engine V2, Inference Runtime V2, Fit Cache, immutable Prediction and immutable ledger semantics are unchanged.

## Audit fields

The selector exposes audit metadata including:

- due candidate count;
- target candidate count;
- already-recorded fixtures excluded;
- pending fixtures before the limit;
- selected fixture count;
- deferred pending fixture count;
- confirmation that the limit is applied after ledger exclusion.

## Guardrails

This change does not increase `MAX_FIXTURES_PER_RUN`; it only makes the existing capacity available to actual pending work.

API version: `0.38.0`.
