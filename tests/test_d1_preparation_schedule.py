from pathlib import Path


def test_daily_operations_sync_has_d1_and_day_refresh_windows() -> None:
    source = Path('.github/workflows/daily-operations-sync.yml').read_text(encoding='utf-8')
    assert 'cron: "40 2 * * *"' in source
    assert 'cron: "0 9 * * *"' in source
    assert 'cron: "0 15 * * *"' in source
    assert 'D1_PRELOAD_2340' in source
    assert "date -d 'tomorrow' +%F" in source
    assert 'target_date=${TARGET_DATE}&refresh_odds=true' in source


def test_match_center_exposes_today_tomorrow_without_changing_j1_semantics() -> None:
    source = Path('app/dashboard_match_center_v3_5m.py').read_text(encoding='utf-8')
    assert 'DASHBOARD_MATCH_CENTER_V3_REFRESH_MS = 300_000' in source
    assert '>Hoje</a>' in source
    assert '>Amanhã</a>' in source
    assert 'PRÉ-J1 · D+1' in source
    assert 'j1_predictions_remain_official_only_inside_j1_window' in source
    assert '?target_date=' in source
