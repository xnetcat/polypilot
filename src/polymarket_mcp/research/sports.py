"""Sports research — ESPN scoreboard data for sports markets.

Ported from polydegen/research.py (sports helpers), stripping prompt-text
formatting and config/cache coupling.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

# Simple in-process cache keyed by question text.
_CACHE: dict[str, dict | None] = {}

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
    if any(t in q for t in nba_teams):
        return "basketball/nba"
    if any(t in q for t in nhl_teams):
        return "hockey/nhl"
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


def _extract_teams_from_question(question: str) -> tuple[str, str] | None:
    """Extract two team names from a sports question. Handles 'vs', 'beat', etc."""
    vs_match = re.search(r"(.+?)\s+vs\.?\s+(.+?)(?::|$|\?)", question)
    if vs_match:
        return vs_match.group(1).strip(), vs_match.group(2).strip()
    beat_match = re.search(
        r"[Ww]ill\s+(.+?)\s+(?:beat|defeat|win against)\s+(.+?)(?:\?|$|:| in )", question
    )
    if beat_match:
        return beat_match.group(1).strip(), beat_match.group(2).strip()
    win_match = re.search(r"[Ww]ill\s+(.+?)\s+win\b", question)
    if win_match:
        return win_match.group(1).strip(), ""
    draw_match = re.search(r"(.+?)\s+vs\.?\s+(.+?):", question)
    if draw_match:
        return draw_match.group(1).strip(), draw_match.group(2).strip()
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

        t1_lower = team1.lower()
        t2_lower = team2.lower() if team2 else ""
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
    if question in _CACHE:
        return _CACHE[question]
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
    _CACHE[question] = result
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
