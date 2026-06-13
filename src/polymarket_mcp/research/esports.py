"""Esports research — PandaScore match data for esports markets.

Best-effort, never-raises lookup for esports markets (CS2/CS:GO, Dota 2,
League of Legends, Valorant, Overwatch, Rocket League). Detects the game and
the two teams from the question text, then — when a PandaScore API key is
configured (``config.PANDASCORE_KEY``) — queries the PandaScore matches API for
an upcoming/running/finished match involving those teams.

Self-contained by design: it replicates a small "A vs B" / "A beat B" team
extractor (modelled on ``research/sports.py``) rather than importing from
sports.py, so the two modules stay independent. When no key is set the module
returns an error dict that tells the caller to fall back to web search; it
never performs network I/O without a key.
"""
from __future__ import annotations

import logging
import re

import httpx

from .. import config

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


def fetch_esports(question: str) -> dict:
    """PandaScore-backed data for an esports market: game, teams, match status.

    Best-effort and never raises. Returns one of:
      * ``{"error": "no esports game detected", "question": ...}`` when no
        supported game keyword is found,
      * ``{game, team1, team2, error: "POLYPILOT_PANDASCORE_KEY not set",
        source: "pandascore"}`` when no API key is configured (no network call —
        the caller should fall back to web search),
      * ``{game, team1, team2, status, scheduled_at, opponents, results?,
        source, source_url}`` on a successful PandaScore lookup,
      * an ``{"error": ...}`` dict on HTTP failure or no match found.
    """
    game = _detect_game(question)
    if not game:
        return {"error": "no esports game detected", "question": question}

    teams = _extract_teams(question)
    if teams:
        team1, team2 = teams
    else:
        team1, team2 = "", ""

    if not config.PANDASCORE_KEY:
        return {
            "game": game,
            "team1": team1,
            "team2": team2,
            "error": "POLYPILOT_PANDASCORE_KEY not set",
            "source": "pandascore",
        }

    return _query_pandascore(game, team1, team2)
