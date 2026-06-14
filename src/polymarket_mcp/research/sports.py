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

_ESPN_SPORTS = {
    "nba": "basketball/nba",
    "nfl": "football/nfl",
    "nhl": "hockey/nhl",
    "mlb": "baseball/mlb",
    "premier league": "soccer/eng.1",
    "la liga": "soccer/esp.1",
    "serie a": "soccer/ita.1",
    "bundesliga": "soccer/ger.1",
    "champions league": "soccer/uefa.champions",
    "mls": "soccer/usa.1",
}


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
    """Detect which ESPN sport endpoint to use from the question."""
    q = question.lower()
    for keyword, slug in _ESPN_SPORTS.items():
        if keyword in q:
            return slug
    nba_teams = ["lakers", "celtics", "warriors", "nuggets", "bucks", "suns", "knicks",
                 "heat", "76ers", "cavaliers", "thunder", "clippers", "nets", "bulls",
                 "hawks", "mavericks", "grizzlies", "timberwolves", "pelicans", "kings",
                 "trail blazers", "blazers", "rockets", "spurs", "magic", "pacers",
                 "hornets", "pistons", "wizards", "raptors", "jazz"]
    nhl_teams = ["bruins", "rangers", "maple leafs", "oilers", "avalanche", "panthers",
                 "hurricanes", "stars", "jets", "wild", "lightning", "devils", "islanders",
                 "penguins", "capitals", "senators", "flames", "canucks", "predators",
                 "golden knights", "blues", "kraken", "flyers", "red wings", "blackhawks",
                 "sabres", "coyotes", "sharks", "ducks", "canadiens", "blue jackets"]
    # MLB / NFL nicknames. Names that collide across sports — "giants" and
    # "cardinals" (MLB vs NFL), "rangers"/"jets"/"panthers" (vs NHL) — are
    # deliberately OMITTED so they don't mis-route; rely on an explicit league
    # keyword for those. Covers the common, unambiguous teams.
    mlb_teams = ["rays", "angels", "yankees", "red sox", "blue jays", "orioles",
                 "astros", "athletics", "mariners", "twins", "white sox",
                 "guardians", "royals", "tigers", "dodgers", "padres",
                 "diamondbacks", "dbacks", "rockies", "brewers", "reds", "cubs",
                 "pirates", "phillies", "mets", "braves", "nationals", "marlins"]
    nfl_teams = ["chiefs", "eagles", "49ers", "niners", "cowboys", "bills",
                 "dolphins", "patriots", "ravens", "bengals", "browns",
                 "steelers", "colts", "jaguars", "titans", "broncos", "chargers",
                 "raiders", "packers", "vikings", "lions", "buccaneers", "saints",
                 "falcons", "seahawks", "commanders", "texans", "bears"]
    if any(t in q for t in nba_teams):
        return "basketball/nba"
    if any(t in q for t in nhl_teams):
        return "hockey/nhl"
    if any(t in q for t in mlb_teams):
        return "baseball/mlb"
    if any(t in q for t in nfl_teams):
        return "football/nfl"
    # Soccer — map teams to their actual league
    _soccer_league_map = {
        "arsenal": "soccer/eng.1", "liverpool": "soccer/eng.1", "man city": "soccer/eng.1",
        "chelsea": "soccer/eng.1", "tottenham": "soccer/eng.1", "manchester united": "soccer/eng.1",
        "manchester city": "soccer/eng.1", "aston villa": "soccer/eng.1", "newcastle": "soccer/eng.1",
        "barcelona": "soccer/esp.1", "real madrid": "soccer/esp.1", "atletico": "soccer/esp.1",
        "sevilla": "soccer/esp.1", "villarreal": "soccer/esp.1",
        "bayern": "soccer/ger.1", "dortmund": "soccer/ger.1", "leverkusen": "soccer/ger.1",
        "leipzig": "soccer/ger.1",
        "juventus": "soccer/ita.1", "inter milan": "soccer/ita.1", "ac milan": "soccer/ita.1",
        "napoli": "soccer/ita.1", "roma": "soccer/ita.1", "lazio": "soccer/ita.1",
        "psg": "soccer/fra.1", "marseille": "soccer/fra.1", "lyon": "soccer/fra.1",
        "sporting cp": "soccer/por.1", "benfica": "soccer/por.1", "porto": "soccer/por.1",
        "club américa": "soccer/mex.1", "chivas": "soccer/mex.1", "cruz azul": "soccer/mex.1",
        "tigres": "soccer/mex.1", "monterrey": "soccer/mex.1", "santos laguna": "soccer/mex.1",
        "juárez": "soccer/mex.1", "juarez": "soccer/mex.1", "tijuana": "soccer/mex.1",
        "león": "soccer/mex.1", "leon": "soccer/mex.1", "toluca": "soccer/mex.1",
        "pachuca": "soccer/mex.1", "atlas": "soccer/mex.1", "puebla": "soccer/mex.1",
        "mazatlán": "soccer/mex.1", "necaxa": "soccer/mex.1", "querétaro": "soccer/mex.1",
        "champions league": "soccer/uefa.champions",
    }
    if "champions league" in q or "ucl" in q:
        return "soccer/uefa.champions"
    for team, league in _soccer_league_map.items():
        if team in q:
            return league
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


def _get_espn_score_data(team1: str, team2: str, question: str) -> dict | None:
    sport = _detect_sport(question)
    if not sport:
        return None

    try:
        # Query today + next 3 days to catch upcoming games.
        base = datetime.now(timezone.utc)
        dates_to_check = [(base + timedelta(days=d)).strftime("%Y%m%d") for d in range(4)]

        # Also try to extract a specific date from the question (string YYYY-MM-DD).
        target_date = _extract_date(question)
        if target_date:
            target_str = target_date.replace("-", "")
            if target_str not in dates_to_check:
                dates_to_check.insert(0, target_str)

        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/scoreboard"
        all_events = []
        for date_str in dates_to_check:
            try:
                resp = httpx.get(url, params={"dates": date_str}, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                all_events.extend(data.get("events", []))
            except Exception:
                continue

        if not all_events:
            return None

        # Strip the leading article for the prefix match so "the Lakers" keys on
        # "lakers" rather than "the La" (which would never hit the ESPN name).
        t1_lower = _normalize_team_name(team1, strip_article=True).lower()
        t2_lower = _normalize_team_name(team2, strip_article=True).lower() if team2 else ""
        for event in all_events:
            name = event.get("name", "").lower()
            short_name = event.get("shortName", "").lower()
            t1_match = t1_lower[:6] in name or t1_lower[:6] in short_name
            t2_match = not t2_lower or t2_lower[:6] in name or t2_lower[:6] in short_name
            if t1_match and t2_match:
                competition = event.get("competitions", [{}])[0]
                status_type = competition.get("status", {}).get("type", {})
                state = status_type.get("state", "pre")
                detail = status_type.get("detail", "")

                competitors = competition.get("competitors", [])
                structured_competitors = []
                total = 0
                total_available = True
                for competitor in competitors:
                    score_str = competitor.get("score", "0")
                    try:
                        score_val = int(score_str)
                    except (TypeError, ValueError):
                        score_val = 0
                        total_available = False
                    structured_competitors.append(
                        {
                            "team_name": competitor.get("team", {}).get("displayName", "Unknown"),
                            "score": score_val,
                            "home_away": competitor.get("homeAway", ""),
                        }
                    )
                    total += score_val

                return {
                    "sport": sport,
                    "state": state,
                    "detail": detail,
                    "competitors": structured_competitors,
                    "combined_score": total if total_available and state in ("post", "in") else None,
                }
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
    sport_slug = _detect_sport(question)
    score_data = _get_espn_score_data(team1, team2, question)
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
