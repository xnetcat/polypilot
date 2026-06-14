import json
from pathlib import Path

import polymarket_mcp.research.sports as sports

FIX = Path(__file__).parent / "fixtures"


def test_detect_sport_mlb_and_nfl_by_team():
    # MLB and NFL teams must resolve to their ESPN leagues (previously null,
    # which silently disabled live-score lookups for those games).
    assert sports._detect_sport("Tampa Bay Rays vs. Los Angeles Angels: O/U 5.5") == "baseball/mlb"
    assert sports._detect_sport("Will the Dodgers beat the Mets?") == "baseball/mlb"
    assert sports._detect_sport("Will the Chiefs beat the Eagles?") == "football/nfl"


def test_detect_sport_nba_nhl_still_work():
    assert sports._detect_sport("Will the Lakers win tonight?") == "basketball/nba"
    assert sports._detect_sport("Bruins vs Rangers") == "hockey/nhl"


def test_detect_sport_explicit_league_keyword_wins():
    assert sports._detect_sport("MLB: who wins the World Series?") == "baseball/mlb"
    assert sports._detect_sport("Premier League title winner") == "soccer/eng.1"


def test_fetch_sports_parses_event(monkeypatch):
    sports._CACHE.clear()
    data = json.loads((FIX / "espn_scoreboard.json").read_text())

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    monkeypatch.setattr(sports.httpx, "get", lambda *a, **k: R())
    result = sports.fetch_sports("Will the Lakers beat the Celtics?")
    assert "sport" in result
    assert result.get("error") is None or "team1" in result
    assert result["team1"] == "Lakers"
    assert result["team2"] == "Celtics"
    assert result["sport"] == "basketball/nba"


def test_fetch_sports_matches_scoreboard_event(monkeypatch):
    sports._CACHE.clear()
    data = json.loads((FIX / "espn_scoreboard.json").read_text())

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    monkeypatch.setattr(sports.httpx, "get", lambda *a, **k: R())
    # The fixture contains a "New York Knicks at San Antonio Spurs" event.
    # Use bare team names ("Knicks vs Spurs") so the ESPN substring match (first
    # 6 chars of each team name) hits the event name.
    result = sports.fetch_sports("Knicks vs Spurs")
    assert result["sport"] == "basketball/nba"
    assert result["team1"] == "Knicks"
    assert result["score"] is not None
    assert result["status"] in ("pre", "in", "post")
    assert len(result["score"]) == 2


def test_fetch_sports_no_teams_returns_error():
    sports._CACHE.clear()
    result = sports.fetch_sports("What is the weather today?")
    assert "error" in result
