"""Копитрейдинг (Этап 5): CopySource и Hyperliquid public API (адаптировано из insider-trade)."""
from __future__ import annotations

import codecs
import gc
import json
import math
import threading
import time
from abc import ABC, abstractmethod
from typing import Iterable, Iterator

import requests

from ..models import Signal

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def wilson_lower_bound(hits: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = hits / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n)


class RateLimiter:
    def __init__(self, per_min: float):
        self.rate, self.cap = per_min / 60.0, float(per_min)
        self.tokens, self.t, self.lock = float(per_min), time.time(), threading.Lock()

    def take(self, weight: float) -> None:
        weight = min(weight, self.cap)
        while True:
            with self.lock:
                now = time.time()
                self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                wait = (weight - self.tokens) / self.rate
            time.sleep(min(wait, 2.0))


def _iter_leaderboard_rows(chunks: Iterable[bytes]) -> Iterator[dict]:
    """Потоковый парсер массива leaderboardRows (файл ~40 МБ) без загрузки всех 45k+ записей в RAM."""
    dec = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")(errors="ignore")
    buf = ""
    in_rows = False
    for chunk in chunks:
        if not chunk:
            continue
        buf += utf8.decode(chunk, final=False)
        if not in_rows:
            idx = buf.find('"leaderboardRows"')
            if idx == -1:
                buf = buf[-64:]
                continue
            br = buf.find("[", idx)
            if br == -1:
                continue
            buf = buf[br + 1:]
            in_rows = True
        pos = 0
        n = len(buf)
        while pos < n:
            while pos < n and buf[pos] in " \r\n\t,":
                pos += 1
            if pos >= n:
                break
            if buf[pos] == "]":
                return
            try:
                row, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                break
            pos = end
            if isinstance(row, dict):
                yield row
        buf = buf[pos:]
    tail = utf8.decode(b"", final=True)
    if in_rows and (buf or tail):
        buf += tail
        pos, n = 0, len(buf)
        while pos < n:
            while pos < n and buf[pos] in " \r\n\t,":
                pos += 1
            if pos >= n or buf[pos] == "]":
                break
            try:
                row, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                break
            pos = end
            if isinstance(row, dict):
                yield row


class CopySource(ABC):
    @abstractmethod
    def refresh_traders(self) -> list[dict]: ...

    @abstractmethod
    def poll_signals(self, now: float) -> list[Signal]: ...


class HyperliquidCopySource(CopySource):
    def __init__(self, tracked_max: int = 10, min_score: float = 0.5, copy_delay_min: int = 3):
        self.tracked_max = tracked_max
        self.min_score = min_score
        self.copy_delay_min = copy_delay_min
        self.limiter = RateLimiter(400)
        self.session = requests.Session()
        self.tracked: list[dict] = []
        self._prev_positions: dict[str, dict[str, float]] = {}

    def _post(self, payload: dict, weight: float = 2.0) -> dict:
        self.limiter.take(weight)
        r = self.session.post(INFO_URL, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def score_trader_history(self, hits: int, total: int, market_baseline: float = 0.5) -> float:
        return max(0.0, round(wilson_lower_bound(hits, total) - max(0.0, market_baseline - 0.5), 4))

    def refresh_traders(self) -> list[dict]:
        cands = []
        with self.session.get(LEADERBOARD_URL, timeout=30, stream=True) as r:
            r.raise_for_status()
            for row in _iter_leaderboard_rows(r.iter_content(chunk_size=65536)):
                acc = float(row.get("accountValue") or 0)
                if acc < 50_000:
                    continue
                m_pnl, m_roi, m_vlm = 0.0, 0.0, 0.0
                w_pnl, a_pnl = 0.0, 0.0
                for w_name, vals in row.get("windowPerformances", []):
                    if w_name == "month":
                        m_pnl, m_roi, m_vlm = float(vals["pnl"]), float(vals["roi"]), float(vals.get("vlm") or 0)
                    elif w_name == "week":
                        w_pnl = float(vals["pnl"])
                    elif w_name == "allTime":
                        a_pnl = float(vals["pnl"])
                turnover = m_vlm / acc if acc else 0.0
                if m_pnl <= 0 or a_pnl <= 0 or m_roi < 0.05 or turnover < 1.0 or turnover > 1500.0:
                    continue
                # Многооконный скоринг устойчивости: месяц + неделя + allTime + умеренный оборот (отсекает лудоманов)
                consistency = (1.0 if w_pnl > 0 else 0.6) * (1.0 / (1.0 + turnover / 400.0))
                score = round(math.log1p(m_pnl) * min(m_roi, 2.0) * consistency, 4)
                cands.append({
                    "addr": row["ethAddress"], "account": acc, "month_pnl": m_pnl,
                    "turnover": round(turnover, 1), "score": score,
                })
        cands.sort(key=lambda x: -x["score"])
        self.tracked = cands[: self.tracked_max]
        del cands
        gc.collect()
        return self.tracked

    def fetch_positions(self, addr: str) -> dict[str, float]:
        d = self._post({"type": "clearinghouseState", "user": addr}, weight=2.0)
        out: dict[str, float] = {}
        for ap in d.get("assetPositions", []):
            p = ap["position"]
            szi = float(p["szi"])
            if szi:
                out[p["coin"]] = szi
        return out

    def poll_signals(self, now: float) -> list[Signal]:
        votes: dict[tuple[str, str], list[str]] = {}
        for t in self.tracked:
            addr = t["addr"]
            try:
                cur = self.fetch_positions(addr)
            except Exception:
                continue
            prev = self._prev_positions.get(addr)
            self._prev_positions[addr] = cur
            if prev is None:
                continue
            for coin, szi in cur.items():
                old = prev.get(coin, 0.0)
                if (old == 0.0 and szi != 0.0) or (old * szi < 0):
                    direction = "long" if szi > 0 else "short"
                    votes.setdefault((f"{coin}/USDT", direction), []).append(addr[:10])

        signals: list[Signal] = []
        for (sym, direction), addrs in votes.items():
            if len(addrs) >= 2:
                signals.append(Signal(
                    strategy_id="copy_hyperliquid", strategy_version="1.0", symbol=sym,
                    direction=direction, confidence=min(0.75, 0.5 + 0.08 * len(addrs)),
                    timeframe="1h", horizon_min=24 * 60, stop_atr_mult=2.0, take_atr_mult=3.5,
                    ts=now, thesis=f"Hyperliquid copy: {len(addrs)} трейдеров открыли {direction}",
                    evidence=addrs, sources=["hyperliquid_copy"],
                ))
        return signals
