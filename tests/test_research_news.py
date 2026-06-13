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
