import json
from pathlib import Path

import polymarket_mcp.research.sports as sports

FIX = Path(__file__).parent / "fixtures"


def _clear():
    sports._CACHE.clear()
    sports._SCOREBOARD_CACHE.clear()


def _scoreboard(name, short, state="in", scores=(110, 105), teams=("Home", "Away")):
    """A minimal ESPN scoreboard payload with one event."""
    return {"events": [{
        "name": name, "shortName": short,
        "competitions": [{
            "status": {"type": {"state": state, "detail": "Q4 2:00"}},
            "competitors": [
                {"team": {"displayName": teams[0]}, "score": str(scores[0]), "homeAway": "home"},
                {"team": {"displayName": teams[1]}, "score": str(scores[1]), "homeAway": "away"},
            ],
        }],
    }]}


def _mock_by_slug(monkeypatch, slug_to_payload):
    """Mock httpx.get so each ESPN scoreboard slug returns its mapped payload
    (matched by substring); any other slug returns no events. This lets the
    dynamic league probe find the game under the CORRECT league only."""
    def fake_get(url, *a, **k):
        payload = {"events": []}
        for sub, data in slug_to_payload.items():
            if sub in url:
                payload = data
                break

        class R:
            def raise_for_status(self): pass
            def json(self): return payload
        return R()

    monkeypatch.setattr(sports.httpx, "get", fake_get)


# --- detection is keyword-only; team->league is dynamic -------------------

def test_detect_sport_is_keyword_only():
    # League NAMES resolve; bare TEAM names do NOT (resolved dynamically instead).
    assert sports._detect_sport("MLB: who wins tonight?") == "baseball/mlb"
    assert sports._detect_sport("Premier League title winner") == "soccer/eng.1"
    assert sports._detect_sport("College Football championship") == "football/college-football"
    assert sports._detect_sport("Tampa Bay Rays vs. Los Angeles Angels") is None
    assert sports._detect_sport("Will the Lakers beat the Celtics?") is None


# --- dynamic resolution: team names matched against live scoreboards -------

def test_dynamic_resolves_league_by_matching_teams(monkeypatch):
    _clear()
    # No league keyword; only the MLB scoreboard has this game -> probe finds it.
    _mock_by_slug(monkeypatch, {
        "baseball/mlb": _scoreboard(
            "Tampa Bay Rays at Los Angeles Angels", "TB @ LAA",
            state="in", scores=(3, 2), teams=("Los Angeles Angels", "Tampa Bay Rays")),
    })
    out = sports.fetch_sports("Tampa Bay Rays vs. Los Angeles Angels: O/U 5.5")
    assert out["sport"] == "baseball/mlb"      # discovered dynamically, no hardcoding
    assert out["status"] == "in"               # live game
    assert out["combined_score"] == 5          # 3 + 2 runs (for the O/U)
    assert {c["team_name"] for c in out["score"]} == {"Los Angeles Angels", "Tampa Bay Rays"}


def test_dynamic_resolves_global_soccer(monkeypatch):
    _clear()
    # A global league (EPL) the bot has no team list for — found by probing.
    _mock_by_slug(monkeypatch, {
        "soccer/eng.1": _scoreboard(
            "Arsenal vs Chelsea", "ARS v CHE", state="post", scores=(2, 1),
            teams=("Arsenal", "Chelsea")),
    })
    out = sports.fetch_sports("Will Arsenal beat Chelsea?")
    assert out["sport"] == "soccer/eng.1"
    assert out["team1"] == "Arsenal"
    assert out["status"] == "post"


def test_no_match_anywhere_returns_no_score(monkeypatch):
    _clear()
    # Teams parse but appear on no probed scoreboard -> no score data, no crash.
    _mock_by_slug(monkeypatch, {})   # every slug returns empty events
    out = sports.fetch_sports("Wakanda FC vs Atlantis United")
    assert out["team1"] == "Wakanda FC"
    assert out["score"] is None


def test_keyword_narrows_probe_to_one_league(monkeypatch):
    _clear()
    calls = []

    def fake_get(url, *a, **k):
        calls.append(url)

        class R:
            def raise_for_status(self): pass
            def json(self): return _scoreboard("Lakers at Celtics", "LAL @ BOS",
                                               teams=("Celtics", "Lakers"))
        return R()

    monkeypatch.setattr(sports.httpx, "get", fake_get)
    out = sports.fetch_sports("Will the Lakers beat the Celtics in the NBA?")
    assert out["sport"] == "basketball/nba"
    # Only the NBA scoreboard slug was queried (keyword fast-path, no full probe).
    assert all("basketball/nba" in u for u in calls)


def test_fetch_sports_matches_real_fixture(monkeypatch):
    _clear()
    data = json.loads((FIX / "espn_scoreboard.json").read_text())
    # The fixture is an NBA game; serve it only under the NBA slug.
    _mock_by_slug(monkeypatch, {"basketball/nba": data})
    result = sports.fetch_sports("Knicks vs Spurs")
    assert result["sport"] == "basketball/nba"
    assert result["team1"] == "Knicks"
    assert result["score"] is not None
    assert result["status"] in ("pre", "in", "post")
    assert len(result["score"]) == 2


def test_fetch_sports_no_teams_returns_error():
    _clear()
    result = sports.fetch_sports("What is the weather today?")
    assert "error" in result


# --- secondary source: TheSportsDB fallback when ESPN has no match ---------

def _tsdb_event(home, away, hs, as_, status, sport="Soccer", league="EFL Cup"):
    return {"event": [{
        "strEvent": f"{home} vs {away}", "strSport": sport, "strLeague": league,
        "strHomeTeam": home, "strAwayTeam": away,
        "intHomeScore": str(hs), "intAwayScore": str(as_),
        "strStatus": status, "dateEvent": "2026-06-14"}]}


def test_thesportsdb_fallback_when_espn_empty(monkeypatch):
    _clear()
    payload = _tsdb_event("Arsenal", "Chelsea", 2, 1, "FT")

    def fake_get(url, *a, **k):
        # ESPN scoreboards return nothing; TheSportsDB returns the event.
        is_tsdb = "thesportsdb.com" in url
        body = payload if is_tsdb else {"events": []}

        class R:
            def raise_for_status(self): pass
            def json(self): return body
        return R()

    monkeypatch.setattr(sports.httpx, "get", fake_get)
    out = sports.fetch_sports("Will Arsenal beat Chelsea?")
    assert out["source"] == "thesportsdb"          # fell back to the 2nd source
    assert out["sport"] == "EFL Cup"
    assert out["status"] == "post"                 # 'FT' -> post
    assert out["combined_score"] == 3
    assert {c["team_name"] for c in out["score"]} == {"Arsenal", "Chelsea"}


def test_espn_preferred_over_thesportsdb(monkeypatch):
    _clear()

    def fake_get(url, *a, **k):
        if "thesportsdb.com" in url:
            raise AssertionError("TheSportsDB must not be queried when ESPN matched")
        body = _scoreboard("Arsenal vs Chelsea", "ARS v CHE", state="in",
                           scores=(1, 1), teams=("Arsenal", "Chelsea"))

        class R:
            def raise_for_status(self): pass
            def json(self): return body if "soccer/eng.1" in url else {"events": []}
        return R()

    monkeypatch.setattr(sports.httpx, "get", fake_get)
    out = sports.fetch_sports("Arsenal vs Chelsea")
    assert out["source"] == "espn"


def test_tsdb_state_mapping():
    assert sports._tsdb_state("FT") == "post"
    assert sports._tsdb_state("NS") == "pre"
    assert sports._tsdb_state("") == "pre"
    assert sports._tsdb_state("2H") == "in"
    assert sports._tsdb_state("Live") == "in"


def test_thesportsdb_never_raises_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tsdb down")
    monkeypatch.setattr(sports.httpx, "get", boom)
    assert sports._get_thesportsdb_data("Arsenal", "Chelsea") is None
