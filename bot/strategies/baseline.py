"""Базовые стратегии: трендовая EMA и контрольная случайная random_benchmark."""
from __future__ import annotations

import random
from typing import Optional, Sequence

from ..market.indicators import atr, detect_regime, ema
from ..models import Candle, Signal
from .base import Strategy


class EmaTrendStrategy(Strategy):
    id, version, n_params = "ema_trend", "1.0", 2

    def __init__(self, fast: int = 20, slow: int = 50, timeframe: str = "1h"):
        self.fast, self.slow, self.timeframe = fast, slow, timeframe
        self.min_candles = slow + 20

    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None
        closes = [c.c for c in candles]
        f, s = ema(closes, self.fast), ema(closes, self.slow)
        cross_up = f[-2] <= s[-2] and f[-1] > s[-1]
        cross_dn = f[-2] >= s[-2] and f[-1] < s[-1]
        if not (cross_up or cross_dn):
            return None
        a = atr(candles, 14)
        return Signal(
            strategy_id=self.id, strategy_version=self.version, symbol=symbol,
            direction="long" if cross_up else "short", confidence=0.52,
            timeframe=self.timeframe, horizon_min=48 * 60, stop_atr_mult=2.5, take_atr_mult=4.0, ts=now,
            thesis=f"EMA{self.fast} пересекла EMA{self.slow} {'вверх' if cross_up else 'вниз'}",
            evidence=[f"EMA{self.fast}={f[-1]:.4f}", f"EMA{self.slow}={s[-1]:.4f}", f"ATR14={a:.4f}"],
            invalidation="обратное пересечение EMA / ATR-стоп", regime=detect_regime(candles),
        )


class RandomEntryStrategy(Strategy):
    id, version, n_params = "random_benchmark", "1.0", 0

    def __init__(self, prob: float = 0.03, seed: int = 42, timeframe: str = "1h"):
        self.prob, self.seed, self.timeframe = prob, seed, timeframe
        self.min_candles = 30

    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None
        rng = random.Random(f"{self.seed}:{symbol}:{candles[-1].ts}")
        if rng.random() > self.prob:
            return None
        d = "long" if rng.random() < 0.5 else "short"
        return Signal(
            strategy_id=self.id, strategy_version=self.version, symbol=symbol, direction=d, confidence=0.5,
            timeframe=self.timeframe, horizon_min=48 * 60, stop_atr_mult=2.5, take_atr_mult=4.0, ts=now,
            benchmark=True, thesis="контрольный случайный вход (бенчмарк)", regime="random_control",
            evidence=[f"seed={self.seed}", f"prob={self.prob}"], invalidation="ATR-стоп",
        )
