"""Optional news headlines via free RSS (stdlib only, no API key).

Sentiment is the weakest link in any retail system (news is priced in fast),
so this is best-effort: if a feed is blocked or empty, the system carries on
and the sentiment agent simply scores 0 with reduced confidence.
"""
from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET

# A couple of free, no-key finance RSS feeds. Order = priority.
DEFAULT_FEEDS = [
    "https://www.investing.com/rss/news_25.rss",       # forex news
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",   # WSJ markets
]


def fetch_headlines(feeds: list[str] | None = None, limit: int = 12) -> list[str]:
    feeds = feeds or DEFAULT_FEEDS
    headlines: list[str] = []
    for url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                root = ET.fromstring(resp.read())
            for item in root.iter("item"):
                title = item.findtext("title")
                if title:
                    headlines.append(title.strip())
                if len(headlines) >= limit:
                    break
        except Exception:
            continue
        if len(headlines) >= limit:
            break
    return headlines[:limit]
