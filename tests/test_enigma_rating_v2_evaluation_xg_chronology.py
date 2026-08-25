import unittest
from collections import deque
from pathlib import Path

import app.enigma_rating_v2_evaluation as evaluation
from app.enigma_rating_v2_evaluation_chronology import (
    EVALUATION_LOADER_VERSION,
    FIXTURE_PAGE_SIZE,
    RATE_HISTORY_MAX_MATCHES,
    XG_HISTORY_MAX_OBSERVATIONS,
    _append_observation,
    _compose_model_history,
    _evaluate_challengers_chronologically,
    install_evaluation_v1_xg_chronology_fix,
)


class EnigmaRatingV2XGChronologyTests(unittest.TestCase):
    @staticmethod
    def _observation(*, xg: float | None) -> dict:
        return {
            "points": 1.0,
            "goals_for": 1.0,
            "goals_against": 1.0,
            "xg_for": xg,
            "xg_against": (xg + 0.1) if xg is not None else None,
        }

    def test_missing_xg_matches_do_not_evict_valid_xg_evidence(self):
        rate_history = deque(maxlen=RATE_HISTORY_MAX_MATCHES)
        xg_history = deque(maxlen=XG_HISTORY_MAX_OBSERVATIONS)

        for value in (1.1, 1.2, 1.3):
            appended = _append_observation(
                rate_history=rate_history,
                xg_history=xg_history,
                observation=self._observation(xg=value),
            )
            self.assertTrue(appended)

        for _ in range(10):
            appended = _append_observation(
                rate_history=rate_history,
                xg_history=xg_history,
                observation=self._observation(xg=None),
            )
            self.assertFalse(appended)

        self.assertEqual(len(rate_history), 10)
        self.assertEqual(len(xg_history), 3)
        summary = _compose_model_history(rate_history, xg_history)
        self.assertEqual(summary["history_matches"], 10)
        self.assertEqual(summary["xg_for_history_matches"], 3)
        self.assertEqual(summary["xg_against_history_matches"], 3)
        self.assertAlmostEqual(summary["xg_for_avg"], 1.2)

    def test_three_retained_xg_observations_make_full_models_eligible(self):
        def build_history() -> dict:
            rate_history = deque(maxlen=RATE_HISTORY_MAX_MATCHES)
            xg_history = deque(maxlen=XG_HISTORY_MAX_OBSERVATIONS)
            for value in (1.0, 1.2, 1.4):
                _append_observation(
                    rate_history=rate_history,
                    xg_history=xg_history,
                    observation=self._observation(xg=value),
                )
            for _ in range(7):
                _append_observation(
                    rate_history=rate_history,
                    xg_history=xg_history,
                    observation=self._observation(xg=None),
                )
            return _compose_model_history(rate_history, xg_history)

        home = build_history()
        away = build_history()
        models = evaluation._expected_goal_models(
            home_history=home,
            away_history=away,
            dixon_coles_rho=-0.08,
            home_advantage_multiplier=1.08,
        )
        self.assertIsNotNone(models[evaluation.MODEL_POISSON_GOALS])
        self.assertIsNotNone(models[evaluation.MODEL_DC_GOALS])
        self.assertIsNotNone(models[evaluation.MODEL_POISSON_XG])
        self.assertIsNotNone(models[evaluation.MODEL_DC_XG])

    def test_install_replaces_only_the_chronological_evaluator(self):
        original_builder = evaluation.build_enigma_rating_v2_evaluation_v1
        original_router = evaluation.router

        install_evaluation_v1_xg_chronology_fix()

        self.assertIs(
            evaluation._evaluate_challengers_chronologically,
            _evaluate_challengers_chronologically,
        )
        self.assertIs(evaluation.build_enigma_rating_v2_evaluation_v1, original_builder)
        self.assertIs(evaluation.router, original_router)


class EnigmaRatingV2MemoryHardeningContractTests(unittest.TestCase):
    def test_1460_day_default_is_preserved(self):
        source = Path("app/enigma_rating_v2_evaluation.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_ELO_WARMUP_DAYS = 1460", source)

    def test_loader_is_bounded_and_does_not_materialize_full_snapshot_history(self):
        source = Path("app/enigma_rating_v2_evaluation_chronology.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(EVALUATION_LOADER_VERSION, "evaluation_v1_streaming_loader_v1")
        self.assertLessEqual(FIXTURE_PAGE_SIZE, 128)
        self.assertIn("func.row_number()", source)
        self.assertIn('ranked.c.snapshot_rank == 1', source)
        self.assertIn('"lineups_not_loaded": True', source)
        self.assertIn('"older_snapshots_not_materialized": True', source)
        self.assertIn('"raw_snapshot_objects_retained_across_pages": False', source)
        self.assertIn("snapshot_map.clear()", source)
        self.assertIn("page.clear()", source)
        self.assertNotIn("evaluation._latest_snapshot_map(", source)
        self.assertNotIn("payload_cache =", source)
        self.assertNotIn("select(Fixture)", source)
        self.assertNotIn("FixtureDataSnapshot.lineups", source)

    def test_canonical_league_filter_happens_before_snapshot_json_load(self):
        source = Path("app/enigma_rating_v2_evaluation_chronology.py").read_text(
            encoding="utf-8"
        )
        canonical_at = source.index("eligible_page = [")
        snapshot_at = source.index("snapshot_map = _latest_snapshot_payloads")
        self.assertLess(canonical_at, snapshot_at)
        self.assertIn(
            '"league_canonicalization_before_snapshot_json_load": True',
            source,
        )

    def test_same_timestamp_guard_is_preserved_across_page_boundaries(self):
        source = Path("app/enigma_rating_v2_evaluation_chronology.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("pending_group", source)
        self.assertIn("pending_group_starts_at", source)
        self.assertIn(
            '"same_timestamp_group_may_span_page_boundary": True',
            source,
        )
        self.assertIn(
            '"same_timestamp_targets_are_scored_before_any_same_timestamp_result_update": True',
            source,
        )


if __name__ == "__main__":
    unittest.main()
