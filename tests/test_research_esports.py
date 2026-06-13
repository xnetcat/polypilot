import polymarket_mcp.research.esports as esports


# --- Game detection -------------------------------------------------------

def test_detect_game_cs2():
    assert esports._detect_game("Will NAVI win CS2 major?") == "csgo"


def test_detect_game_csgo_variants():
    assert esports._detect_game("CS:GO grand final") == "csgo"
    assert esports._detect_game("Counter-Strike major winner") == "csgo"


def test_detect_game_dota():
    assert esports._detect_game("Team Spirit vs Tundra Dota 2") == "dota2"
    assert esports._detect_game("Will OG win the Dota International?") == "dota2"


def test_detect_game_lol():
    assert esports._detect_game("Will T1 win League of Legends Worlds?") == "lol"
    assert esports._detect_game("G2 vs FNC LoL") == "lol"


def test_detect_game_valorant():
    assert esports._detect_game("Sentinels vs Fnatic Valorant Champions") == "valorant"


def test_detect_game_overwatch_and_rl():
    assert esports._detect_game("Overwatch League final") == "ow"
    assert esports._detect_game("Rocket League world championship") == "rl"


def test_non_esports_question_returns_no_game():
    result = esports.fetch_esports("What is the weather in Paris tomorrow?")
    assert result == {
        "error": "no esports game detected",
        "question": "What is the weather in Paris tomorrow?",
    }


# --- Team extraction ------------------------------------------------------

def test_extract_teams_vs():
    assert esports._extract_teams("NAVI vs G2 in CS2") == ("NAVI", "G2 in CS2")


def test_extract_teams_beat():
    # The "beat" parser stops the second team at " in " (mirrors sports.py), so
    # the trailing "in LoL" qualifier is dropped.
    assert esports._extract_teams("Will T1 beat Gen.G in LoL?") == ("T1", "Gen.G")


def test_extract_teams_none():
    assert esports._extract_teams("Who wins the tournament") is None


# --- No-key path: never touches the network -------------------------------

def test_no_key_returns_error_and_skips_network(monkeypatch):
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("httpx.get must not be called without an API key")

    monkeypatch.setattr(esports.httpx, "get", _boom)

    result = esports.fetch_esports("NAVI vs G2 CS2")
    assert result == {
        "game": "csgo",
        "team1": "NAVI",
        "team2": "G2 CS2",
        "error": "POLYPILOT_PANDASCORE_KEY not set",
        "source": "pandascore",
    }


# --- With key: parse a sample PandaScore payload --------------------------

_SAMPLE_MATCHES = [
    {
        "id": 1,
        "status": "running",
        "scheduled_at": "2026-06-14T18:00:00Z",
        "begin_at": "2026-06-14T18:00:00Z",
        "opponents": [
            {"opponent": {"id": 10, "name": "Natus Vincere", "acronym": "NAVI"}},
            {"opponent": {"id": 20, "name": "G2 Esports", "acronym": "G2"}},
        ],
        "results": [
            {"team_id": 10, "score": 1},
            {"team_id": 20, "score": 0},
        ],
    },
    {
        "id": 2,
        "status": "not_started",
        "scheduled_at": "2026-06-15T18:00:00Z",
        "opponents": [
            {"opponent": {"id": 30, "name": "FaZe Clan", "acronym": "FaZe"}},
            {"opponent": {"id": 40, "name": "Vitality", "acronym": "VIT"}},
        ],
        "results": [],
    },
]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.last = None

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_with_key_parses_match(monkeypatch):
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)

    captured = {}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return _FakeResponse(_SAMPLE_MATCHES)

    monkeypatch.setattr(esports.httpx, "get", _fake_get)

    result = esports.fetch_esports("Will Natus Vincere beat G2 in CS2?")

    assert result["game"] == "csgo"
    assert result["team1"] == "Natus Vincere"
    assert result["status"] == "running"
    assert result["scheduled_at"] == "2026-06-14T18:00:00Z"
    assert "Natus Vincere" in result["opponents"]
    assert "G2 Esports" in result["opponents"]
    assert result["results"] == [
        {"team_id": 10, "score": 1},
        {"team_id": 20, "score": 0},
    ]
    assert result["source"] == "pandascore"
    assert result["source_url"] == "https://api.pandascore.co/csgo/matches"

    # The key was sent as a bearer token, and the right game endpoint was hit.
    assert captured["url"] == "https://api.pandascore.co/csgo/matches"
    assert captured["headers"].get("Authorization") == "Bearer fake-key-123"


def test_with_key_no_match_returns_error(monkeypatch):
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)
    monkeypatch.setattr(
        esports.httpx, "get", lambda *a, **k: _FakeResponse(_SAMPLE_MATCHES)
    )

    result = esports.fetch_esports("Will Cloud9 beat Liquid in CS2?")
    assert result["game"] == "csgo"
    assert result["error"] == "no matching pandascore match found"
    assert result["source"] == "pandascore"


def test_with_key_http_failure_returns_error(monkeypatch):
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)

    def _raise(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(esports.httpx, "get", _raise)

    result = esports.fetch_esports("NAVI vs G2 CS2")
    assert result["game"] == "csgo"
    assert "pandascore request failed" in result["error"]
    assert result["source"] == "pandascore"


# --- Query is split by status with opposite sorts (regression guard) ------
# A single begin_at-ASCENDING query that includes `finished` surfaces the OLDEST
# matches in PandaScore's history and never reaches current ones. The lookup must
# instead issue TWO pages: upcoming/running soonest-first AND finished newest-first.

def test_query_splits_by_status_and_sort(monkeypatch):
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)
    calls = []

    def _fake_get(url, **kwargs):
        params = kwargs.get("params", {})
        calls.append((params.get("filter[status]"), params.get("sort")))
        # Only the "finished" page carries the wanted match, proving recently
        # finished matches are searched too (not just upcoming).
        if params.get("filter[status]") == "finished":
            return _FakeResponse(_SAMPLE_MATCHES)
        return _FakeResponse([])

    monkeypatch.setattr(esports.httpx, "get", _fake_get)

    result = esports.fetch_esports("Will Natus Vincere beat G2 in CS2?")
    assert result["status"] == "running"          # match found on the finished page
    assert ("not_started,running", "begin_at") in calls   # upcoming, soonest-first
    assert ("finished", "-begin_at") in calls             # finished, newest-first
    assert len(calls) == 2


def test_query_prefers_upcoming_over_finished(monkeypatch):
    # When BOTH pages contain a wanted match, the upcoming/running one wins
    # (upcoming is searched first), so the trader sees the live/next game.
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)

    upcoming = [{
        "id": 99, "status": "not_started", "scheduled_at": "2026-07-01T18:00:00Z",
        "opponents": [{"opponent": {"id": 10, "name": "Natus Vincere"}},
                      {"opponent": {"id": 20, "name": "G2 Esports"}}],
        "results": [],
    }]
    finished = [{
        "id": 1, "status": "finished", "scheduled_at": "2026-06-01T18:00:00Z",
        "opponents": [{"opponent": {"id": 10, "name": "Natus Vincere"}},
                      {"opponent": {"id": 20, "name": "G2 Esports"}}],
        "results": [{"team_id": 10, "score": 2}, {"team_id": 20, "score": 1}],
    }]

    def _fake_get(url, **kwargs):
        status = kwargs.get("params", {}).get("filter[status]")
        return _FakeResponse(upcoming if status == "not_started,running" else finished)

    monkeypatch.setattr(esports.httpx, "get", _fake_get)
    result = esports.fetch_esports("Will Natus Vincere beat G2 in CS2?")
    assert result["status"] == "not_started"
    assert result["scheduled_at"] == "2026-07-01T18:00:00Z"
