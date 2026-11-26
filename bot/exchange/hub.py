"""Хаб бирж: основная + резервные, автоматическое переключение при недоступности, автовыбор по пробе."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .adapter import BadSymbol, Blocked, ExchangeAdapter, Unavailable

log = logging.getLogger("bot.hub")


class NoExchangeAvailable(Exception):
    pass


@dataclass
class Health:
    failures: int = 0
    last_error: str = ""
    down_until: float = 0.0
    blocked: bool = False


class ExchangeHub(ExchangeAdapter):
    id = "hub"

    def __init__(self, adapters: list[ExchangeAdapter], cooldown_s: float = 60.0, clock: Callable[[], float] = time.time,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        if not adapters:
            raise ValueError("нужна хотя бы одна биржа")
        self.adapters = list(adapters)
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.on_event = on_event or (lambda kind, d: None)
        self.health = {a.id: Health() for a in adapters}
        self._primary = 0

    @property
    def primary(self) -> ExchangeAdapter:
        return self.adapters[self._primary]

    @property
    def exchange_id(self) -> str:
        return self.primary.id

    @property
    def markets(self) -> dict:
        return self.primary.markets

    def set_primary(self, exchange_id: str) -> bool:
        for i, a in enumerate(self.adapters):
            if a.id == exchange_id:
                self._primary = i
                return True
        return False

    def _order(self) -> list[int]:
        now = self.clock()
        idx = [self._primary] + [i for i in range(len(self.adapters)) if i != self._primary]
        up = [i for i in idx if self.health[self.adapters[i].id].down_until <= now]
        return up or idx

    async def _call(self, op: str, *args, **kw):
        errors = []
        for i in self._order():
            a = self.adapters[i]
            h = self.health[a.id]
            try:
                res = await getattr(a, op)(*args, **kw)
            except BadSymbol:
                raise
            except Unavailable as e:
                h.failures += 1
                h.last_error = str(e)[:200]
                h.blocked = isinstance(e, Blocked)
                h.down_until = self.clock() + self.cooldown_s * (10 if h.blocked else 1)
                errors.append(f"{a.id}: {h.last_error}")
                continue
            h.failures = 0
            if i != self._primary:
                old = self.primary.id
                self._primary = i
                self.on_event("failover", {"from": old, "to": a.id, "op": op, "errors": errors})
            return res
        raise Unavailable("все биржи недоступны: " + "; ".join(errors))

    async def load_markets(self):
        return await self._call("load_markets")

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        return await self._call("fetch_ohlcv", symbol, timeframe, since, limit)

    async def fetch_order_book(self, symbol, limit=50):
        return await self._call("fetch_order_book", symbol, limit)

    async def fetch_ticker(self, symbol):
        return await self._call("fetch_ticker", symbol)

    async def fetch_tickers(self, symbols=None):
        return await self._call("fetch_tickers", symbols)

    async def fetch_funding_rate(self, symbol):
        return await self._call("fetch_funding_rate", symbol)

    def perp_symbol(self, spot_symbol):
        return self.primary.perp_symbol(spot_symbol)

    async def close(self):
        await asyncio.gather(*(a.close() for a in self.adapters), return_exceptions=True)


@dataclass
class ProbeResult:
    id: str
    ok: bool
    reason: str = ""
    latency_ms: float = 0.0
    n_markets: int = 0
    n_quote_markets: int = 0
    has_perp: bool = False
    blocked: bool = False

    @property
    def score(self) -> float:
        if not self.ok:
            return float("-inf")
        return min(self.n_quote_markets, 500) / 100 + (1.0 if self.has_perp else 0.0) - self.latency_ms / 400


async def probe_exchange(adapter: ExchangeAdapter, symbol: str = "BTC/USDT", timeout_s: float = 20.0) -> ProbeResult:
    res = ProbeResult(adapter.id, False)

    async def run():
        markets = await adapter.load_markets()
        res.n_markets = len(markets)
        quote = symbol.split("/")[1]
        res.n_quote_markets = sum(1 for m in markets.values() if m.get("spot") and m.get("quote") == quote and m.get("active", True) is not False)
        res.has_perp = adapter.perp_symbol(symbol) is not None
        lat = []
        for fn in (lambda: adapter.fetch_ticker(symbol), lambda: adapter.fetch_order_book(symbol, 20)):
            t1 = time.perf_counter()
            await fn()
            lat.append((time.perf_counter() - t1) * 1000)
        res.latency_ms = sum(lat) / len(lat)
        res.ok = True

    try:
        await asyncio.wait_for(run(), timeout_s)
    except asyncio.TimeoutError:
        res.reason = "таймаут пробы"
    except Blocked as e:
        res.blocked, res.reason = True, f"доступ ограничен: {str(e)[:120]}"
    except Exception as e:
        res.reason = f"{type(e).__name__}: {str(e)[:120]}"
    return res


async def autoselect(factory: Callable[[str], ExchangeAdapter], candidates: list[str], symbol: str = "BTC/USDT",
                     timeout_s: float = 20.0, min_backups: int = 1, cooldown_s: float = 60.0,
                     on_event: Optional[Callable[[str, dict], None]] = None) -> tuple[ExchangeHub, list[ProbeResult]]:
    adapters = []
    for c in candidates:
        try:
            adapters.append(factory(c))
        except Exception:
            pass
    results = await asyncio.gather(*(probe_exchange(a, symbol, timeout_s) for a in adapters))
    good = sorted([(r, a) for r, a in zip(results, adapters) if r.ok], key=lambda ra: -ra[0].score)
    for r, a in zip(results, adapters):
        if not r.ok:
            await a.close()
    if not good:
        raise NoExchangeAvailable("ни одна биржа не прошла пробу: " + "; ".join(f"{r.id}: {r.reason}" for r in results))
    keep = good[:1 + max(0, min_backups)]
    for _, a in good[len(keep):]:
        await a.close()
    return ExchangeHub([a for _, a in keep], cooldown_s=cooldown_s, on_event=on_event), results


def probe_summary(results: list[ProbeResult]) -> str:
    return json.dumps([r.__dict__ for r in results], ensure_ascii=False)
