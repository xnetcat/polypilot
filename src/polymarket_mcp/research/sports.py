"""Sports research — ESPN scoreboard data for sports markets.

Ported from polydegen/research.py (sports helpers), stripping prompt-text
formatting and config/cache coupling.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from ._cache import _MISS, DEFAULT_TTL_SECONDS, cache_get, cache_set

log = logging.getLogger(__name__)

# Simple in-process cache keyed by question text, with a time-based TTL so a
# long-running loop re-pulls fresh ESPN scoreboards (see ._cache).
CACHE_TTL_SECONDS: float = DEFAULT_TTL_SECONDS
_CACHE: dict[str, tuple[float, dict | None]] = {}

# League NAME keywords -> ESPN scoreboard slug. A small, stable fast-path for
# when the question names the league explicitly (NOT team names — those are
# resolved dynamically against live scoreboards, see _ESPN_LEAGUES).
_ESPN_SPORTS = {
    "nba": "basketball/nba",
    "wnba": "basketball/wnba",
    "nfl": "football/nfl",
    "nhl": "hockey/nhl",
    "mlb": "baseball/mlb",
    "premier league": "soccer/eng.1",
    "epl": "soccer/eng.1",
    "la liga": "soccer/esp.1",
    "serie a": "soccer/ita.1",
    "bundesliga": "soccer/ger.1",
    "ligue 1": "soccer/fra.1",
    "eredivisie": "soccer/ned.1",
    "primeira liga": "soccer/por.1",
    "liga mx": "soccer/mex.1",
    "mls": "soccer/usa.1",
    "brasileirao": "soccer/bra.1",
    "brasileirão": "soccer/bra.1",
    "champions league": "soccer/uefa.champions",
    "ucl": "soccer/uefa.champions",
    "europa league": "soccer/uefa.europa",
    "saudi pro league": "soccer/ksa.1",
    "college football": "football/college-football",
    "college basketball": "basketball/mens-college-basketball",
}

# Ordered list of ESPN scoreboard slugs probed to resolve a game DYNAMICALLY by
# matching the question's teams against live scoreboards — no hardcoded team
# lists. Covers the US majors plus the major global soccer leagues; extend
# coverage by adding a slug here (a wrong/unknown slug just returns no events).
# Ordered most-likely-first so the common cases match early and stop the probe.
_ESPN_LEAGUES = [
    "baseball/mlb", "basketball/nba", "hockey/nhl", "football/nfl",
    "basketball/wnba",
    "soccer/eng.1", "soccer/esp.1", "soccer/ita.1", "soccer/ger.1",
    "soccer/fra.1", "soccer/usa.1", "soccer/mex.1",
    "soccer/uefa.champions", "soccer/uefa.europa",
    "soccer/ned.1", "soccer/por.1", "soccer/bra.1", "soccer/arg.1",
    "soccer/ksa.1", "soccer/eng.2",
    "football/college-football", "basketball/mens-college-basketball",
]

# Per-(slug, date) scoreboard cache so probing many leagues in a cycle — and
# re-checking the same game across cycles — doesn't re-hit ESPN every time.
_SCOREBOARD_CACHE: dict[str, tuple[float, list]] = {}


def _extract_date(question: str) -> str | None:
    """Extract date from question like 'April 6' -> '2026-04-06'."""
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    q = question.lower()
    for month_name, month_num in months.items():
        match = re.search(rf"{month_name}\s+(\d{{1,2}})", q)
        if match:
            day = int(match.group(1))
            year = datetime.now().year
            return f"{year}-{month_num:02d}-{day:02d}"
    return None


def _detect_sport(question: str) -> str | None:
    """Return an ESPN slug only when the question names the LEAGUE explicitly.

    Team→sport is intentionally NOT hardcoded — that is resolved dynamically by
    matching the question's teams against live scoreboards (see
    ``_get_espn_score_data``). This keyword fast-path just avoids probing every
    league when the league is named outright (e.g. "Premier League", "NBA")."""
    q = question.lower()
    # Longest keyword first so "college football" wins over a stray "football".
    for keyword in sorted(_ESPN_SPORTS, key=len, reverse=True):
        if keyword in q:
            return _ESPN_SPORTS[keyword]
    return None


# Trailing words that are noise for ESPN team matching: time references and
# generic game words that frequently trail a team name in market questions
# ("...beat the Celtics tonight", "Lakers game"). Kept deliberately small and
# conservative so real team names are never truncated.
_TEAM_TRAILING_NOISE = {
    "tonight",
    "today",
    "tomorrow",
    "game",
    "match",
}


def _normalize_team_name(name: str, *, strip_article: bool = True) -> str:
    """Normalize a raw extracted team name to reduce ESPN-matching noise.

    Conservatively trims obvious noise only:
      * strips trailing punctuation and surrounding whitespace,
      * collapses internal whitespace,
      * strips trailing time/filler words ("tonight", "today", "tomorrow",
        "game", "match"), possibly several in a row,
      * and (when ``strip_article`` is true) strips a leading article
        ("the Lakers" -> "Lakers").

    A clean "Team A vs Team B" name is returned unchanged. The trim is bounded
    to the known noise set, so multi-word team names ("Maple Leafs",
    "Trail Blazers") are preserved.

    ``strip_article`` is split out because the public extractor keeps the
    leading article in its returned display name (callers/tests rely on the raw
    "the Lakers" form), while the ESPN lookup uses ``strip_article=True`` so the
    6-char prefix match keys on the real team name rather than "the La".
    """
    # Collapse whitespace and strip surrounding punctuation/spaces first.
    cleaned = re.sub(r"\s+", " ", name).strip().strip(".,!?;:").strip()

    if strip_article:
        # Strip a single leading article.
        cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)

    # Peel trailing noise words (and any punctuation between them) one at a time.
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


def _extract_teams_from_question(question: str) -> tuple[str, str] | None:
    """Extract two team names from a sports question. Handles 'vs', 'beat', etc.

    Extracted names are run through ``_normalize_team_name`` to strip a leading
    article ("the Lakers" -> "Lakers"), trailing time/filler noise ("tonight",
    "game", ...), and surrounding punctuation that hurts ESPN matching, while
    leaving clean "Team A vs Team B" names intact.
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
    draw_match = re.search(r"(.+?)\s+vs\.?\s+(.+?):", question)
    if draw_match:
        return _norm(draw_match.group(1)), _norm(draw_match.group(2))
    return None


def _fetch_scoreboard(slug: str, date_str: str | None) -> list:
    """Fetch one ESPN scoreboard page for ``slug`` (optional ``YYYYMMDD`` date),
    cached per (slug, date) with the module TTL so probing many leagues in a
    cycle doesn't re-hit ESPN. Returns the events list ([] on any error)."""
    key = f"{slug}:{date_str or 'now'}"
    cached = cache_get(_SCOREBOARD_CACHE, key, CACHE_TTL_SECONDS)
    if cached is not _MISS:
        return cached
    url = f"https://site.api.espn.com/apis/site/v2/sports/{slug}/scoreboard"
    try:
        resp = httpx.get(url, params=({"dates": date_str} if date_str else {}), timeout=10)
        resp.raise_for_status()
        events = resp.json().get("events", []) or []
    except Exception:
        events = []
    cache_set(_SCOREBOARD_CACHE, key, events)
    return events


def _event_matches(event: dict, t1_lower: str, t2_lower: str) -> bool:
    """True when both teams (6-char prefix) appear in the event's name/shortName."""
    name = event.get("name", "").lower()
    short_name = event.get("shortName", "").lower()
    t1_match = t1_lower[:6] in name or t1_lower[:6] in short_name
    t2_match = not t2_lower or t2_lower[:6] in name or t2_lower[:6] in short_name
    return t1_match and t2_match


def _structure_event(event: dict, slug: str) -> dict:
    """Reduce a matched ESPN event into the score-data dict (state/detail/
    competitors/combined_score), tagged with the league ``slug`` it came from."""
    competition = event.get("competitions", [{}])[0]
    status_type = competition.get("status", {}).get("type", {})
    state = status_type.get("state", "pre")
    detail = status_type.get("detail", "")
    structured = []
    total = 0
    total_available = True
    for competitor in competition.get("competitors", []):
        try:
            score_val = int(competitor.get("score", "0"))
        except (TypeError, ValueError):
            score_val = 0
            total_available = False
        structured.append({
            "team_name": competitor.get("team", {}).get("displayName", "Unknown"),
            "score": score_val,
            "home_away": competitor.get("homeAway", ""),
        })
        total += score_val
    return {
        "sport": slug,
        "state": state,
        "detail": detail,
        "competitors": structured,
        "combined_score": total if total_available and state in ("post", "in") else None,
    }


def _get_espn_score_data(team1: str, team2: str, question: str) -> dict | None:
    """Resolve a game's ESPN score data by matching the teams against live
    scoreboards. If the league is named, probe just that one across a date
    window; otherwise probe ``_ESPN_LEAGUES`` (today + any explicit date) and
    return the first league whose scoreboard contains both teams — fully dynamic,
    no hardcoded team->sport map. Never raises.

    Strip the leading article for matching so "the Lakers" keys on "lakers".
    """
    t1_lower = _normalize_team_name(team1, strip_article=True).lower()
    if not t1_lower:
        return None
    t2_lower = _normalize_team_name(team2, strip_article=True).lower() if team2 else ""

    base = datetime.now(timezone.utc)
    target_date = _extract_date(question)
    target_str = target_date.replace("-", "") if target_date else None
    keyword_slug = _detect_sport(question)

    if keyword_slug:
        # League known: search it across today..+3 days (+ any explicit date).
        slugs = [keyword_slug]
        dates = [(base + timedelta(days=d)).strftime("%Y%m%d") for d in range(4)]
        if target_str and target_str not in dates:
            dates.insert(0, target_str)
    else:
        # League unknown: probe the league list, but keep it cheap — today only
        # (the live-games case), plus an explicit date when the question gives one.
        slugs = _ESPN_LEAGUES
        dates = [target_str] if target_str else [base.strftime("%Y%m%d")]

    try:
        for slug in slugs:
            for date_str in dates:
                for event in _fetch_scoreboard(slug, date_str):
                    if _event_matches(event, t1_lower, t2_lower):
                        return _structure_event(event, slug)
        return None
    except Exception as e:
        log.debug("[research] ESPN fetch error: %s", e)
        return None


def _format_espn_score_data(score_data: dict | None) -> str:
    if not score_data:
        return ""

    lines = []
    state = score_data.get("state", "pre")
    detail = score_data.get("detail", "")
    if state == "post":
        lines.append("GAME STATUS: FINAL (verified by ESPN)")
    elif state == "in":
        lines.append(f"GAME STATUS: LIVE — {detail}")
    else:
        lines.append(f"GAME STATUS: Scheduled — {detail}")

    for competitor in score_data.get("competitors", []):
        lines.append(
            f"  {competitor['team_name']} ({competitor['home_away']}): {competitor['score']}"
        )
    combined = score_data.get("combined_score")
    if combined is not None:
        lines.append(f"  Combined score: {combined}")
    return "\n".join(lines)


def _get_sports_research(question: str) -> dict | None:
    cached = cache_get(_CACHE, question, CACHE_TTL_SECONDS)
    if cached is not _MISS:
        return cached
    teams = _extract_teams_from_question(question)
    if not teams:
        log.debug("[research] Could not extract teams from: %s", question[:80])
        return None
    team1, team2 = teams
    score_data = _get_espn_score_data(team1, team2, question)
    # The slug comes from the matched scoreboard (dynamic) when a game was found,
    # else from an explicit league keyword if the question named one.
    sport_slug = (score_data or {}).get("sport") or _detect_sport(question)
    result = {
        "team1": team1,
        "team2": team2,
        "source": "espn",
        "source_url": (
            f"https://site.api.espn.com/apis/site/v2/sports/{sport_slug}/scoreboard"
            if sport_slug
            else ""
        ),
        "sport_slug": sport_slug,
        "espn_data": score_data,
        "espn_summary": _format_espn_score_data(score_data) if score_data else "",
    }
    cache_set(_CACHE, question, result)
    return result


def fetch_sports(question: str) -> dict:
    """ESPN-backed data for a sports market: detected sport, teams, game status,
    and live/last score.

    Returns {sport, team1, team2, status, score, source} or an {"error": ...}
    dict when teams can't be parsed.
    """
    research = _get_sports_research(question)
    if not research:
        return {"error": "could not extract teams from the question"}

    score_data = research.get("espn_data")
    status = score_data.get("state") if score_data else None
    score = score_data.get("competitors") if score_data else None
    return {
        "sport": research.get("sport_slug"),
        "team1": research.get("team1"),
        "team2": research.get("team2"),
        "status": status,
        "detail": score_data.get("detail") if score_data else None,
        "score": score,
        "combined_score": score_data.get("combined_score") if score_data else None,
        "summary": research.get("espn_summary", ""),
        "source": research.get("source", "espn"),
        "source_url": research.get("source_url", ""),
    }
