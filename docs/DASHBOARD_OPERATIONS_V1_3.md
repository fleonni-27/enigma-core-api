# Dashboard Operations & Audit V1.3

## Purpose

Provide read-only operational observability for the Enigma Core forward-test pipeline without changing model decisions, settlement, thresholds, or execution policy.

## Routes

- `GET /dashboard/operations` — HTML operations view.
- `GET /dashboard/api/operations?days=90` — JSON operational metrics.

## Metrics

The operations endpoint reports:

- total ledger records and unique fixtures;
- duplicate fixture groups and excess records;
- records produced by future-batch sources versus manual/other sources;
- reconstructed batch runs grouped by `snapshot_window`;
- estimated batch latency derived from the `batch_YYYYMMDDTHHMMSSZ` timestamp and ledger `recorded_at` values;
- audit-only market outliers;
- source breakdown.

## Batch latency limitation

Latency is reconstructed only from persisted ledger records. A selected fixture that stops at inference-not-ready creates no ledger row, so the real batch duration can be longer than the reported estimate.

## Audit-only outliers

The initial operational flags are deliberately separate from Decision Engine thresholds:

- absolute edge >= 15 percentage points;
- absolute expected value >= 50%;
- selected odd >= 4.00.

These flags do not modify, reject, approve, or overwrite any decision. They exist only to prioritize review of large model-market divergences.

## Sample integrity

The automated forward batch defaults to `skip_existing_fixtures=true`. Operations V1.3 verifies the resulting ledger by surfacing any fixture with more than one record inside the selected dashboard window.

## Policy

- research only;
- read only;
- no stake sizing;
- no real-money execution;
- no retroactive decision changes;
- audit thresholds are not betting thresholds.
