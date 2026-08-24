from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app import daily_prediction_runner as legacy
from app import j1_pending_selector_v2 as selector


class J1PendingSelectorV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        selector.restore_legacy_j1_selector_for_tests()
        self.now = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
        starts_at = datetime(2026, 8, 24, 20, 45, tzinfo=timezone.utc)
        self.fixtures = [
            SimpleNamespace(
                id=index,
                sportmonks_id=900000 + index,
                starts_at=starts_at,
                league_name="Serie A",
            )
            for index in range(1, 9)
        ]

    def tearDown(self) -> None:
        selector.restore_legacy_j1_selector_for_tests()

    def _run(self, recorded: set[tuple[int, str]], max_fixtures: int = 5):
        with (
            patch.object(selector, "_load_due_candidates", return_value=self.fixtures),
            patch.object(selector, "_target_candidates", return_value=self.fixtures),
            patch.object(selector, "_recorded_fixture_windows", return_value=recorded),
        ):
            return selector.select_pending_j1_fixtures(
                now=self.now,
                max_lateness_minutes=20,
                max_fixtures=max_fixtures,
            )

    def test_recorded_first_batch_does_not_starve_later_due_fixtures(self) -> None:
        window = legacy._snapshot_window(self.fixtures[0])
        recorded = {(fixture.id, window) for fixture in self.fixtures[:5]}

        selected, audit = self._run(recorded)

        self.assertEqual([fixture.id for fixture in selected], [6, 7, 8])
        self.assertEqual(audit["already_recorded_excluded"], 5)
        self.assertEqual(audit["pending_before_limit"], 3)
        self.assertEqual(audit["selected_fixture_count"], 3)
        self.assertEqual(audit["deferred_pending_fixture_count"], 0)
        self.assertTrue(audit["selection_limit_applied_after_recorded_exclusion"])
        self.assertTrue(audit["recorded_fixture_windows_do_not_consume_batch_capacity"])

    def test_limit_is_applied_to_pending_fixtures_only(self) -> None:
        selected, audit = self._run(set())

        self.assertEqual([fixture.id for fixture in selected], [1, 2, 3, 4, 5])
        self.assertEqual(audit["pending_before_limit"], 8)
        self.assertEqual(audit["selected_fixture_count"], 5)
        self.assertEqual(audit["deferred_pending_fixture_count"], 3)

    def test_record_from_other_snapshot_window_does_not_exclude_fixture(self) -> None:
        selected, audit = self._run({(1, "j1_45m_19990101")}, max_fixtures=1)

        self.assertEqual([fixture.id for fixture in selected], [1])
        self.assertEqual(audit["already_recorded_excluded"], 0)

    def test_install_replaces_legacy_selector_interface(self) -> None:
        selector.install_j1_pending_selector_v2()
        self.assertIs(legacy._due_target_fixtures, selector._due_target_fixtures_v2)


if __name__ == "__main__":
    unittest.main()
