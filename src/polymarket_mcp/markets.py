"""Market discovery from Polymarket's Gamma/CLOB APIs."""
from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
import logging
import re

import httpx

from . import config

log = logging.getLogger(__name__)


def _hours_until(end_date: str) -> float | None:
    """Hours from now (UTC) until an ISO-8601 ``end_date``; None if unparseable.

    Negative when the close time is already past (e.g. an active-but-overdue
    market). Used so callers can target soon-resolving markets.
    """
    if not end_date:
        return None
    raw = end_date.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return round((dt - now).total_seconds() / 3600.0, 2)


@dataclass
class Market:
    condition_id: str
    question: str
    category: str
    yes_price: float
    no_price: float
    volume: float
    end_date: str
    active: bool
    tokens: list[dict]
    is_binary: bool = True

    @property
    def implied_probability(self) -> float:
        return self.yes_price

    @property
    def hours_to_close(self) -> float | None:
        """Hours from now until the market closes (None if unparseable)."""
        return _hours_until(self.end_date)

    def to_dict(self) -> dict:
        return {
            "market_id": self.condition_id,
            "question": self.question,
            "category": self.category,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "volume": self.volume,
            "end_date": self.end_date,
            "hours_to_close": self.hours_to_close,
            "active": self.active,
            "tokens": self.tokens,
            "is_binary": self.is_binary,
            "outcomes": [
                {
                    "outcome": t.get("outcome"),
                    "token_id": t.get("token_id"),
                    "price": t.get("price"),
                }
                for t in self.tokens
            ],
        }


def fetch_active_markets(limit: int = 50) -> list[Market]:
    """Fetch active, liquid markets from Polymarket's Gamma API."""
    markets = []

    try:
        resp = httpx.get(
            f"{config.GAMMA_API}/markets",
            params={
                "limit": limit,
                "active": True,
                "closed": False,
                "order": "volume",
                "ascending": False,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("[markets] Gamma API error: %s, falling back to CLOB...", e)
        return _fetch_from_clob(limit)

    items = data if isinstance(data, list) else data.get("data", [])

    for m in items:
        try:
            # Gamma API uses outcomePrices as a JSON string
            outcome_prices = m.get("outcomePrices", "")
            yes_price = 0.5
            no_price = 0.5
            prices: list = []  # always bound, even if outcomePrices is malformed JSON

            if outcome_prices:
                try:
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                    if len(prices) >= 2:
                        yes_price = float(prices[0])
                        no_price = float(prices[1])
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            token_list = _normalize_tokens(
                raw_tokens=m.get("tokens", []),
                raw_outcomes=m.get("outcomes", []),
                raw_clob_token_ids=m.get("clobTokenIds", []),
                raw_prices=prices if outcome_prices else [],
                yes_price=yes_price,
                no_price=no_price,
            )

            vol = float(m.get("volume", m.get("volumeNum", 0)) or 0)
            question = (m.get("question") or "").strip()
            condition_id = m.get("conditionId", m.get("condition_id", m.get("id", "")))
            is_binary = _has_binary_tokens(token_list)

            # Skip low-info / illiquid / resolved markets.
            # The price-extreme prune (yes_price at 0.0/1.0) only makes sense for
            # binary markets: a multi-outcome token sitting at 0.0/1.0 is a normal
            # long-shot/lock candidate and must not drop the whole market.
            if vol == 0 or not question or not condition_id:
                continue
            if is_binary and yes_price in (0.0, 1.0):
                continue
            if not _has_usable_tokens(token_list):
                continue

            markets.append(Market(
                condition_id=condition_id,
                question=question,
                category=_infer_category(question, m.get("tags", None) or []),
                yes_price=yes_price,
                no_price=no_price,
                volume=vol,
                end_date=m.get("endDate", m.get("end_date_iso", "")),
                active=m.get("active", True),
                tokens=token_list,
                is_binary=is_binary,
            ))
        except (KeyError, ValueError, TypeError):
            continue

    # Sort by volume descending
    markets.sort(key=lambda x: x.volume, reverse=True)
    return markets


def fetch_market(condition_id: str) -> Market | None:
    """Fetch a single market directly by its condition id.

    Uses the Gamma API ``condition_ids`` filter so the market is returned even
    if it would be far down the volume-sorted active list. Builds the ``Market``
    with the same normalization/fields/binary-token check as
    ``fetch_active_markets``. Returns ``None`` on any error or empty result.
    """
    if not condition_id:
        return None

    try:
        resp = httpx.get(
            f"{config.GAMMA_API}/markets",
            params={"condition_ids": condition_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("[markets] Gamma API error fetching %s: %s", condition_id, e)
        return None

    items = data if isinstance(data, list) else data.get("data", [])

    for m in items:
        try:
            # Gamma API uses outcomePrices as a JSON string
            outcome_prices = m.get("outcomePrices", "")
            yes_price = 0.5
            no_price = 0.5
            prices: list = []  # always bound, even if outcomePrices is malformed JSON

            if outcome_prices:
                try:
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                    if len(prices) >= 2:
                        yes_price = float(prices[0])
                        no_price = float(prices[1])
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            token_list = _normalize_tokens(
                raw_tokens=m.get("tokens", []),
                raw_outcomes=m.get("outcomes", []),
                raw_clob_token_ids=m.get("clobTokenIds", []),
                raw_prices=prices if outcome_prices else [],
                yes_price=yes_price,
                no_price=no_price,
            )

            vol = float(m.get("volume", m.get("volumeNum", 0)) or 0)
            question = (m.get("question") or "").strip()
            resolved_id = m.get("conditionId", m.get("condition_id", m.get("id", "")))

            # Direct lookup: keep the structural checks (must be a usable market
            # with at least 2 tradable tokens) but NOT the liquidity/resolution
            # filters that fetch_active_markets uses to prune its volume-sorted
            # scan — the caller asked for THIS market by id, so return it even if
            # 0-volume or priced at an extreme.
            if not question or not resolved_id:
                continue
            if not _has_usable_tokens(token_list):
                continue

            return Market(
                condition_id=resolved_id,
                question=question,
                category=_infer_category(question, m.get("tags", None) or []),
                yes_price=yes_price,
                no_price=no_price,
                volume=vol,
                end_date=m.get("endDate", m.get("end_date_iso", "")),
                active=m.get("active", True),
                tokens=token_list,
                is_binary=_has_binary_tokens(token_list),
            )
        except (KeyError, ValueError, TypeError):
            continue

    return None


def _fetch_from_clob(limit: int) -> list[Market]:
    """Fallback: fetch from CLOB API directly."""
    markets = []
    try:
        resp = httpx.get(
            f"{config.POLYMARKET_HOST}/markets",
            params={"limit": limit, "active": True},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("[markets] CLOB API error: %s", e)
        return markets

    items = data if isinstance(data, list) else data.get("data", data.get("markets", []))

    for m in items:
        try:
            tokens = m.get("tokens", [])
            yes_price = 0.5
            no_price = 0.5
            for t in tokens:
                outcome = t.get("outcome", "").lower()
                price = float(t.get("price", 0.5))
                if outcome == "yes":
                    yes_price = price
                elif outcome == "no":
                    no_price = price

            token_list = _normalize_tokens(
                raw_tokens=tokens,
                raw_outcomes=m.get("outcomes", []),
                raw_clob_token_ids=[],
                raw_prices=[],
                yes_price=yes_price,
                no_price=no_price,
            )
            condition_id = m.get("condition_id", m.get("id", ""))
            question = (m.get("question", "") or "").strip()
            volume = float(m.get("volume", 0) or 0)
            # Match the Gamma path: prune zero-volume / missing-id / non-usable
            # markets in this CLOB fallback too.
            if not condition_id or not question or volume == 0 or not _has_usable_tokens(token_list):
                continue

            markets.append(Market(
                condition_id=condition_id,
                question=question,
                category=_infer_category(question, m.get("tags") or []),
                yes_price=yes_price,
                no_price=no_price,
                volume=volume,
                end_date=m.get("end_date_iso", m.get("end_date", "")),
                active=m.get("active", True),
                tokens=token_list,
                is_binary=_has_binary_tokens(token_list),
            ))
        except (KeyError, TypeError, ValueError):
            continue

    markets.sort(key=lambda x: x.volume, reverse=True)
    return markets


def _parse_sequence(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _default_price_for_outcome(outcome: str, index: int, yes_price: float, no_price: float) -> float:
    normalized = outcome.upper()
    if normalized == "YES":
        return yes_price
    if normalized == "NO":
        return no_price
    return yes_price if index == 0 else no_price if index == 1 else 0.0


def _normalize_tokens(
    *,
    raw_tokens,
    raw_outcomes,
    raw_clob_token_ids,
    raw_prices,
    yes_price: float,
    no_price: float,
) -> list[dict]:
    tokens = _parse_sequence(raw_tokens)
    outcomes = [str(outcome).strip() for outcome in _parse_sequence(raw_outcomes) if str(outcome).strip()]
    clob_token_ids = [str(token_id) for token_id in _parse_sequence(raw_clob_token_ids) if token_id]
    price_values: list[float] = []
    for raw_price in _parse_sequence(raw_prices):
        try:
            price_values.append(float(raw_price))
        except (TypeError, ValueError):
            price_values.append(0.0)
    normalized: list[dict] = []

    for index, token in enumerate(tokens):
        if not isinstance(token, dict):
            continue

        outcome = str(
            token.get("outcome")
            or token.get("name")
            or token.get("label")
            or token.get("result")
            or ""
        ).strip()
        if not outcome and index < 2:
            outcome = "Yes" if index == 0 else "No"

        token_id = (
            token.get("token_id")
            or token.get("tokenId")
            or token.get("clobTokenId")
            or token.get("id")
        )
        if not token_id and index < len(clob_token_ids):
            token_id = clob_token_ids[index]
        if not token_id or not outcome:
            continue

        try:
            price = float(token.get("price"))
        except (TypeError, ValueError):
            if index < len(price_values):
                price = price_values[index]
            else:
                price = _default_price_for_outcome(outcome, index, yes_price, no_price)

        normalized.append(
            {
                "token_id": str(token_id),
                "outcome": outcome,
                "price": price,
            }
        )

    if normalized:
        return normalized

    fallback_outcomes = outcomes or ["Yes", "No"]
    for index, token_id in enumerate(clob_token_ids[: len(fallback_outcomes)]):
        outcome = fallback_outcomes[index]
        price = price_values[index] if index < len(price_values) else _default_price_for_outcome(outcome, index, yes_price, no_price)
        normalized.append(
            {
                "token_id": token_id,
                "outcome": outcome,
                "price": price,
            }
        )
    return normalized


def _has_binary_tokens(tokens: list[dict]) -> bool:
    outcomes = {str(token.get("outcome", "")).upper() for token in tokens}
    return outcomes == {"YES", "NO"}


def _is_usable_token(token: dict) -> bool:
    """A token is tradable if it has a token_id, an outcome label, and a price."""
    if not isinstance(token, dict):
        return False
    if not str(token.get("token_id", "")).strip():
        return False
    if not str(token.get("outcome", "")).strip():
        return False
    price = token.get("price")
    if isinstance(price, bool):
        return False
    return isinstance(price, (int, float))


def _has_usable_tokens(tokens: list[dict]) -> bool:
    """Markets need at least two tradable tokens to be worth surfacing.

    Replaces the old strict YES/NO gate so multi-outcome markets (N>2 candidate
    tokens) pass discovery. Degenerate markets (0 or 1 usable token) are still
    dropped.
    """
    return sum(1 for token in tokens if _is_usable_token(token)) >= 2


def _contains_keyword(text: str, keyword: str) -> bool:
    if re.fullmatch(r"[a-z0-9]+", keyword):
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text


def _contains_any_keyword(text: str, keywords: list[str]) -> bool:
    return any(_contains_keyword(text, keyword) for keyword in keywords)


def _infer_category(question: str, tags: list) -> str:
    """Infer category from question text and tags."""
    q = question.lower()
    tag_str = " ".join(str(t).lower() for t in tags)
    combined = f"{q} {tag_str}"

    if _contains_any_keyword(combined, ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "blockchain", "defi", "token"]):
        return "crypto"
    # Politics BEFORE weather — "ukraine" contains "rain" which would false-match weather
    if _contains_any_keyword(combined, ["election", "president", "congress", "senate", "trump", "biden", "political",
                                        "rutte", "white house", "putin", "zelensky", "diplomat", "prime minister",
                                        "legislation", "executive order", "veto", "impeach", "governor",
                                        "trump say", "will trump", "biden say", "will biden"]):
        return "politics"
    if _contains_any_keyword(combined, ["temperature", "weather", "°c", "°f", "highest temp", "lowest temp",
                                        "rain forecast", "rainfall", "precipitation"]):
        return "weather"
    if _contains_any_keyword(combined, ["counter-strike", "valorant", "dota", "league of legends", "rocket league", "overwatch", "rainbow six", "honor of kings"]):
        return "esports"
    if _contains_any_keyword(combined, ["ufc", "nba", "nfl", "nhl", "mlb", "fight", "match", "game", "vs.", "o/u", "rounds",
                                        "win the 2", "premier league", "la liga", "serie a", "bundesliga"]):
        return "sports"
    if _contains_any_keyword(combined, ["ai", "artificial intelligence", "openai", "chatgpt", "llm", "google ai", "anthropic"]):
        return "ai"
    if _contains_any_keyword(combined, ["spacex", "nasa", "climate", "research", "study", "discovery"]):
        return "science"
    if _contains_any_keyword(combined, ["tech", "apple", "google", "microsoft", "software", "startup"]):
        return "technology"

    q_lower = question.lower()
    for keyword, cat in [
        ("tariff", "finance"), ("gdp", "economy"), ("trade war", "finance"),
        ("interest rate", "finance"), ("fed ", "finance"), ("federal reserve", "finance"),
        ("inflation", "economy"), ("unemployment", "economy"),
        ("oscar", "culture"), ("grammy", "culture"), ("emmy", "culture"),
        ("sanction", "geopolitics"), ("nato", "geopolitics"), ("un ", "geopolitics"),
    ]:
        if keyword in q_lower:
            return cat

    return "other"


def get_token_id(market: Market, side: str) -> str | None:
    """Get the token ID for a given side (YES/NO)."""
    for t in market.tokens:
        if t.get("outcome", "").upper() == side.upper():
            return t.get("token_id")
    return None
