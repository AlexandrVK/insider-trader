"""Пул стратегий: EMA, MACD, Donchian, RSI, Bollinger, Momentum, Flat, Random."""
from __future__ import annotations

from typing import Optional, Sequence

from ..config import StrategiesCfg
from ..market.indicators import bollinger, closes, detect_regime, donchian, macd, rsi
from ..models import Candle, Signal
from .base import Strategy
from .baseline import EmaTrendStrategy, RandomEntryStrategy


class MacdTrendStrategy(Strategy):
    id, version, n_params = "macd_trend", "1.0", 3

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, timeframe: str = "1h"):
        self.fast, self.slow, self.signal, self.timeframe = fast, slow, signal, timeframe
        self.min_candles = slow + signal + 15

    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None
        line, _, hist = macd(closes(candles), self.fast, self.slow, self.signal)
        up, dn = hist[-2] <= 0 and hist[-1] > 0, hist[-2] >= 0 and hist[-1] < 0
        if not (up or dn):
            return None
        return Signal(
            strategy_id=self.id, strategy_version=self.version, symbol=symbol,
            direction="long" if up else "short", confidence=0.55, timeframe=self.timeframe,
            horizon_min=36 * 60, stop_atr_mult=2.2, take_atr_mult=3.8, ts=now,
            regime=detect_regime(candles),
            thesis=f"MACD({self.fast},{self.slow},{self.signal}) пересечение {'вверх' if up else 'вниз'}",
            evidence=[f"hist={hist[-1]:.5f}", f"line={line[-1]:.5f}"],
            invalidation="обратное пересечение MACD / ATR-стоп",
        )


class DonchianBreakoutStrategy(Strategy):
    id, version, n_params = "donchian_breakout", "1.0", 1

    def __init__(self, period: int = 20, timeframe: str = "1h"):
        self.period, self.timeframe = period, timeframe
        self.min_candles = period + 10

    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None
        lo, hi = donchian(candles, self.period)
        last = candles[-1].c
        d = "long" if last > hi > 0 else ("short" if last < lo and lo > 0 else None)
        if not d:
            return None
        return Signal(
            strategy_id=self.id, strategy_version=self.version, symbol=symbol, direction=d, confidence=0.58,
            timeframe=self.timeframe, horizon_min=48 * 60, stop_atr_mult=2.5, take_atr_mult=4.5, ts=now,
            regime=detect_regime(candles),
            thesis=f"Пробой канала Дончиана({self.period}) {'вверх' if d == 'long' else 'вниз'}",
            evidence=[f"close={last:.4f}", f"hi={hi:.4f}", f"lo={lo:.4f}"],
            invalidation="возврат внутрь канала / ATR-стоп",
        )


class RsiMeanReversionStrategy(Strategy):
    id, version, n_params = "rsi_mean_rev", "1.0", 3

    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0, timeframe: str = "1h"):
        self.period, self.oversold, self.overbought, self.timeframe = period, oversold, overbought, timeframe
        self.min_candles = period + 20

    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None
        regime = detect_regime(candles)
        if regime == "panic":
            return None
        r = rsi(closes(candles), self.period)
        d = "long" if r[-2] < self.oversold <= r[-1] else ("short" if r[-2] > self.overbought >= r[-1] else None)
        if not d:
            return None
        return Signal(
            strategy_id=self.id, strategy_version=self.version, symbol=symbol, direction=d, confidence=0.52,
            timeframe=self.timeframe, horizon_min=24 * 60, stop_atr_mult=1.8, take_atr_mult=2.5, ts=now,
            regime=regime, thesis=f"Возврат RSI({self.period}) из экстремума",
            evidence=[f"rsi_prev={r[-2]:.1f}", f"rsi_now={r[-1]:.1f}"], invalidation="ATR-стоп",
        )


class BollingerMeanReversionStrategy(Strategy):
    id, version, n_params = "bb_mean_rev", "1.0", 2

    def __init__(self, period: int = 20, k: float = 2.0, timeframe: str = "1h"):
        self.period, self.k, self.timeframe = period, k, timeframe
        self.min_candles = period + 15

    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None
        regime = detect_regime(candles)
        if regime == "panic":
            return None
        c = closes(candles)
        lo, mid, hi = bollinger(c, self.period, self.k)
        d = "long" if c[-2] < lo[-2] and c[-1] >= lo[-1] else ("short" if c[-2] > hi[-2] and c[-1] <= hi[-1] else None)
        if not d:
            return None
        return Signal(
            strategy_id=self.id, strategy_version=self.version, symbol=symbol, direction=d, confidence=0.53,
            timeframe=self.timeframe, horizon_min=24 * 60, stop_atr_mult=1.8, take_atr_mult=2.4, ts=now,
            regime=regime, thesis=f"Возврат внутрь Bollinger({self.period},{self.k})",
            evidence=[f"close={c[-1]:.4f}", f"mid={mid[-1]:.4f}"], invalidation="ATR-стоп",
        )


class MomentumStrategy(Strategy):
    id, version, n_params = "momentum", "1.0", 2

    def __init__(self, lookback: int = 24, threshold_pct: float = 2.5, timeframe: str = "1h"):
        self.lookback, self.threshold_pct, self.timeframe = lookback, threshold_pct, timeframe
        self.min_candles = lookback + 10

    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None
        c = closes(candles)
        ret = (c[-1] / c[-1 - self.lookback] - 1.0) * 100.0
        prev = (c[-2] / c[-2 - self.lookback] - 1.0) * 100.0
        d = "long" if prev < self.threshold_pct <= ret else ("short" if prev > -self.threshold_pct >= ret else None)
        if not d:
            return None
        return Signal(
            strategy_id=self.id, strategy_version=self.version, symbol=symbol, direction=d, confidence=0.56,
            timeframe=self.timeframe, horizon_min=36 * 60, stop_atr_mult=2.2, take_atr_mult=3.5, ts=now,
            regime=detect_regime(candles), thesis=f"Импульс за {self.lookback} свечей: {ret:+.2f}%",
            evidence=[f"ret={ret:+.2f}%"], invalidation="ATR-стоп",
        )


class FlatStrategy(Strategy):
    id, version, n_params = "flat", "1.0", 0

    def __init__(self, timeframe: str = "1h"):
        self.timeframe = timeframe
        self.min_candles = 1

    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]:
        return None


def build_strategies(cfg: StrategiesCfg) -> list[Strategy]:
    tf = cfg.timeframe
    reg = {
        "ema_trend": lambda: EmaTrendStrategy(cfg.ema_fast, cfg.ema_slow, tf),
        "macd_trend": lambda: MacdTrendStrategy(12, 26, 9, tf),
        "donchian_breakout": lambda: DonchianBreakoutStrategy(20, tf),
        "rsi_mean_rev": lambda: RsiMeanReversionStrategy(14, 30.0, 70.0, tf),
        "bb_mean_rev": lambda: BollingerMeanReversionStrategy(20, 2.0, tf),
        "momentum": lambda: MomentumStrategy(24, 2.5, tf),
        "flat": lambda: FlatStrategy(tf),
        "random_benchmark": lambda: RandomEntryStrategy(cfg.random_prob, cfg.random_seed, tf),
    }
    return [reg[name]() for name in cfg.enabled if name in reg]
