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
    # Trailing game qualifiers are peeled so the names match the data sources.
    assert esports._extract_teams("NAVI vs G2 in CS2") == ("NAVI", "G2")


def test_extract_teams_beat():
    # The "beat" parser stops the second team at " in " (mirrors sports.py), so
    # the trailing "in LoL" qualifier is dropped.
    assert esports._extract_teams("Will T1 beat Gen.G in LoL?") == ("T1", "Gen.G")


def test_extract_teams_none():
    assert esports._extract_teams("Who wins the tournament") is None


def test_extract_teams_strips_trailing_game_suffix():
    # Various trailing-game forms all reduce to the bare team names.
    assert esports._extract_teams("T1 vs Gen.G LoL") == ("T1", "Gen.G")
    assert esports._extract_teams("NAVI vs G2 CS2") == ("NAVI", "G2")
    assert esports._extract_teams("OG vs Liquid Dota 2") == ("OG", "Liquid")
    assert esports._extract_teams(
        "Sentinels vs Fnatic Valorant") == ("Sentinels", "Fnatic")
    assert esports._extract_teams(
        "Spirit vs Tundra league of legends") == ("Spirit", "Tundra")


def test_strip_game_suffix_leaves_clean_and_bare_names():
    # A clean team name is untouched; a bare game word is NOT stripped to empty.
    assert esports._strip_game_suffix("G2 Esports") == "G2 Esports"
    assert esports._strip_game_suffix("CS2") == "CS2"
    assert esports._strip_game_suffix("Cloud9") == "Cloud9"


# --- httpx routing helper -------------------------------------------------
# fetch_esports now hits TWO hosts concurrently. Tests route httpx.get by URL:
# PandaScore = "pandascore.co", TheSportsDB = "thesportsdb.com".


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _router(*, pandascore=None, thesportsdb=None, on_pandascore=None, on_thesportsdb=None):
    """Build an httpx.get replacement that dispatches by URL host.

    ``pandascore``/``thesportsdb`` give the JSON payload each host returns;
    ``on_pandascore``/``on_thesportsdb`` are optional callbacks(url, kwargs) for
    assertions (e.g. recording that a host was/was not hit).
    """
    def _fake_get(url, **kwargs):
        if "pandascore.co" in url:
            if on_pandascore:
                on_pandascore(url, kwargs)
            return _FakeResponse(pandascore if pandascore is not None else [])
        if "thesportsdb.com" in url:
            if on_thesportsdb:
                on_thesportsdb(url, kwargs)
            return _FakeResponse(
                thesportsdb if thesportsdb is not None else {"event": []}
            )
        raise AssertionError(f"unexpected URL: {url}")

    return _fake_get


# A TheSportsDB searchevents payload with the NAVI vs G2 esports event.
_TSDB_NAVI_G2 = {
    "event": [
        {
            "strEvent": "Natus Vincere vs G2 Esports",
            "strSport": "Esports",
            "strLeague": "ESL Pro League",
            "strHomeTeam": "Natus Vincere",
            "strAwayTeam": "G2 Esports",
            "intHomeScore": "2",
            "intAwayScore": "1",
            "strStatus": "Match Finished",
            "dateEvent": "2026-06-14",
        }
    ]
}


# --- No-key path: PandaScore skipped, TheSportsDB IS queried ---------------

def test_no_key_skips_pandascore_but_queries_thesportsdb(monkeypatch):
    # With no key: NO pandascore.co request, but TheSportsDB IS called and its
    # data is returned (the key behavior change).
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "", raising=False)

    hits = {"pandascore": 0, "thesportsdb": 0}

    def _on_ps(url, kwargs):
        hits["pandascore"] += 1
        raise AssertionError("pandascore.co must not be called without a key")

    def _on_tsdb(url, kwargs):
        hits["thesportsdb"] += 1

    monkeypatch.setattr(
        esports.httpx,
        "get",
        _router(thesportsdb=_TSDB_NAVI_G2, on_pandascore=_on_ps, on_thesportsdb=_on_tsdb),
    )

    result = esports.fetch_esports("Will Natus Vincere beat G2 in CS2?")

    assert hits["pandascore"] == 0          # PandaScore skipped (no key)
    assert hits["thesportsdb"] == 1         # TheSportsDB was queried
    assert result["game"] == "csgo"
    assert result["team1"] == "Natus Vincere"
    assert result["team2"] == "G2"
    # Result is TheSportsDB data, and the no-key PandaScore placeholder is recorded.
    assert result["source"] == "thesportsdb"
    assert result["status"] == "finished"
    assert result["sources"]["pandascore"]["error"] == "POLYPILOT_PANDASCORE_KEY not set"
    assert result["sources"]["thesportsdb"]["source"] == "thesportsdb"


def test_no_key_no_thesportsdb_match_returns_no_data(monkeypatch):
    # No key AND TheSportsDB has nothing -> a no-data dict steering to web search.
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "", raising=False)

    def _on_ps(url, kwargs):
        raise AssertionError("pandascore.co must not be called without a key")

    monkeypatch.setattr(
        esports.httpx,
        "get",
        _router(thesportsdb={"event": None}, on_pandascore=_on_ps),
    )

    result = esports.fetch_esports("NAVI vs G2 CS2")
    assert result["game"] == "csgo"
    assert result["error"] == "no esports match data found"
    assert "web search" in result["note"]
    assert result["sources"]["thesportsdb"] is None
    assert result["sources_agree"] is None


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


def test_with_key_parses_match(monkeypatch):
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)

    captured = {}

    def _on_ps(url, kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})

    # PandaScore returns the sample match; TheSportsDB is routed (empty) so the
    # concurrent call is satisfied without crashing.
    monkeypatch.setattr(
        esports.httpx,
        "get",
        _router(pandascore=_SAMPLE_MATCHES, thesportsdb={"event": []}, on_pandascore=_on_ps),
    )

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
    # PandaScore is the top-level source; TheSportsDB key is present (resolved None).
    assert result["source"] == "pandascore"
    assert result["source_url"] == "https://api.pandascore.co/csgo/matches"
    assert "thesportsdb" in result["sources"]
    assert result["sources"]["thesportsdb"] is None
    assert result["sources"]["pandascore"]["status"] == "running"
    # Only PandaScore resolved real data, so no cross-source agreement.
    assert result["sources_agree"] is None

    # The key was sent as a bearer token, and the right game endpoint was hit.
    assert captured["url"] == "https://api.pandascore.co/csgo/matches"
    assert captured["headers"].get("Authorization") == "Bearer fake-key-123"


def test_with_key_no_match_returns_no_data(monkeypatch):
    # PandaScore has no match and TheSportsDB has nothing -> no-data dict, but
    # PandaScore's {error} is recorded under sources and it's still top-level.
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)
    monkeypatch.setattr(
        esports.httpx,
        "get",
        _router(pandascore=_SAMPLE_MATCHES, thesportsdb={"event": []}),
    )

    result = esports.fetch_esports("Will Cloud9 beat Liquid in CS2?")
    assert result["game"] == "csgo"
    assert result["error"] == "no esports match data found"
    assert result["source"] == "pandascore"
    assert result["sources"]["pandascore"]["error"] == "no matching pandascore match found"
    assert result["sources"]["thesportsdb"] is None


def test_with_key_http_failure_returns_no_data(monkeypatch):
    # PandaScore HTTP failure + TheSportsDB empty -> no-data dict; PandaScore's
    # request-failed error is recorded under sources.
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)

    def _fake_get(url, **kwargs):
        if "pandascore.co" in url:
            raise RuntimeError("network down")
        return _FakeResponse({"event": []})

    monkeypatch.setattr(esports.httpx, "get", _fake_get)

    result = esports.fetch_esports("NAVI vs G2 CS2")
    assert result["game"] == "csgo"
    assert result["error"] == "no esports match data found"
    assert "pandascore request failed" in result["sources"]["pandascore"]["error"]


def test_with_key_prefers_pandascore_when_both_resolve(monkeypatch):
    # PandaScore AND TheSportsDB both resolve the NAVI vs G2 match -> PandaScore
    # is top-level, both appear under sources, and agreement is computed.
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)
    # PandaScore reports the match as finished 2-1 so statuses+scores agree with
    # the TheSportsDB fixture (Match Finished -> finished, 2 and 1).
    ps_finished = [{
        "id": 1, "status": "finished", "scheduled_at": "2026-06-14T18:00:00Z",
        "opponents": [{"opponent": {"id": 10, "name": "Natus Vincere"}},
                      {"opponent": {"id": 20, "name": "G2 Esports"}}],
        "results": [{"team_id": 10, "score": 2}, {"team_id": 20, "score": 1}],
    }]
    monkeypatch.setattr(
        esports.httpx,
        "get",
        _router(pandascore=ps_finished, thesportsdb=_TSDB_NAVI_G2),
    )

    result = esports.fetch_esports("Will Natus Vincere beat G2 in CS2?")
    assert result["source"] == "pandascore"
    assert result["sources"]["pandascore"]["status"] == "finished"
    assert result["sources"]["thesportsdb"]["status"] == "finished"
    assert result["agreement"] == {"status": True, "score": True}
    assert result["sources_agree"] is True


# --- Query is split by status with opposite sorts (regression guard) ------
# A single begin_at-ASCENDING query that includes `finished` surfaces the OLDEST
# matches in PandaScore's history and never reaches current ones. The lookup must
# instead issue TWO pages: upcoming/running soonest-first AND finished newest-first.

def test_query_splits_by_status_and_sort(monkeypatch):
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)
    calls = []

    def _fake_get(url, **kwargs):
        if "thesportsdb.com" in url:
            return _FakeResponse({"event": []})
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


# --- TheSportsDB unit + neither-resolves coverage -------------------------

def test_thesportsdb_filters_non_esports(monkeypatch):
    # A same-named football event must be skipped; with only that event,
    # _query_thesportsdb_esports returns None.
    football_only = {
        "event": [
            {
                "strEvent": "Natus Vincere vs G2 Esports",
                "strSport": "Soccer",
                "strLeague": "Some Football League",
                "strHomeTeam": "Natus Vincere",
                "strAwayTeam": "G2 Esports",
                "intHomeScore": "1", "intAwayScore": "0",
                "strStatus": "Match Finished", "dateEvent": "2026-06-14",
            }
        ]
    }
    monkeypatch.setattr(
        esports.httpx, "get", _router(thesportsdb=football_only)
    )
    assert esports._query_thesportsdb_esports("Natus Vincere", "G2 Esports") is None


def test_thesportsdb_returns_esports_event(monkeypatch):
    monkeypatch.setattr(
        esports.httpx, "get", _router(thesportsdb=_TSDB_NAVI_G2)
    )
    sd = esports._query_thesportsdb_esports("Natus Vincere", "G2 Esports")
    assert sd is not None
    assert sd["source"] == "thesportsdb"
    assert sd["status"] == "finished"
    assert sd["league"] == "ESL Pro League"
    assert sd["results"] == [
        {"team_name": "Natus Vincere", "score": 2},
        {"team_name": "G2 Esports", "score": 1},
    ]


def test_neither_source_resolves_returns_no_data(monkeypatch):
    # With a key but neither source matching -> no-data dict steering to web search.
    monkeypatch.setattr(esports.config, "PANDASCORE_KEY", "fake-key-123", raising=False)
    monkeypatch.setattr(
        esports.httpx, "get", _router(pandascore=[], thesportsdb={"event": None})
    )

    result = esports.fetch_esports("Will Cloud9 beat Liquid in CS2?")
    assert result["error"] == "no esports match data found"
    assert "web search" in result["note"]
    assert result["sources"]["thesportsdb"] is None
    assert result["agreement"] is None
    assert result["sources_agree"] is None


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
