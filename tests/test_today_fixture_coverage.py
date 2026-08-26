from app.j1_scheduler import FIXTURE_COVERAGE_INTERVAL_MINUTES
from app.league_registry import canonical_league


def test_south_american_target_aliases_are_monitored():
    cases = {
        "CONMEBOL Libertadores": "Copa Libertadores",
        "CONMEBOL Copa Libertadores": "Copa Libertadores",
        "CONMEBOL Sudamericana": "Copa Sudamericana",
        "CONMEBOL Copa Sudamericana": "Copa Sudamericana",
        "Brazil Copa do Brasil": "Copa do Brasil",
        "Brazil Serie B": "Serie B",
        "Brazilian Serie B": "Serie B",
    }
    for raw_name, canonical_name in cases.items():
        resolved = canonical_league(raw_name)
        assert resolved["target"] is True
        assert resolved["canonical_name"] == canonical_name


def test_fixture_coverage_refresh_is_frequent_enough_for_j1():
    assert FIXTURE_COVERAGE_INTERVAL_MINUTES == 15
    assert FIXTURE_COVERAGE_INTERVAL_MINUTES < 45
