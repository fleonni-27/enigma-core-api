# Inference Runtime V2

## Goal

Reduce repeated work inside one J1 cycle without changing the promoted STANDARD model, its probabilities, calibration compatibility, or temporal integrity.

## Reuse scope

`InferenceRuntimeV2` lives only for the duration of one Daily Prediction Runner cycle. Fixtures whose target kickoff dates resolve to the same V1 historical window share:

- one `build_full_training_dataset` call;
- one parse/flatten pass over the historical rows;
- one compact prepared-row cache.

Each fixture still receives its own target features, strict `starts_at < target kickoff` training view, training SHA256, model fit, prediction persistence and J1 timing validation.

The cache is memory bounded to at most two historical date windows, covering the midnight boundary case without retaining an unbounded set of datasets.

## Compatibility guarantees

Inference Runtime V2 intentionally keeps:

- `baseline_1x2_temporal_v1`;
- STANDARD 36 features;
- history window = 730 days;
- lookback = 5 matches;
- minimum target history = 3;
- median imputer + StandardScaler + LogisticRegression;
- class weights disabled;
- prediction window `j1_45m_v1`;
- immutable `Prediction` rows;
- V1 training-hash payload semantics.

No lineup, target-match postgame data or J1 odds are introduced into model features.

## Runtime audit

The Daily Prediction Runner response now contains `inference_runtime` with:

- dataset cache entries;
- dataset builds;
- dataset reuses;
- prepared row count;
- training views;
- fit calls;
- dataset build time;
- fit time;
- persisted/reused prediction counts.

A healthy multi-fixture J1 cycle on the same UTC target date should normally show `dataset_builds = 1` and `dataset_reuses >= 1` after the first inference that actually requires a historical dataset.

## Deliberate non-optimization

Model fitting is still per fixture/cutoff. Reusing a fitted estimator across different cutoffs could alter training membership and is outside this change. A future runtime version can cache a fitted estimator only when the training SHA256 is exactly identical.
