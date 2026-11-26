"""Автоформирование вселенной: ликвидность, спред, глубина, история, накрутка объёма, кластеры корреляции."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from ..config import UniverseCfg
from ..exchange.adapter import ExchangeAdapter
from .indicators import correlation_clusters, suspicious_volume_ratio

log = logging.getLogger("bot.universe")
STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "EUR", "EURI", "GBP", "AEUR", "USDE", "PYUSD", "XUSD", "USD1", "UST"}
_LEVERAGED = re.compile(r"(UP|DOWN|BULL|BEAR|[235][LS])$")


@dataclass
class UniverseResult:
    symbols: list[str]
    clusters: dict[str, str] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)
    stats: dict[str, dict] = field(default_factory=dict)
    ts: float = 0.0

    def to_json(self) -> str:
        return json.dumps({"symbols": self.symbols, "clusters": self.clusters, "rejected": self.rejected,
                           "stats": self.stats, "ts": self.ts}, ensure_ascii=False)


def _eligible(markets: dict, cfg: UniverseCfg) -> list[str]:
    out = []
    for sym, m in markets.items():
        if not (m.get("spot") and m.get("quote") == cfg.quote and m.get("active", True) is not False):
            continue
        base = m.get("base", "")
        if base in STABLES or sym in cfg.exclude_symbols or base in cfg.exclude_symbols:
            continue
        if _LEVERAGED.search(base) and len(base) > 4:
            continue
        out.append(sym)
    return out


async def select_universe(ex: ExchangeAdapter, cfg: UniverseCfg, now: float | None = None) -> UniverseResult:
    now = now or time.time()
    res = UniverseResult([], ts=now)
    markets = await ex.load_markets()
    cand = _eligible(markets, cfg)
    tickers = await ex.fetch_tickers(cand)
    ranked = sorted(((s, t) for s, t in tickers.items() if s in cand), key=lambda st: -st[1].quote_volume)
    for s, t in ranked:
        if t.quote_volume < cfg.min_quote_volume_usd:
            res.rejected[s] = f"объём 24ч ${t.quote_volume:,.0f} ниже порога"
    shortlist = [s for s, t in ranked if t.quote_volume >= cfg.min_quote_volume_usd][: cfg.size * 3]
    sem = asyncio.Semaphore(4)

    async def check(sym: str):
        async with sem:
            try:
                ob = await ex.fetch_order_book(sym, 50)
                if ob.spread_bps > cfg.max_spread_bps:
                    return sym, f"спред {ob.spread_bps:.1f} bps выше порога", {}
                depth = ob.depth_usd(50)
                if depth < cfg.min_depth_usd:
                    return sym, f"глубина ${depth:,.0f} (0.5%) ниже порога", {}
                daily = await ex.fetch_ohlcv(sym, "1d", None, cfg.min_history_days + 10)
                if len(daily) < cfg.min_history_days:
                    return sym, f"история {len(daily)} дн. < {cfg.min_history_days}", {}
                wash = suspicious_volume_ratio(daily)
                if wash > cfg.wash_volume_ratio:
                    return sym, f"подозрение на накрутку объёма ({wash:.0%} дней)", {}
                return sym, "", {"spread_bps": round(ob.spread_bps, 2), "depth_usd": round(depth), "history_days": len(daily),
                                 "quote_volume": round(tickers[sym].quote_volume), "wash_ratio": round(wash, 3),
                                 "closes": [c.c for c in daily[-90:]]}
            except Exception as e:
                return sym, f"ошибка проверки: {type(e).__name__}", {}

    checked = await asyncio.gather(*(check(s) for s in shortlist))
    closes = {}
    for sym, reason, st in checked:
        if reason:
            res.rejected[sym] = reason
        elif len(res.symbols) < cfg.size:
            res.symbols.append(sym)
            closes[sym] = st.pop("closes")
            res.stats[sym] = st
        else:
            res.rejected[sym] = "вне топ-N"
    res.clusters = correlation_clusters(closes, 0.8)
    return res
