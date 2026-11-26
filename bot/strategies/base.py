from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from ..models import Candle, Signal


class Strategy(ABC):
    id: str = "base"
    version: str = "1"
    timeframe: str = "1h"
    min_candles: int = 60
    n_params: int = 2

    @abstractmethod
    def generate(self, symbol: str, candles: Sequence[Candle], now: float) -> Optional[Signal]: ...
