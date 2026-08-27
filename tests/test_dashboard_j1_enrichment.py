import unittest
from pathlib import Path

from app.dashboard_j1_team_enrichment import _facts, _relative_strength


class DashboardJ1EnrichmentTests(unittest.TestCase):
    def test_relative_strength_is_bounded_and_inverse_defense_prefers_lower_xga(self):
        a, b = _relative_strength(1.8, 1.2)
        self.assertEqual(a, 100.0)
        self.assertAlmostEqual(b, 66.7, places=1)
        da, db = _relative_strength(0.8, 1.6, inverse=True)
        self.assertEqual(da, 100.0)
        self.assertEqual(db, 50.0)

    def test_facts_are_deterministic_and_do_not_claim_external_news(self):
        home = {"xg": 1.8, "xga": 0.9, "goals_for_avg": 1.7, "form_5": ["V"] * 4 + ["E"]}
        away = {"xg": 1.2, "xga": 1.4, "goals_for_avg": 1.1, "form_5": ["E"] * 5}
        facts = _facts("Casa", "Fora", home, away)
        self.assertTrue(any("produção média de xG" in fact for fact in facts))
        self.assertTrue(any("contenção de xG" in fact for fact in facts))
        self.assertFalse(any("notícia" in fact.lower() for fact in facts))

    def test_match_center_reads_materialized_enrichment_without_provider_call_on_refresh(self):
        source = Path("app/dashboard_match_center_v3_light.py").read_text()
        runner_source = Path("app/dashboard_enrichment_runner.py").read_text()
        html_source = Path("app/dashboard_match_center_v3_5m.py").read_text()
        self.assertIn("load_dashboard_enrichment(fixture_ids)", source)
        self.assertNotIn("build_bulk_team_enrichment(items)", source)
        self.assertIn("build_bulk_team_enrichment(items)", runner_source)
        self.assertIn('"provider_calls_during_dashboard_refresh": False', source)
        self.assertIn('"history_reconstruction_during_dashboard_refresh": False', source)
        self.assertIn('"xg_xga_are_informational_only": True', source)
        self.assertIn("Fatos / alertas Enigma", html_source)


if __name__ == "__main__":
    unittest.main()
