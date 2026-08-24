# Inference Runtime V2

## Goal

Reduce repeated work inside one J1 cycle without changing the promoted STANDARD model, its probabilities, calibration compatibility, or temporal integrity.

## Reuse scope

`InferenceRuntimeV2` lives only for the duration of one Daily Prediction Runner cycle. Fixtures whose target kickoff dates resolve to the same V1 historical window share:

- one `build_full_training_dataset` call;
- one parse/flatten pass over the historical rows;
- one compact prepared-row cache.

Each fixture still receives its own target features, strict `starts_at < target kickoff` training view, training SHA256, prediction persistence and J1 timing validation.

The historical dataset cache is memory bounded to at most two date windows, covering the UTC midnight boundary without retaining an unbounded set of datasets.

## Training Fit Cache V1

The runtime now also has a cycle-local fitted-estimator cache. A fitted STANDARD pipeline is reusable only when all of the following are identical:

- the V1 `training_sha256`;
- the model version;
- the pipeline signature, including class-weight policy and fixed Logistic Regression parameters.

Target features are never stored in the fit cache. A cache hit reuses only the fitted imputer/scaler/classifier and performs a fresh `predict_proba` for the current fixture.

Because the V1 training SHA includes the target cutoff and complete ordered training-row payload, fixtures with different training SHA values never share a fit. This is deliberately conservative and preserves the current temporal semantics.

The fit cache is memory bounded to eight entries per J1 cycle.

## Compatibility guarantees

Inference Runtime V2 intentionally keeps:

- `baseline_1x2_temporal_v1`;
- STANDARD 36 features;
- history window = 730 days;
- lookback = 5 matches;
- minimum target history = 3;
- median imputer + StandardScaler + LogisticRegression;
- `C=1`, L2, `lbfgs`, `max_iter=2000`, `random_state=42`;
- class weights disabled in the production J1 runner;
- prediction window `j1_45m_v1`;
- immutable `Prediction` rows;
- V1 training-hash payload semantics.

No lineup, target-match postgame data or J1 odds are introduced into model features.

## Runtime audit

The Daily Prediction Runner response contains `inference_runtime` with dataset and fit-cache diagnostics, including:

- dataset cache entries, builds and reuses;
- prepared row count and training views;
- fit calls (actual estimator builds);
- fit reuses / cache hits / misses;
- fit cache entries and bounds;
- dataset build time and actual fit time;
- persisted/reused prediction counts;
- nested `fit_cache` policy and pipeline signature.

A multi-fixture J1 cycle on the same UTC target date should normally show `dataset_builds = 1` after the first inference that requires historical data. Fit reuse occurs only when two fixtures resolve to the exact same `training_sha256`; otherwise each SHA produces its own estimator.

## Integrity rule

Fit reuse is an optimization of computation, not a relaxation of the model cutoff. The runtime does not create a coarser training-data hash and does not reuse an estimator merely because two training views happen to have the same row count. Exact V1 `training_sha256` equality is required.
