from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class OrderRequest:
    client_id: str
    symbol: str
    instrument: str
    side: str
    qty: float
    ref_price: float
    strategy_id: str = ""
    bet_id: str = ""
    reduce_only: bool = False
    reason: str = ""
    maker_preferred: bool = False


@dataclass
class Fill:
    id: str
    ts: float
    price: float
    qty: float
    fee: float
    slippage_bps: float
    liquidity: str = "taker"


@dataclass
class OrderResult:
    status: str
    fills: list = field(default_factory=list)
    reject_reason: str = ""

    @property
    def qty(self) -> float:
        return sum(f.qty for f in self.fills)

    @property
    def avg_px(self) -> float:
        q = self.qty
        return sum(f.price * f.qty for f in self.fills) / q if q else 0.0

    @property
    def fee(self) -> float:
        return sum(f.fee for f in self.fills)

    @property
    def slippage_bps(self) -> float:
        q = self.qty
        return sum(f.slippage_bps * f.qty for f in self.fills) / q if q else 0.0


class Executor(ABC):
    @abstractmethod
    async def submit(self, req: OrderRequest) -> OrderResult: ...
