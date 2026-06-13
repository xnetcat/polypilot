import polymarket_mcp.research.news as news


def test_fetch_news_filters_by_topic(monkeypatch):
    sample = [
        news.NewsItem(headline="Bitcoin surges past 70k", source="x",
                      published="2026-06-13", url="u1"),
        news.NewsItem(headline="Local bakery opens", source="y",
                      published="2026-06-13", url="u2"),
    ]
    monkeypatch.setattr(news, "_gather_rss", lambda *a, **k: sample)
    result = news.fetch_news("bitcoin", limit=10)
    assert len(result["headlines"]) == 1
    assert "Bitcoin" in result["headlines"][0]["headline"]
    assert result["count"] == 1
    assert result["topic"] == "bitcoin"


def test_fetch_news_respects_limit(monkeypatch):
    sample = [
        news.NewsItem(headline=f"Crypto news item {i}", source="x",
                      published=f"2026-06-{10 + i:02d}", url=f"u{i}")
        for i in range(5)
    ]
    monkeypatch.setattr(news, "_gather_rss", lambda *a, **k: sample)
    result = news.fetch_news("crypto", limit=3)
    assert len(result["headlines"]) == 3
    # Sorted by published descending -> newest first.
    assert result["headlines"][0]["headline"] == "Crypto news item 4"


def test_deduplicate_removes_duplicate_headlines():
    items = [
        news.NewsItem(headline="Same headline", source="a", published="t1", url="u1"),
        news.NewsItem(headline="Same headline", source="b", published="t2", url="u2"),
        news.NewsItem(headline="Different", source="c", published="t3", url="u3"),
    ]
    unique = news.deduplicate(items)
    assert len(unique) == 2


def test_feed_set_covers_broadened_categories():
    # The broadened feed set should include esports, sports, politics, tech,
    # and crypto sources, all merged into the flat RSS_FEEDS list.
    for cat in ("esports", "sports", "politics", "tech", "crypto"):
        assert cat in news.RSS_FEEDS_BY_CATEGORY
        assert news.RSS_FEEDS_BY_CATEGORY[cat], f"{cat} has no feeds"
    # HLTV (esports) is one of the broadened sources requested.
    assert any("hltv.org" in url for url in news.RSS_FEEDS_BY_CATEGORY["esports"])
    # Flat list is deduped but contains every category's feeds.
    for feeds in news.RSS_FEEDS_BY_CATEGORY.values():
        for url in feeds:
            assert url in news.RSS_FEEDS


def test_esports_topic_surfaces_matching_headline(monkeypatch):
    # Mock the per-feed scrape so no network is hit. The esports feeds now
    # carry esports headlines that should surface for an esports topic.
    def fake_scrape_rss(feed_url, lookback_hours):
        if "hltv.org" in feed_url:
            return [
                news.NewsItem(headline="NAVI win the CS2 Major in Copenhagen",
                              source="HLTV", published="2026-06-13", url="h1"),
            ]
        if "dexerto.com" in feed_url:
            return [
                news.NewsItem(headline="Valorant Champions bracket revealed",
                              source="Dexerto", published="2026-06-12", url="h2"),
            ]
        # Unrelated feed (e.g. crypto) — should be filtered out by topic.
        return [
            news.NewsItem(headline="Bitcoin dips below 60k", source="x",
                          published="2026-06-13", url="b1"),
        ]

    monkeypatch.setattr(news.config, "NEWSAPI_KEY", "", raising=False)
    monkeypatch.setattr(news, "scrape_rss", fake_scrape_rss)

    result = news.fetch_news("CS2", limit=10, category="esports")
    assert result["count"] >= 1
    assert any("CS2" in h["headline"] for h in result["headlines"])
    # The crypto headline must not leak through the topic filter.
    assert all("Bitcoin" not in h["headline"] for h in result["headlines"])


def test_category_crypto_prioritizes_crypto_feeds(monkeypatch):
    # category="crypto" must remain functional and surface crypto headlines.
    def fake_scrape_rss(feed_url, lookback_hours):
        if "cointelegraph" in feed_url or "coindesk" in feed_url or "decrypt" in feed_url:
            return [
                news.NewsItem(headline="Ethereum upgrade ships on mainnet",
                              source="crypto-feed", published="2026-06-13", url="c1"),
            ]
        return []

    monkeypatch.setattr(news.config, "NEWSAPI_KEY", "", raising=False)
    monkeypatch.setattr(news, "scrape_rss", fake_scrape_rss)

    result = news.fetch_news("ethereum", limit=10, category="crypto")
    assert result["count"] >= 1
    assert any("Ethereum" in h["headline"] for h in result["headlines"])


def test_feeds_for_category_prioritizes_then_includes_rest():
    esports = news.RSS_FEEDS_BY_CATEGORY["esports"]
    ordered = news._feeds_for_category("esports")
    # Esports feeds appear first.
    assert ordered[: len(esports)] == esports
    # Every curated feed is still represented.
    assert set(ordered) == set(news.RSS_FEEDS)


def test_feeds_for_category_none_returns_all():
    assert news._feeds_for_category(None) == news.RSS_FEEDS
    # Unknown category falls back to all feeds.
    assert news._feeds_for_category("nonsense") == news.RSS_FEEDS
