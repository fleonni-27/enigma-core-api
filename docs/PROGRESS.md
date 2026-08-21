# Enigma Core — Progress Checkpoint

Last checkpoint: 2026-08-21

## Current validated architecture

- Historical Controller v2
- Quality Batch v2
- Feature Profile v1
- Repair Incomplete v1
- Upstream Exception v1
- Database-state checkpoint/resume strategy

## Historical ingestion and quality

The historical pipeline ingests fixtures, enriches snapshots with lineups/statistics/xG when available, audits structural quality, assigns feature profiles, repairs incomplete snapshots when upstream data becomes available, and formally quarantines irrecoverable upstream exceptions.

### Core policies

- `skip_existing=true` is the standard resume mechanism.
- Healthy snapshots are not touched by repair operations.
- xG absence is treated as unavailable data, never as xG = 0.
- `training_eligible=false` for incomplete/upstream-unavailable fixtures.
- Upstream exceptions remain `INCOMPLETE` and are excluded from training.
- A month is not marked complete when the audit report is truncated/at its safety limit.
- `dataset_complete` means no missing snapshots, no incomplete snapshots, and no truncated audit.
- `collection_complete` means no pending snapshots and every remaining incomplete snapshot is formally quarantined as upstream unavailable.

## February 2026 validation — five target leagues

Requested leagues:
- Serie A
- Serie B
- La Liga
- Premier League (request alias also accepted as `Premiere League`)
- Copa Libertadores

Historical Controller v2 result:

- Fixtures in scope: 132
- Enriched fixtures: 132
- Missing snapshots: 0
- Coverage: 100%
- Training eligible: 131 (99.2%)
- FULL_XG: 131
- INCOMPLETE: 1
- Upstream exceptions: 1
- Unresolved incomplete snapshots: 0
- `collection_complete: true`
- `dataset_complete: false`

The sole incomplete fixture is Serie A, Flamengo vs Mirassol (`sportmonks_fixture_id=19622049`). Fresh upstream repair did not provide lineups/statistics. It was formally registered as `UPSTREAM_UNAVAILABLE`, remains `INCOMPLETE`, quality score 21, and is not training eligible.

This validates the intended distinction: operational collection may be complete even when the raw dataset cannot be declared structurally complete, provided all remaining incomplete records are formally quarantined.

## February 2026 quality by league

- Premier League: 42/42 training eligible; 100% FULL_XG.
- La Liga: 40/40 training eligible; 100% FULL_XG.
- Serie A: 27/28 training eligible; 27 FULL_XG + 1 INCOMPLETE/upstream exception.
- Copa Libertadores: 22/22 training eligible; 100% FULL_XG.
- Serie B was requested but did not appear in `by_league`; this should eventually be made explicit as `fixtures: 0` when there are no fixtures, so absence cannot be confused with filter/normalization failure.

## April 2026 validation already completed

Historical collection and enrichment were validated for the priority leagues. Quality Batch v2 and Feature Profile v1 demonstrated full snapshot coverage and training eligibility across the tested April datasets, with Serie B correctly represented as `STANDARD_NO_XG` where xG is unavailable rather than zero.

## Next planned layer

`training_dataset_v1`

Goal: transform validated historical snapshots into leakage-safe model-ready datasets.

Requirements:
- select only `training_eligible=true` fixtures;
- explicitly separate `FULL_XG` and `STANDARD_NO_XG` profiles;
- exclude `INCOMPLETE` and `UPSTREAM_UNAVAILABLE` records;
- construct only information that would have been available before each target fixture;
- prevent future-data leakage;
- preserve league/profile/quality metadata for downstream backtesting;
- create a reproducible dataset contract for subsequent predictive models.

## Operational stack

- API/backend: FastAPI/Python repository `fleonni-27/enigma-core-api`
- Source control: GitHub, default branch `main`
- Deployment: Render web service
- Database: Supabase/PostgreSQL
- Football data source: Sportmonks

This document is the persistent engineering checkpoint for the Enigma Core project and should be updated after each major validated architecture milestone.
