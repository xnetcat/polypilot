"""News research — RSS (plus optional NewsAPI) headlines for news-driven markets.

Ported from polydegen/scraper.py (NewsItem, scrape_rss, scrape_newsapi,
deduplicate), stripping config coupling. The curated RSS feed set lives here;
NewsAPI is used only when config.NEWSAPI_KEY is set.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .. import config

log = logging.getLogger(__name__)

# Curated RSS feeds (free, no API key needed). Ported from polydegen config.
RSS_FEEDS = [
    # General tech/AI
    "https://news.google.com/rss/search?q=AI+artificial+intelligence&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.feedburner.com/TechCrunch",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.theverge.com/rss/index.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    # Crypto
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://news.google.com/rss/search?q=bitcoin+OR+ethereum+OR+solana+OR+crypto&hl=en-US&gl=US&ceid=US:en",
    # Sports
    "https://www.espn.com/espn/rss/news",
    "https://news.google.com/rss/search?q=NBA+OR+NFL+OR+UFC+OR+soccer+OR+premier+league&hl=en-US&gl=US&ceid=US:en",
    # Weather
    "https://news.google.com/rss/search?q=weather+forecast+temperature&hl=en-US&gl=US&ceid=US:en",
    # Politics/Geopolitics
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "https://news.google.com/rss/search?q=congress+OR+white+house+OR+tariff+OR+sanctions&hl=en-US&gl=US&ceid=US:en",
    # Science/Space
    "https://news.google.com/rss/search?q=SpaceX+OR+NASA+OR+rocket+launch&hl=en-US&gl=US&ceid=US:en",
]

DEFAULT_LOOKBACK_HOURS = 48


@dataclass
class NewsItem:
    headline: str
    source: str
    published: str
    url: str
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "headline": self.headline,
            "source": self.source,
            "published": self.published,
            "url": self.url,
        }


def scrape_rss(feed_url: str, lookback_hours: int) -> list[NewsItem]:
    """Parse a single RSS feed and return recent items."""
    items: list[NewsItem] = []
    try:
        feed = feedparser.parse(feed_url)
    except Exception:
        return items

    if feed.get("bozo") and not getattr(feed, "entries", None):
        return items

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    source_name = feed.feed.get("title", feed_url)

    for entry in feed.entries:
        published = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
        else:
            published = datetime.now(timezone.utc)

        if published < cutoff:
            continue

        headline = entry.get("title", "").strip()
        if not headline:
            continue

        items.append(NewsItem(
            headline=headline,
            source=source_name,
            published=published.isoformat(),
            url=entry.get("link", ""),
            summary=entry.get("summary", "")[:500],
        ))

    return items


def scrape_newsapi(query: str, lookback_hours: int) -> list[NewsItem]:
    """Pull from NewsAPI.org if a key is configured."""
    if not config.NEWSAPI_KEY:
        return []

    items: list[NewsItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    from_dt = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        resp = httpx.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "from": from_dt,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": 50,
                "apiKey": config.NEWSAPI_KEY,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return items

    if data.get("status") != "ok":
        return items

    for article in data.get("articles", []):
        pub_str = article.get("publishedAt", "")
        try:
            published = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            published = datetime.now(timezone.utc)
        if published < cutoff:
            continue

        headline = article.get("title", "").strip()
        if not headline:
            continue

        items.append(NewsItem(
            headline=headline,
            source=article.get("source", {}).get("name", "NewsAPI"),
            published=published.isoformat(),
            url=article.get("url", ""),
            summary=(article.get("description") or "")[:500],
        ))

    return items


def deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    """Remove near-duplicate headlines by normalized prefix matching."""
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        if not item.headline:
            continue
        key = item.headline.lower()[:80]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _gather_rss(lookback_hours: int = DEFAULT_LOOKBACK_HOURS) -> list[NewsItem]:
    """Scrape the curated RSS feed set and return all recent items."""
    all_items: list[NewsItem] = []
    for feed_url in RSS_FEEDS:
        all_items.extend(scrape_rss(feed_url, lookback_hours))
    return all_items


def fetch_news(topic: str, limit: int = 20, lookback_hours: int = DEFAULT_LOOKBACK_HOURS) -> dict:
    """Recent news headlines (RSS, plus NewsAPI if configured) matching a topic.

    Filters headlines by case-insensitive substring match on the topic, dedups,
    and returns {topic, count, headlines: [{headline, source, published, url}]}.
    """
    items = list(_gather_rss(lookback_hours))
    if config.NEWSAPI_KEY:
        items.extend(scrape_newsapi(topic, lookback_hours))

    topic_lower = topic.lower().strip()
    if topic_lower:
        items = [it for it in items if topic_lower in it.headline.lower()]

    items = deduplicate(items)
    items.sort(key=lambda it: it.published, reverse=True)
    headlines = [it.to_dict() for it in items[:limit]]
    return {"topic": topic, "count": len(headlines), "headlines": headlines}
