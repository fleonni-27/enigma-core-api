from datetime import date

from app.daily_operations_business_date_fix import _merge_business_date_payloads


def _row(fixture_id: int, starting_at: str) -> dict:
    return {"id": fixture_id, "starting_at": starting_at}


def test_late_brazil_fixture_from_following_utc_date_is_selected():
    payload = _merge_business_date_payloads(
        target_date=date(2026, 8, 25),
        payloads=[
            {"data": [_row(1, "2026-08-25T22:00:00+00:00")]},
            {"data": [_row(2, "2026-08-26T00:00:00+00:00")]},
        ],
    )
    assert [row["id"] for row in payload["data"]] == [1, 2]
    assert payload["meta"]["queried_upstream_dates"] == ["2026-08-25", "2026-08-26"]


def test_following_local_day_fixture_is_not_selected():
    payload = _merge_business_date_payloads(
        target_date=date(2026, 8, 25),
        payloads=[
            {"data": []},
            {"data": [_row(3, "2026-08-26T12:00:00+00:00")]},
        ],
    )
    assert payload["data"] == []


def test_duplicate_fixture_across_upstream_buckets_is_deduplicated():
    row = _row(7, "2026-08-26T00:00:00+00:00")
    payload = _merge_business_date_payloads(
        target_date=date(2026, 8, 25),
        payloads=[{"data": [row]}, {"data": [dict(row)]}],
    )
    assert len(payload["data"]) == 1
    assert payload["meta"]["deduplicated_by_sportmonks_fixture_id"] is True
