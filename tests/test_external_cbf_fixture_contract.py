from datetime import date

from app.dashboard_match_center_v3_light import _external_coverage_fixture, CBF_EXTERNAL_COVERAGE


def test_external_cbf_fixture_uses_numeric_internal_id_and_separate_external_reference():
    row = CBF_EXTERNAL_COVERAGE[date(2026, 8, 27)][0]
    fixture = _external_coverage_fixture(row)

    assert isinstance(fixture["fixture_id"], int)
    assert fixture["fixture_id"] < 0
    assert fixture["sportmonks_fixture_id"] is None
    assert fixture["external_id"] == row["external_id"]
    assert fixture["data_quality"]["external_coverage"] is True
    assert fixture["data_quality"]["official_j1_prediction_available"] is False
