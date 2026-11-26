"""Единый интерфейс биржи + реализация поверх ccxt (async). Только публичные рыночные данные: ключи не нужны."""
from __future__ import annotations

import gc
import time
from abc import ABC, abstractmethod
from typing import Optional

from ..models import Candle, FundingRate, OrderBook, Ticker

_BLOCK_HINTS = ("restricted location", "451", "geo", "forbidden", "403", "not available in your country",
                "unavailable from a restricted", "access denied", "region")


class ExchangeError(Exception):
    pass


class Unavailable(ExchangeError):
    pass


class Blocked(Unavailable):
    pass


class BadSymbol(ExchangeError):
    pass


class ExchangeAdapter(ABC):
    id: str = "abstract"
    markets: dict = {}

    @abstractmethod
    async def load_markets(self) -> dict: ...

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, timeframe: str, since: Optional[int] = None, limit: int = 500) -> list[Candle]: ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, limit: int = 50) -> OrderBook: ...

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    async def fetch_tickers(self, symbols: Optional[list[str]] = None) -> dict[str, Ticker]: ...

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> Optional[FundingRate]: ...

    async def close(self) -> None:
        pass

    def perp_symbol(self, spot_symbol: str) -> Optional[str]:
        try:
            base, quote = spot_symbol.split("/")
        except ValueError:
            return None
        cand = f"{base}/{quote}:{quote}"
        m = self.markets.get(cand)
        if m and m.get("swap") and m.get("linear", True) and m.get("active", True) is not False:
            return cand
        return None


def _classify(exc: Exception) -> Exception:
    import ccxt
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, ccxt.BadSymbol):
        return BadSymbol(msg)
    if isinstance(exc, (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.DDoSProtection, ccxt.RateLimitExceeded,
                        ccxt.RequestTimeout, ccxt.OnMaintenance)) or isinstance(exc, TimeoutError):
        return Blocked(msg) if any(h in low for h in _BLOCK_HINTS) else Unavailable(msg)
    if isinstance(exc, ccxt.AuthenticationError) or any(h in low for h in _BLOCK_HINTS):
        return Blocked(msg)
    return ExchangeError(msg)


class CcxtAdapter(ExchangeAdapter):
    def __init__(self, exchange_id: str, timeout_s: float = 15.0):
        import ccxt.async_support as ccxt_async
        if not hasattr(ccxt_async, exchange_id):
            raise ValueError(f"ccxt не знает биржу {exchange_id!r}")
        self.id = exchange_id
        self._ex = getattr(ccxt_async, exchange_id)({"enableRateLimit": True, "timeout": int(timeout_s * 1000)})
        self.markets = {}

    async def _call(self, name: str, *args, **kw):
        try:
            return await getattr(self._ex, name)(*args, **kw)
        except Exception as e:
            raise _classify(e) from e

    async def load_markets(self) -> dict:
        markets = await self._call("load_markets")
        # Освобождаем тяжёлые сырые JSON-поля info (экономят десятки МБ RAM на Render)
        for m in markets.values():
            if isinstance(m, dict) and m.get("info"):
                m["info"] = {}
        cur = getattr(self._ex, "currencies", None)
        if isinstance(cur, dict):
            for c in cur.values():
                if isinstance(c, dict) and c.get("info"):
                    c["info"] = {}
        self.markets = markets
        gc.collect()
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        rows = await self._call("fetch_ohlcv", symbol, timeframe, since, limit)
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)) for r in rows]

    async def fetch_order_book(self, symbol, limit=50):
        ob = await self._call("fetch_order_book", symbol, limit)
        ts = (ob.get("timestamp") or time.time() * 1000) / 1000
        return OrderBook(
            ts,
            [(float(x[0]), float(x[1])) for x in ob["bids"]],
            [(float(x[0]), float(x[1])) for x in ob["asks"]],
        )

    @staticmethod
    def _ticker(symbol: str, t: dict) -> Ticker:
        last = float(t.get("last") or t.get("close") or 0)
        return Ticker(symbol, (t.get("timestamp") or time.time() * 1000) / 1000, float(t.get("bid") or last),
                      float(t.get("ask") or last), last, float(t.get("quoteVolume") or 0))

    async def fetch_ticker(self, symbol):
        return self._ticker(symbol, await self._call("fetch_ticker", symbol))

    async def fetch_tickers(self, symbols=None):
        raw = await self._call("fetch_tickers", symbols) if symbols else await self._call("fetch_tickers")
        return {s: self._ticker(s, t) for s, t in raw.items()}

    async def fetch_funding_rate(self, symbol):
        if not self._ex.has.get("fetchFundingRate"):
            return None
        d = await self._call("fetch_funding_rate", symbol)
        rate = d.get("fundingRate")
        if rate is None:
            return None
        nxt = d.get("nextFundingTimestamp") or d.get("fundingTimestamp") or 0
        return FundingRate(symbol, float(rate), nxt / 1000 if nxt else 0.0, time.time())

    async def close(self) -> None:
        try:
            await self._ex.close()
        except Exception:
            pass
        finally:
            self.markets = {}
            for attr in ("markets", "markets_by_id", "currencies", "currencies_by_id", "symbols", "ids"):
                if hasattr(self._ex, attr):
                    try:
                        setattr(self._ex, attr, None)
                    except Exception:
                        pass
