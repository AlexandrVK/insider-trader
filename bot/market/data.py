"""Рыночные данные: свечи (кэш в SQLite, только закрытые), тикеры, стакан, свежесть, контекст."""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..db import Database
from ..exchange.adapter import ExchangeAdapter
from ..models import Candle, MarketContext, OrderBook, Ticker
from .indicators import atr

log = logging.getLogger("bot.data")
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


class MarketData:
    def __init__(self, hub: ExchangeAdapter, db: Database, clock: Callable[[], float] = time.time, ticker_ttl: float = 5.0,
                 stream=None):
        self.hub, self.db, self.clock, self.ticker_ttl, self.stream = hub, db, clock, ticker_ttl, stream
        self._tickers: dict[str, Ticker] = {}
        self._fetched_at: dict[str, float] = {}
        self._last_ok: dict[str, float] = {}

    @property
    def exchange_id(self) -> str:
        return getattr(self.hub, "exchange_id", self.hub.id)

    async def refresh_candles(self, symbol: str, tf: str, limit: int = 500) -> int:
        ex = self.exchange_id
        row = self.db.one("SELECT MAX(ts) AS m FROM candles WHERE exchange=? AND symbol=? AND timeframe=?", (ex, symbol, tf))
        last = row["m"] if row and row["m"] is not None else None
        since = last if last is not None else int((self.clock() - limit * TF_SEC[tf]) * 1000)
        candles = await self.hub.fetch_ohlcv(symbol, tf, since, limit)
        ex = self.exchange_id
        if candles:
            self.db.executemany(
                "INSERT OR REPLACE INTO candles (exchange, symbol, timeframe, ts, o, h, l, c, v) VALUES (?,?,?,?,?,?,?,?,?)",
                [(ex, symbol, tf, c.ts, c.o, c.h, c.l, c.c, c.v) for c in candles])
            cutoff = self.db.one(
                "SELECT ts FROM candles WHERE exchange=? AND symbol=? AND timeframe=? ORDER BY ts DESC LIMIT 1 OFFSET 600",
                (ex, symbol, tf),
            )
            if cutoff is not None:
                self.db.execute(
                    "DELETE FROM candles WHERE exchange=? AND symbol=? AND timeframe=? AND ts <= ?",
                    (ex, symbol, tf, cutoff["ts"]),
                )
            self._last_ok[symbol] = self.clock()
        return len(candles)

    def closed_candles(self, symbol: str, tf: str, limit: int = 300) -> list[Candle]:
        now_ms = self.clock() * 1000
        rows = self.db.query(
            "SELECT ts, o, h, l, c, v FROM candles WHERE exchange=? AND symbol=? AND timeframe=? AND ts + ? <= ? "
            "ORDER BY ts DESC LIMIT ?", (self.exchange_id, symbol, tf, TF_SEC[tf] * 1000, now_ms, limit))
        return [Candle(r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in reversed(rows)]

    async def ticker(self, symbol: str, force: bool = False) -> Ticker:
        now = self.clock()
        if self.stream is not None and not force:
            t = self.stream.fresh(symbol, 10.0)
            if t:
                self._tickers[symbol], self._fetched_at[symbol], self._last_ok[symbol] = t, now, now
                return t
        if not force and symbol in self._tickers and now - self._fetched_at.get(symbol, 0) < self.ticker_ttl:
            return self._tickers[symbol]
        t = await self.hub.fetch_ticker(symbol)
        self._tickers[symbol], self._fetched_at[symbol], self._last_ok[symbol] = t, now, now
        return t

    def ingest_tickers(self, tickers: dict) -> None:
        now = self.clock()
        for s, t in tickers.items():
            self._tickers[s], self._fetched_at[s], self._last_ok[s] = t, now, now

    async def order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        ob = await self.hub.fetch_order_book(symbol, depth)
        self._last_ok[symbol] = self.clock()
        return ob

    def data_age(self, symbol: str) -> float:
        t = self._last_ok.get(symbol)
        return float("inf") if t is None else max(0.0, self.clock() - t)

    async def alt_price(self, symbol: str) -> Optional[float]:
        adapters = getattr(self.hub, "adapters", None)
        if not adapters or len(adapters) < 2:
            return None
        for a in adapters:
            if a is self.hub.primary:
                continue
            try:
                return (await a.fetch_ticker(symbol)).mid
            except Exception:
                continue
        return None

    async def context(self, symbol: str, tf: str, instrument: str = "spot", cluster: str = "", with_alt: bool = True,
                      book_symbol: Optional[str] = None) -> MarketContext:
        t = await self.ticker(symbol, force=True)
        ob = await self.order_book(book_symbol or symbol, 50)
        cs = self.closed_candles(symbol, tf, 60)
        gap = 0.0
        if cs:
            gap = abs(t.mid / cs[-1].c - 1) * 100
            if len(cs) > 1:
                gap = max(gap, abs(cs[-1].c / cs[-2].c - 1) * 100)
        alt = await self.alt_price(symbol) if with_alt else None
        return MarketContext(symbol=symbol, price=ob.mid or t.mid, atr=atr(cs, 14), spread_bps=ob.spread_bps,
                             data_age_sec=self.data_age(symbol), depth_usd=ob.depth_usd(50), gap_pct=gap, alt_price=alt,
                             cluster=cluster, instrument=instrument, obi=ob.imbalance(10))
