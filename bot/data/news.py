"""Бесплатные источники новостей, сентимента и макро-данных (RSS, Google News, Fear & Greed, FRED)."""
from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

import requests

RSS_FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
}


@dataclass
class NewsItem:
    id: str
    source_id: str
    title: str
    published_ts: float
    url: str = ""
    assets: list[str] = field(default_factory=list)


def _clean(text: str) -> str:
    return re.sub(r"[\x00-\x1f\s]+", " ", text or "").strip()[:300]


def fetch_rss(source_id: str, url: str, limit: int = 10, timeout: float = 10.0) -> list[NewsItem]:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    now = time.time()
    out: list[NewsItem] = []
    for it in list(root.iter("item"))[:limit]:
        title = _clean(it.findtext("title", ""))
        link = _clean(it.findtext("link", ""))
        if not title:
            continue
        nid = hashlib.sha256(f"{source_id}:{title.lower()}".encode()).hexdigest()[:16]
        out.append(NewsItem(id=nid, source_id=source_id, title=title, published_ts=now, url=link))
    return out


def fetch_google_news_rss(query: str, limit: int = 8, timeout: float = 10.0) -> list[NewsItem]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    return fetch_rss("google_news_rss", url, limit=limit, timeout=timeout)


def fetch_fear_and_greed(timeout: float = 10.0) -> Optional[dict]:
    r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=timeout)
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        return None
    return {"value": int(data[0]["value"]), "classification": data[0]["value_classification"], "ts": time.time()}


def fetch_fred_series(series_id: str = "DFF", timeout: float = 10.0) -> Optional[float]:
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        return None
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}"
           f"&api_key={key}&file_type=json&sort_order=desc&limit=1")
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    obs = r.json().get("observations") or []
    if not obs or obs[0].get("value") in (None, "."):
        return None
    return float(obs[0]["value"])
