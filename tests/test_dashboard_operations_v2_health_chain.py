from __future__ import annotations

import unittest
from unittest.mock import patch

from app import dashboard_operations_v2 as dashboard
from app import dashboard_operations_v2_health as health


class DashboardOperationsV2HealthChainTests(unittest.TestCase):
    def test_health_wrapper_captures_current_builder_at_install_time(self) -> None:
        original_builder = dashboard.build_dashboard_operations_v2
        original_version = dashboard.DASHBOARD_OPERATIONS_V2_VERSION
        original_html = dashboard.DASHBOARD_OPERATIONS_V2_HTML
        original_installed = health._installed
        original_health_builder = health._original_builder

        calls: list[object] = []

        def optimized_builder(*, target_date=None):
            calls.append(target_date)
            return {
                "status": "ok",
                "version": "dashboard_operations_v2",
                "overview": {},
                "policy": {},
                "performance": {
                    "version": "dashboard_operations_v2_bulk_reads_v1",
                    "query_strategy": "fixed_bulk_reads",
                    "data_select_query_count": 5,
                    "per_fixture_query_count": 0,
                },
            }

        try:
            dashboard.build_dashboard_operations_v2 = optimized_builder
            health._installed = False
            health._original_builder = None

            with patch.object(
                health,
                "_scheduler_payload",
                return_value={
                    "status": "HEALTHY",
                    "source": "test",
                    "run_id": 1,
                    "started_at": None,
                },
            ):
                health.install_dashboard_operations_v2_health()
                payload = dashboard.build_dashboard_operations_v2(target_date="sentinel")

            self.assertEqual(calls, ["sentinel"])
            self.assertEqual(
                payload["performance"]["query_strategy"],
                "fixed_bulk_reads",
            )
            self.assertEqual(payload["performance"]["per_fixture_query_count"], 0)
            self.assertEqual(payload["overview"]["scheduler_health"], "HEALTHY")
        finally:
            dashboard.build_dashboard_operations_v2 = original_builder
            dashboard.DASHBOARD_OPERATIONS_V2_VERSION = original_version
            dashboard.DASHBOARD_OPERATIONS_V2_HTML = original_html
            health._installed = original_installed
            health._original_builder = original_health_builder


if __name__ == "__main__":
    unittest.main()
