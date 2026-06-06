"""News: Finnhub (if FINNHUB_API_KEY set) + RSS aggregation fallback.

Returns a flat list of recent headlines. We deliberately don't try to tag which
instrument each headline is about -- the LLM board-scan does relevance filtering
itself (it already proved it ignores irrelevant noise). So 'broad but noisy' is
fine here.
"""
from __future__ import annotations

from . import net  # noqa: F401

import os
import urllib.request
import xml.etree.ElementTree as ET

# Legitimate machine-readable feeds (RSS is 'scraping' the publisher endorses).
RSS_FEEDS = [
    "https://www.fxstreet.com/rss/news",
    "https://feeds.marketwatch.com/marketwatch/marketpulse/",
    "https://www.investing.com/rss/news_25.rss",
]


def _finnhub(limit: int) -> list[str]:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        import httpx
        url = f"https://finnhub.io/api/v1/news?category=forex&token={key}"
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        items = r.json()
        return [it["headline"] for it in items[:limit] if it.get("headline")]
    except Exception:
        return []


def _rss(limit: int) -> list[str]:
    out: list[str] = []
    for url in RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                root = ET.fromstring(resp.read())
            for item in root.iter("item"):
                title = item.findtext("title")
                if title:
                    out.append(title.strip())
                if len(out) >= limit:
                    break
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def fetch_headlines(limit: int = 20) -> list[str]:
    """Finnhub first (reliable), then top up with RSS. Deduped, capped."""
    seen: set[str] = set()
    result: list[str] = []
    for h in _finnhub(limit) + _rss(limit):
        if h not in seen:
            seen.add(h)
            result.append(h)
        if len(result) >= limit:
            break
    return result
