"""Esports research — cross-source match data for esports markets.

Best-effort, never-raises lookup for esports markets (CS2/CS:GO, Dota 2,
League of Legends, Valorant, Overwatch, Rocket League). Detects the game and
the two teams from the question text, then queries TWO free data sources
concurrently for a match involving those teams:

  * PandaScore (``https://api.pandascore.co``) — only when an API key is
    configured (``config.PANDASCORE_KEY``); rich esports coverage.
  * TheSportsDB (``https://www.thesportsdb.com``) — keyless (free test key
    "3"); covers many esports leagues and works with NO PandaScore key.

Querying both gives the agent cross-source confidence when both resolve, and —
crucially — esports research now returns real data even without a PandaScore
key (TheSportsDB supplies it) instead of a bare "key not set" error.

Self-contained by design: it replicates a small "A vs B" / "A beat B" team
extractor (modelled on ``research/sports.py``) rather than importing from
sports.py, so the two modules stay independent.
"""
from __future__ import annotations

import logging
import re

import httpx

from .. import config
from ._concurrent import gather

log = logging.getLogger(__name__)

# Map of question keywords -> PandaScore videogame slug (the path segment used
# in ``https://api.pandascore.co/{slug}/matches``). Longer/more specific
# keywords are checked first so "counter-strike" wins before a stray substring.
_GAME_KEYWORDS: list[tuple[str, str]] = [
    ("counter-strike", "csgo"),
    ("counter strike", "csgo"),
    ("cs:go", "csgo"),
    ("csgo", "csgo"),
    ("cs2", "csgo"),
    ("dota 2", "dota2"),
    ("dota2", "dota2"),
    ("dota", "dota2"),
    ("league of legends", "lol"),
    ("lol", "lol"),
    ("valorant", "valorant"),
    ("overwatch", "ow"),
    ("rocket league", "rl"),
]


def _detect_game(question: str) -> str | None:
    """Return the PandaScore videogame slug detected in ``question``, or None.

    Matching is case-insensitive substring matching against ``_GAME_KEYWORDS``,
    which is ordered so multi-word/specific keywords ("counter-strike",
    "league of legends", "dota 2") are tested before their shorter aliases.
    """
    q = question.lower()
    for keyword, slug in _GAME_KEYWORDS:
        if keyword in q:
            return slug
    return None


# Trailing words that are noise for team matching: time references and generic
# game words that frequently trail a team name in market questions
# ("...beat NAVI tonight", "G2 match"). Kept deliberately small so real team
# names are never truncated.
_TEAM_TRAILING_NOISE = {"tonight", "today", "tomorrow", "game", "match", "series"}


def _normalize_team_name(name: str, *, strip_article: bool = True) -> str:
    """Conservatively normalize a raw extracted team name.

    Strips surrounding punctuation/whitespace, collapses internal whitespace,
    peels trailing time/filler words, and (optionally) a single leading article.
    A clean "Team A vs Team B" name is returned unchanged.
    """
    cleaned = re.sub(r"\s+", " ", name).strip().strip(".,!?;:").strip()

    if strip_article:
        cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)

    while True:
        tokens = cleaned.split()
        if not tokens:
            break
        last = tokens[-1].strip(".,!?;:").lower()
        if last in _TEAM_TRAILING_NOISE:
            cleaned = " ".join(tokens[:-1]).strip().strip(".,!?;:").strip()
            continue
        break

    return cleaned


def _extract_teams(question: str) -> tuple[str, str] | None:
    """Extract two team names from an esports question.

    Handles "A vs B" / "A vs. B" and "Will A beat/defeat B" phrasings, mirroring
    the simple parser in ``research/sports.py``. Returns ``(team1, team2)`` with
    ``team2`` possibly empty for "Will A win" phrasings, or None when no team
    pair can be parsed.
    """
    def _norm(value: str) -> str:
        return _normalize_team_name(value, strip_article=True)

    vs_match = re.search(r"(.+?)\s+vs\.?\s+(.+?)(?::|$|\?)", question)
    if vs_match:
        return _norm(vs_match.group(1)), _norm(vs_match.group(2))
    beat_match = re.search(
        r"[Ww]ill\s+(.+?)\s+(?:beat|defeat|win against)\s+(.+?)(?:\?|$|:| in )", question
    )
    if beat_match:
        return _norm(beat_match.group(1)), _norm(beat_match.group(2))
    win_match = re.search(r"[Ww]ill\s+(.+?)\s+win\b", question)
    if win_match:
        return _norm(win_match.group(1)), ""
    return None


def _opponent_names(match: dict) -> list[str]:
    """Pull opponent (team) display names out of a PandaScore match dict.

    PandaScore nests opponents as ``[{"opponent": {"name": ...}}, ...]``. Parsed
    defensively: missing/odd shapes yield an empty list rather than raising.
    """
    names: list[str] = []
    for entry in match.get("opponents", []) or []:
        if not isinstance(entry, dict):
            continue
        opp = entry.get("opponent")
        if isinstance(opp, dict):
            name = opp.get("name") or opp.get("acronym")
            if name:
                names.append(str(name))
    return names


def _result_scores(match: dict) -> list[dict] | None:
    """Pull per-team result scores out of a PandaScore match dict, if present.

    Returns a list of ``{"team_id": ..., "score": ...}`` dicts or None when the
    match has no results yet (upcoming match). Parsed defensively.
    """
    results = match.get("results")
    if not isinstance(results, list) or not results:
        return None
    parsed: list[dict] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        parsed.append(
            {
                "team_id": entry.get("team_id"),
                "score": entry.get("score"),
            }
        )
    return parsed or None


def _match_involves(match: dict, t1_lower: str, t2_lower: str) -> bool:
    """True when both wanted teams appear among the match's opponent names.

    Uses a 4-char case-insensitive prefix substring test in either direction so
    minor naming differences ("NAVI" vs "Natus Vincere", "G2" vs "G2 Esports")
    still match. When ``t2_lower`` is empty only ``t1`` must be present.
    """
    opp_names = [n.lower() for n in _opponent_names(match)]
    blob = " ".join(opp_names)

    def _present(needle: str) -> bool:
        if not needle:
            return True
        key = needle[:4]
        return key in blob or any(name[:4] in needle for name in opp_names if name)

    return _present(t1_lower) and _present(t2_lower)


def _fetch_matches(game: str, status: str, sort: str) -> list:
    """Fetch one page of matches for ``game``/``status`` in ``sort`` order.

    Returns a list of match dicts (empty on an unexpected payload shape). Raises
    on HTTP / decode failure — the caller (``_query_pandascore``) wraps the whole
    lookup in try/except so nothing propagates out of the module.
    """
    resp = httpx.get(
        f"https://api.pandascore.co/{game}/matches",
        params={"sort": sort, "filter[status]": status, "per_page": 50},
        headers={
            "Authorization": f"Bearer {config.PANDASCORE_KEY}",
            "Accept": "application/json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("matches", []) or []
    return []


def _query_pandascore(game: str, team1: str, team2: str) -> dict:
    """Query PandaScore for a match involving the teams. Never raises.

    Returns the parsed match dict (status/scheduled_at/opponents/results) on a
    hit, or an ``{"error": ...}`` dict on HTTP failure / no match found. All
    network I/O is wrapped in try/except.
    """
    source_url = f"https://api.pandascore.co/{game}/matches"
    try:
        # Two opposite-sorted pages, then search upcoming first. A single
        # begin_at-ASCENDING query that includes `finished` surfaces the OLDEST
        # matches in PandaScore's history (years ago) and never reaches current
        # ones within a page, so split by status: upcoming/running soonest-first,
        # then most-recently-finished.
        upcoming = _fetch_matches(game, "not_started,running", "begin_at")
        recent = _fetch_matches(game, "finished", "-begin_at")
    except Exception as e:  # noqa: BLE001 — best-effort, never propagate.
        log.debug("[research] PandaScore fetch error: %s", e)
        return {
            "game": game,
            "team1": team1,
            "team2": team2,
            "error": f"pandascore request failed: {e}",
            "source": "pandascore",
            "source_url": source_url,
        }

    matches = list(upcoming) + list(recent)

    t1_lower = team1.lower()
    t2_lower = team2.lower()
    chosen: dict | None = None
    for match in matches:
        if not isinstance(match, dict):
            continue
        if _match_involves(match, t1_lower, t2_lower):
            chosen = match
            break

    if chosen is None:
        return {
            "game": game,
            "team1": team1,
            "team2": team2,
            "error": "no matching pandascore match found",
            "source": "pandascore",
            "source_url": source_url,
        }

    result: dict = {
        "game": game,
        "team1": team1,
        "team2": team2,
        "status": chosen.get("status"),
        "scheduled_at": chosen.get("scheduled_at") or chosen.get("begin_at"),
        "opponents": _opponent_names(chosen),
        "source": "pandascore",
        "source_url": source_url,
    }
    scores = _result_scores(chosen)
    if scores is not None:
        result["results"] = scores
    return result


# --- TheSportsDB (keyless secondary source) -------------------------------
# TheSportsDB's free test key ("3") gives keyless access to ``searchevents``,
# which covers many esports leagues. It's the fallback that makes esports
# research work with NO PandaScore key, and the cross-check that gives
# confidence when PandaScore IS configured.
_TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"

# Strings that mark an event's ``strSport`` as esports (lowercased). Anything
# else (football, basketball, ...) is filtered out — searchevents can return
# same-named traditional-sports events.
_TSDB_ESPORTS_SPORTS = {"esports", "e-sports"}


def _tsdb_status(status: str) -> str:
    """Map a TheSportsDB ``strStatus`` to our status vocabulary.

    "FT"/"Match Finished"/"Final" -> "finished"; ""/"NS"/"Not Started" ->
    "scheduled"; anything else (1h/live/...) -> "live".
    """
    s = (status or "").strip().lower()
    if s in ("ft", "match finished", "final"):
        return "finished"
    if s in ("", "ns", "not started"):
        return "scheduled"
    return "live"


def _tsdb_involves(home: str, away: str, t1_lower: str, t2_lower: str) -> bool:
    """True when both wanted teams appear among an event's home/away teams.

    Mirrors ``_match_involves``: normalizes the event team names and uses a
    short (4-char) case-insensitive prefix test in either direction so minor
    naming differences still match. When ``t2_lower`` is empty only ``t1`` must
    be present.
    """
    names = [
        _normalize_team_name(home).lower(),
        _normalize_team_name(away).lower(),
    ]
    blob = " ".join(n for n in names if n)

    def _present(needle: str) -> bool:
        if not needle:
            return True
        key = needle[:4]
        return key in blob or any(name[:4] in needle for name in names if name)

    return _present(t1_lower) and _present(t2_lower)


def _query_thesportsdb_esports(team1: str, team2: str) -> dict | None:
    """Query TheSportsDB (keyless) for an esports event involving the teams.

    Returns ``{game, league, team1, team2, status, opponents, results?,
    source: "thesportsdb", source_url}`` for the first matching esports event,
    or None on no match / error / no teams. Never raises (network is wrapped in
    try/except). Non-esports events (``strSport`` not in esports) are skipped.
    """
    if not team1:
        return None

    source_url = f"{_TSDB_BASE}/searchevents.php"
    query = (f"{team1}_vs_{team2}" if team2 else team1).replace(" ", "_")
    try:
        resp = httpx.get(source_url, params={"e": query}, timeout=10)
        resp.raise_for_status()
        events = resp.json().get("event") or []
    except Exception as e:  # noqa: BLE001 — best-effort, never propagate.
        log.debug("[research] TheSportsDB esports error: %s", e)
        return None

    t1_lower = team1.lower()
    t2_lower = team2.lower()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        # Keep only esports events; searchevents can match same-named teams in
        # traditional sports.
        if (ev.get("strSport") or "").strip().lower() not in _TSDB_ESPORTS_SPORTS:
            continue
        home = ev.get("strHomeTeam") or ""
        away = ev.get("strAwayTeam") or ""
        if not _tsdb_involves(home, away, t1_lower, t2_lower):
            continue

        def _score(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        league = ev.get("strLeague") or ev.get("strSport") or "thesportsdb"
        result: dict = {
            "game": league,
            "league": league,
            "team1": team1,
            "team2": team2,
            "status": _tsdb_status(ev.get("strStatus")),
            "scheduled_at": ev.get("dateEvent"),
            "opponents": [n for n in (home, away) if n],
            "source": "thesportsdb",
            "source_url": source_url,
        }
        hs, as_ = _score(ev.get("intHomeScore")), _score(ev.get("intAwayScore"))
        if hs is not None or as_ is not None:
            result["results"] = [
                {"team_name": home, "score": hs},
                {"team_name": away, "score": as_},
            ]
        return result
    return None


def _has_match_data(result: dict | None) -> bool:
    """True when a source result is real match data (a ``status``, not an error)."""
    return bool(result) and "status" in result and "error" not in result


def fetch_esports(question: str) -> dict:
    """Cross-source data for an esports market: game, teams, match status.

    Best-effort and never raises. Queries PandaScore (only when a key is set)
    and TheSportsDB (always, keyless) concurrently. Returns one of:
      * ``{"error": "no esports game detected", "question": ...}`` when no
        supported game keyword is found (NO network call),
      * a match dict ``{game, team1, team2, status, ..., source, sources,
        agreement, sources_agree}`` — top-level fields come from PandaScore when
        it returned real data, else from TheSportsDB,
      * a no-data dict ``{game, team1, team2, error, note, source, sources, ...}``
        telling the caller to web-search when NEITHER source resolves.

    ``sources`` always carries the per-source result: ``{"pandascore": <result
    or {error}>, "thesportsdb": <result or None>}``. ``agreement`` /
    ``sources_agree`` compare the two sources' status (and scores when both
    report them) and are None unless BOTH resolved real match data.
    """
    game = _detect_game(question)
    if not game:
        return {"error": "no esports game detected", "question": question}

    teams = _extract_teams(question)
    if teams:
        team1, team2 = teams
    else:
        team1, team2 = "", ""

    # Fetch both free sources concurrently: PandaScore only with a key (else a
    # skipped placeholder, no network call), TheSportsDB always (keyless). The
    # slowest single source bounds the wall-clock; gather never raises.
    has_key = bool(config.PANDASCORE_KEY)
    ps_placeholder = {
        "game": game,
        "team1": team1,
        "team2": team2,
        "error": "POLYPILOT_PANDASCORE_KEY not set",
        "source": "pandascore",
    }
    pandascore_sd, thesportsdb_sd = gather(
        (lambda: _query_pandascore(game, team1, team2)) if has_key
        else (lambda: ps_placeholder),
        lambda: _query_thesportsdb_esports(team1, team2),
    )
    # gather returns None on failure; the PandaScore slot always has a dict
    # (real data, an {error} dict, or the no-key placeholder).
    if pandascore_sd is None:
        pandascore_sd = ps_placeholder

    sources = {"pandascore": pandascore_sd, "thesportsdb": thesportsdb_sd}

    # Cross-source agreement is only meaningful when BOTH resolved real data.
    ps_ok = _has_match_data(pandascore_sd)
    tsdb_ok = _has_match_data(thesportsdb_sd)
    if ps_ok and tsdb_ok:
        status_agree = pandascore_sd.get("status") == thesportsdb_sd.get("status")
        ps_scores = _result_scores(pandascore_sd)
        tsdb_scores = thesportsdb_sd.get("results")
        # Compare scores only when both sides report them; a missing score on
        # either side doesn't count as a conflict.
        if ps_scores and tsdb_scores:
            ps_vals = sorted(s.get("score") for s in ps_scores)
            tsdb_vals = sorted(s.get("score") for s in tsdb_scores)
            score_agree = ps_vals == tsdb_vals
        else:
            score_agree = None
        agreement = {"status": status_agree, "score": score_agree}
        sources_agree = status_agree and (score_agree if score_agree is not None else True)
    else:
        agreement = None
        sources_agree = None

    # Top-level result prefers PandaScore when it returned real match data, else
    # TheSportsDB. ``source`` names whichever supplied the top-level fields.
    if ps_ok:
        result = dict(pandascore_sd)
    elif tsdb_ok:
        result = dict(thesportsdb_sd)
    else:
        # Neither source resolved: a no-data dict steering the agent to web search.
        # The key win over the old code: with no PandaScore key, TheSportsDB was
        # still queried above — this only fires when it ALSO had nothing.
        result = {
            "game": game,
            "team1": team1,
            "team2": team2,
            "error": "no esports match data found",
            "note": "no match found in PandaScore/TheSportsDB; fall back to web search",
            "source": "thesportsdb" if not has_key else "pandascore",
        }

    # Always keep the detected game slug + team identity and the cross-source
    # bundle on the result. (TheSportsDB data carries its own ``league`` name
    # alongside, but ``game`` stays the detected slug for a consistent shape.)
    result["game"] = game
    result["team1"] = team1
    result["team2"] = team2
    result["sources"] = sources
    result["agreement"] = agreement
    result["sources_agree"] = sources_agree
    return result
