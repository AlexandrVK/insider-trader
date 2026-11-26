"""Детерминированная имитация биржи для тестов и офлайн-демо."""
from __future__ import annotations

import time
import zlib
from typing import Callable, Optional

import numpy as np

from ..models import Candle, FundingRate, OrderBook, Ticker
from .adapter import BadSymbol, Blocked, ExchangeAdapter, Unavailable

TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
DEFAULT_SYMBOLS = {"BTC/USDT": (60000.0, 0.0006), "ETH/USDT": (3000.0, 0.0008), "SOL/USDT": (150.0, 0.0011),
                   "BNB/USDT": (600.0, 0.0007), "XRP/USDT": (0.6, 0.0010), "ADA/USDT": (0.45, 0.0011)}


class FakeExchange(ExchangeAdapter):
    def __init__(self, id: str = "fake", symbols: Optional[dict] = None, seed: int = 1, clock: Callable[[], float] = time.time,
                 t0: Optional[float] = None, days: int = 60, spread_bps: float = 2.0, depth_usd: float = 2_000_000.0,
                 drift: float = 0.0):
        self.id = id
        self.clock = clock
        self.spread_bps = spread_bps
        self.depth_usd = depth_usd
        self.symbols = symbols or DEFAULT_SYMBOLS
        now = clock()
        self.t0 = (t0 if t0 is not None else now - days * 86400 * 0.75) // 86400 * 86400
        n = days * 1440
        rng = np.random.default_rng(seed)
        self.paths = {}
        common = rng.normal(0, 0.0004, n)
        for k, (p0, vol) in sorted(self.symbols.items()):
            r = 0.6 * common + 0.8 * rng.normal(drift, vol, n)
            self.paths[k] = p0 * np.exp(np.cumsum(r))
        self.markets = {}
        for k in self.symbols:
            base, quote = k.split("/")
            self.markets[k] = {"symbol": k, "base": base, "quote": quote, "spot": True, "swap": False, "active": True}
            self.markets[f"{k}:{quote}"] = {"symbol": f"{k}:{quote}", "base": base, "quote": quote, "spot": False,
                                            "swap": True, "linear": True, "active": True}
        self.fail: set[str] = set()
        self.blocked = False
        self.calls: list[str] = []
        self.funding = 0.0001

    def _chk(self, op: str) -> None:
        self.calls.append(op)
        if self.blocked:
            raise Blocked("Service unavailable from a restricted location (451)")
        if op in self.fail or "*" in self.fail:
            raise Unavailable(f"{self.id}: имитация сбоя {op}")

    def _idx(self) -> int:
        return max(1, min(int((self.clock() - self.t0) // 60), len(next(iter(self.paths.values()))) - 1))

    def _path(self, symbol: str):
        s = symbol.split(":")[0]
        if s not in self.paths:
            raise BadSymbol(f"{self.id}: неизвестный символ {symbol}")
        return self.paths[s]

    def price(self, symbol: str) -> float:
        return float(self._path(symbol)[self._idx()])

    async def load_markets(self):
        self._chk("load_markets")
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        self._chk("fetch_ohlcv")
        p = self._path(symbol)
        m = TF_MIN[timeframe]
        now_i = self._idx()
        last_bar = now_i // m
        first = max(0, last_bar - limit + 1)
        if since is not None:
            first = max(first, int(((since / 1000) - self.t0) // (60 * m)))
        out = []
        for b in range(first, last_bar + 1):
            a, z = b * m, min((b + 1) * m, now_i + 1)
            seg = p[max(a, 0):max(z, a + 1)]
            o = float(p[a - 1]) if a > 0 else float(seg[0])
            vol = float(self.depth_usd / p[a] * (0.5 + (zlib.crc32(f"{symbol}{b}".encode()) % 100) / 100))
            out.append(Candle(int((self.t0 + a * 60) * 1000), o, float(seg.max()), float(seg.min()), float(seg[-1]), vol))
        return out[-limit:]

    async def fetch_order_book(self, symbol, limit=50):
        self._chk("fetch_order_book")
        mid = self.price(symbol)
        half = mid * self.spread_bps / 2e4
        per = self.depth_usd / 20 / mid
        bids, asks = [], []
        for i in range(limit):
            step = mid * 0.00025 * i
            q = per * (1 + 0.1 * i)
            bids.append((mid - half - step, q))
            asks.append((mid + half + step, q))
        return OrderBook(self.clock(), bids, asks)

    async def fetch_ticker(self, symbol):
        self._chk("fetch_ticker")
        mid = self.price(symbol)
        half = mid * self.spread_bps / 2e4
        return Ticker(symbol, self.clock(), mid - half, mid + half, mid, 50_000_000.0 * (1 + zlib.crc32(symbol.encode()) % 5))

    async def fetch_tickers(self, symbols=None):
        self._chk("fetch_tickers")
        return {s: await self.fetch_ticker(s) for s in (symbols or self.symbols)}

    async def fetch_funding_rate(self, symbol):
        self._chk("fetch_funding_rate")
        now = self.clock()
        return FundingRate(symbol, self.funding, (now // 28800 + 1) * 28800, now)
