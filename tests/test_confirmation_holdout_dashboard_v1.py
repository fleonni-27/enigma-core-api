from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.dashboard_confirmation_holdout_v1 import _inject_holdout_ui
from app.enigma_rating_v2_confirmation_holdout import (
    CONFIRMATION_HOLDOUT_VERSION,
    HOLDOUT_J1_PREDICTION_WINDOW,
    _candidate_metadata,
    _target_payload,
)
from app.enigma_rating_v2_frozen_params import FROZEN_SELECTION_SHA256


def test_holdout_candidate_starts_on_august_25_sao_paulo() -> None:
    before = _candidate_metadata(
        starts_at=datetime(2026, 8, 25, 2, 59, tzinfo=timezone.utc),
        league_name="Serie A",
    )
    at_start = _candidate_metadata(
        starts_at=datetime(2026, 8, 25, 3, 0, tzinfo=timezone.utc),
        league_name="Serie A",
    )
    assert before["candidate"] is False
    assert "BEFORE_CONFIRMATION_HOLDOUT_START" in before["reason_codes"]
    assert at_start["candidate"] is True
    assert at_start["reason_codes"] == []


def test_holdout_candidate_is_restricted_to_frozen_leagues() -> None:
    row = _candidate_metadata(
        starts_at=datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc),
        league_name="Premier League",
    )
    assert row["candidate"] is False
    assert "LEAGUE_NOT_IN_FROZEN_SELECTION" in row["reason_codes"]


def test_target_payload_exposes_identity_not_performance() -> None:
    target = SimpleNamespace(
        target_number=7,
        fixture_id=101,
        sportmonks_fixture_id=202,
        league="Serie A",
        home_team="Home",
        away_team="Away",
        fixture_starts_at=datetime(2026, 8, 26, 22, 0, tzinfo=timezone.utc),
        captured_at=datetime(2026, 8, 26, 21, 15, tzinfo=timezone.utc),
        selection_sha256=FROZEN_SELECTION_SHA256,
        holdout_version=CONFIRMATION_HOLDOUT_VERSION,
    )
    payload = _target_payload(target, settlement_status="SETTLED")
    assert payload["target_number"] == 7
    assert payload["state"] == "SETTLED_TARGET"
    forbidden = {"brier", "log_loss", "accuracy", "ece", "calibration"}
    assert forbidden.isdisjoint(payload)


def test_match_center_html_contains_progress_and_no_peeking() -> None:
    source = '<style></style></head><body><div id="app" class="grid"></div><script>async function load(){document.getElementById(\'app\').innerHTML=(x.fixtures||[]).map(card).join(\'\')||\'<div class="muted">Nenhum jogo-alvo hoje.</div>\'}</script></body>'
    html = _inject_holdout_ui(source)
    assert 'id="holdout"' in html
    assert "CONFIRMATION HOLDOUT" in html
    assert "NO PEEKING" in html
    assert "Métricas bloqueadas" in html
    assert "decorateHoldoutFixtures" in html


def test_production_wiring_precedes_legacy_dashboard_routes() -> None:
    source = Path("app/main_v017.py").read_text()
    assert "install_confirmation_holdout_j1_hook()" in source
    assert "install_confirmation_holdout_startup(app)" in source
    confirmation = source.index("app.include_router(dashboard_confirmation_holdout_router)")
    legacy = source.index("app.include_router(dashboard_match_center_v3_router)")
    assert confirmation < legacy


def test_j1_hook_uses_reserved_forward_window_contract() -> None:
    assert HOLDOUT_J1_PREDICTION_WINDOW == "j1_45m_v1"
    hook = Path("app/enigma_rating_v2_confirmation_hook.py").read_text()
    assert "capture_confirmation_holdout_target" in hook
    assert "ledger.get(\"status\") not in {\"persisted\", \"exists\"}" in hook
    assert "runner.persist_evaluated_decision = persist_with_confirmation_capture" in hook
