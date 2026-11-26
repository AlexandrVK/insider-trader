"""Офлайн-симуляция на FakeExchange с управляемыми часами."""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .db import Database
from .engine import TradingEngine
from .exchange.fake import FakeExchange
from .exchange.hub import ExchangeHub


class Clock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


async def _nosleep(_):
    return None


def sim_settings(base: Optional[Settings] = None) -> Settings:
    s = (base or Settings()).model_copy(deep=True)
    s.universe.size, s.universe.min_quote_volume_usd, s.universe.min_depth_usd, s.universe.min_history_days = 6, 1e6, 1e5, 30
    s.universe.refresh_minutes = 24 * 60
    s.monitoring.snapshot_minutes = 60
    s.strategies.random_prob = 0.08
    s.ai.enabled = False
    s.copytrading.enabled = False
    return s


async def simulate(cfg: Settings, days: float = 7, step_sec: int = 300, seed: int = 1, db: Optional[Database] = None,
                   fake_kwargs: Optional[dict] = None, engine: Optional[TradingEngine] = None, clock: Optional[Clock] = None):
    if engine is None:
        start = 1_790_000_000.0 // 3600 * 3600
        clock = Clock(start)
        fake = FakeExchange(seed=seed, clock=clock, t0=start - 35 * 86400, days=int(days + 40), **(fake_kwargs or {}))
        hub = ExchangeHub([fake], clock=clock)
        db = db or Database(":memory:")
        engine = TradingEngine(cfg, db, hub, clock, sleep=_nosleep)
    for _ in range(int(days * 86400 / step_sec)):
        clock.now += step_sec
        await engine.cycle()
    return engine, clock
