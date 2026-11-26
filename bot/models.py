"""Базовые модели. Время: свечи - миллисекунды (как в ccxt), остальное - секунды epoch (float)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field


@dataclass(slots=True, frozen=True)
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(slots=True)
class Ticker:
    symbol: str
    ts: float
    bid: float
    ask: float
    last: float
    quote_volume: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else self.last


@dataclass(slots=True)
class FundingRate:
    symbol: str
    rate: float
    next_ts: float
    ts: float


@dataclass(slots=True)
class WalkResult:
    filled: float
    avg_px: float
    notional: float
    levels: int
    limited_by: str


@dataclass
class OrderBook:
    ts: float
    bids: list
    asks: list

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2 if self.bids and self.asks else 0.0

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.best_ask - self.best_bid) / m * 1e4 if m else float("inf")

    def depth_usd(self, within_bps: float = 50.0) -> float:
        m = self.mid
        if not m:
            return 0.0
        lo, hi = m * (1 - within_bps / 1e4), m * (1 + within_bps / 1e4)
        bid = sum(p * q for p, q in self.bids if p >= lo)
        ask = sum(p * q for p, q in self.asks if p <= hi)
        return min(bid, ask)

    def imbalance(self, levels: int = 10) -> float:
        """Дисбаланс стакана (OBI) по топ-N уровням: +1 — перевес покупателей, -1 — продавцов."""
        b = sum(p * q for p, q in self.bids[:levels])
        a = sum(p * q for p, q in self.asks[:levels])
        tot = b + a
        return (b - a) / tot if tot > 0 else 0.0

    def walk(self, side: Literal["buy", "sell"], qty: float, max_slip_bps: Optional[float] = None) -> WalkResult:
        book = self.asks if side == "buy" else self.bids
        if not book or qty <= 0:
            return WalkResult(0.0, 0.0, 0.0, 0, "depth")
        best = book[0][0]
        limit = None
        if max_slip_bps is not None:
            limit = best * (1 + max_slip_bps / 1e4) if side == "buy" else best * (1 - max_slip_bps / 1e4)
        filled = notional = 0.0
        levels, limited = 0, ""
        for px, q in book:
            if limit is not None and ((side == "buy" and px > limit) or (side == "sell" and px < limit)):
                limited = "slippage"
                break
            take = min(q, qty - filled)
            if take <= 0:
                break
            filled += take
            notional += take * px
            levels += 1
            if filled >= qty - 1e-12:
                break
        if filled < qty - 1e-12 and not limited:
            limited = "depth"
        return WalkResult(filled, notional / filled if filled else 0.0, notional, levels, limited if filled < qty - 1e-12 else "")


class Signal(BaseModel):
    strategy_id: str
    strategy_version: str = "1"
    symbol: str
    direction: Literal["long", "short", "flat"]
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    timeframe: str = "1h"
    horizon_min: int = 24 * 60
    thesis: str = ""
    evidence: list[str] = Field(default_factory=list)
    invalidation: str = ""
    sources: list[str] = Field(default_factory=list)
    regime: str = ""
    stop_atr_mult: float = 2.0
    take_atr_mult: Optional[float] = 3.0
    benchmark: bool = False
    ai_assisted: bool = False
    ts: float = 0.0


@dataclass
class MarketContext:
    symbol: str
    price: float
    atr: float
    spread_bps: float
    data_age_sec: float
    depth_usd: float = 0.0
    gap_pct: float = 0.0
    alt_price: Optional[float] = None
    cluster: str = ""
    instrument: str = "spot"
    funding_rate: float = 0.0
    obi: float = 0.0
    vol_scalar: float = 1.0
    mtf_trend: int = 0
    ml_scale: float = 1.0
    ml_prob: Optional[float] = None
    ml_features: dict = field(default_factory=dict)


@dataclass
class RiskDecision:
    approved: bool
    qty: float = 0.0
    notional: float = 0.0
    stop_px: float = 0.0
    take_px: float = 0.0
    leverage: float = 1.0
    instrument: str = "spot"
    reasons: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
