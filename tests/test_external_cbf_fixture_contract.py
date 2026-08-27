from app.dashboard_confirmation_holdout_v1 import _numeric_fixture_id


def test_confirmation_holdout_ignores_external_nonnumeric_fixture_ids():
    assert _numeric_fixture_id({"fixture_id": "cbf-copa-do-brasil-2026-jogo-143"}) is None
    assert _numeric_fixture_id({"fixture_id": None}) is None
    assert _numeric_fixture_id({"fixture_id": 123}) == 123
    assert _numeric_fixture_id({"fixture_id": "456"}) == 456
