from app.j1_rich_provider_context import RICH_PREMATCH_INCLUDE
from app.prematch_provider_context import extract_prematch_provider_context


def test_rich_include_excludes_live_and_postmatch_relations():
    lowered = RICH_PREMATCH_INCLUDE.casefold()
    for forbidden in ("events", "scores", "timeline", "pressure", "ballcoordinates", "postmatchnews"):
        assert forbidden not in lowered
    for required in (
        "participants",
        "lineups.player",
        "lineups.xglineup.type",
        "expectedlineups",
        "sidelined.player",
        "prematchnews",
        "predictions",
        "statistics.type",
        "xgfixture.type",
    ):
        assert required in lowered


def test_extractor_keeps_prematch_sections_and_drops_events_scores():
    payload = {
        "_enigma_include_profile": "rich_prematch",
        "data": {
            "id": 123,
            "name": "Home vs Away",
            "participants": [{"id": 1, "name": "Home"}, {"id": 2, "name": "Away"}],
            "lineups": [{"player_id": 10, "player_name": "Player"}],
            "xGFixture": [{"participant_id": 1, "location": "home", "data": {"value": 1.2}}],
            "prematchNews": [{"title": "Team update"}],
            "events": [{"type": "goal"}],
            "scores": [{"score": {"goals": 1}}],
        },
    }
    result = extract_prematch_provider_context(payload)
    assert result["include_profile"] == "rich_prematch"
    assert result["counts"]["participants"] == 2
    assert result["counts"]["lineups"] == 1
    assert result["counts"]["xg_fixture"] == 1
    assert result["counts"]["prematch_news"] == 1
    assert "events" not in result["sections"]
    assert "scores" not in result["sections"]
    assert result["policy"]["post_kickoff_events_scores_timeline_excluded"] is True
