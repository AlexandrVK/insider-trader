"""Необязательный websocket-поток тикеров (ccxt.pro) с авто-переподключением."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from ..models import Ticker

log = logging.getLogger("bot.feed")


class PriceStream:
    def __init__(self, exchange_id: str, symbols: list[str], watch_factory: Optional[Callable] = None,
                 backoff_max: float = 60.0, clock: Callable[[], float] = time.time):
        self.exchange_id, self.symbols, self.watch_factory = exchange_id, symbols, watch_factory
        self.backoff_max, self.clock = backoff_max, clock
        self.latest: dict[str, Ticker] = {}
        self.reconnects = 0

    def fresh(self, symbol: str, max_age: float) -> Optional[Ticker]:
        t = self.latest.get(symbol)
        return t if t and self.clock() - t.ts <= max_age else None

    async def _default_watch(self):
        import ccxt.pro as ccxtpro
        ex = getattr(ccxtpro, self.exchange_id)({"enableRateLimit": True})
        try:
            while True:
                yield await ex.watch_tickers(self.symbols)
        finally:
            await ex.close()

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                async for raw in (self.watch_factory or self._default_watch)():
                    now = self.clock()
                    for s, t in raw.items():
                        last = float(t.get("last") or 0)
                        if last:
                            self.latest[s] = Ticker(s, now, float(t.get("bid") or last), float(t.get("ask") or last), last,
                                                    float(t.get("quoteVolume") or 0))
                    delay = 1.0
                    if stop.is_set():
                        return
                if self.watch_factory is not None:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.reconnects += 1
                log.warning("websocket-поток упал, переподключение", extra={"error": str(e)[:200], "in_s": delay})
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, self.backoff_max)
