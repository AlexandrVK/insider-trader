"""Портфель: баланс, позиции, комиссии, фандинг, MAE/MFE, суб-портфели, периоды и стопы."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from .db import Database

DAY = 86400
WEEK = 7 * DAY


def day_start(ts: float) -> float:
    return ts // DAY * DAY


def week_start(ts: float) -> float:
    return (ts - 4 * DAY) // WEEK * WEEK + 4 * DAY


@dataclass
class Position:
    id: str
    bet_id: str
    strategy_id: str
    symbol: str
    exec_symbol: str
    cluster: str
    instrument: str
    side: int
    qty: float
    entry_px: float
    entry_ts: float
    stop_px: float
    take_px: float
    margin: float
    leverage: float
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    mae_px: float = 0.0
    mfe_px: float = 0.0
    horizon_ts: float = 0.0
    status: str = "open"
    last_funding_ts: float = 0.0
    close_ts: float = 0.0
    close_px: float = 0.0
    exit_reason: str = ""
    realized_pnl: float = 0.0

    def notional(self, px: float) -> float:
        return self.qty * px

    def unrealized(self, px: float) -> float:
        return self.side * (px - self.entry_px) * self.qty

    def value(self, px: float) -> float:
        return self.qty * px if self.instrument == "spot" else self.margin + self.unrealized(px)


class Portfolio:
    def __init__(self, db: Database, initial_capital: float, allocations: Optional[dict] = None,
                 clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: dict[str, Position] = {}
        self.allocations = dict(allocations or {})
        self.peak_equity = initial_capital
        self.halted, self.halt_reason = False, ""
        self.paused_until, self.pause_reason = 0.0, ""
        now = clock()
        self.day_start_ts, self.day_start_equity = day_start(now), initial_capital
        self.week_start_ts, self.week_start_equity = week_start(now), initial_capital
        self._closed_realized: dict[str, float] = {}
        self.load()

    def load(self) -> bool:
        st = self.db.one("SELECT * FROM portfolio_state WHERE id=1")
        if not st:
            return False
        self.cash, self.initial_capital, self.peak_equity = st["cash"], st["initial_capital"], st["peak_equity"]
        self.halted, self.halt_reason = bool(st["halted"]), st["halt_reason"] or ""
        self.paused_until, self.pause_reason = st["paused_until"] or 0.0, st["pause_reason"] or ""
        self.day_start_ts, self.day_start_equity = st["day_start_ts"], st["day_start_equity"]
        self.week_start_ts, self.week_start_equity = st["week_start_ts"], st["week_start_equity"]
        self.positions = {r["id"]: Position(**{k: r[k] for k in Position.__dataclass_fields__})
                          for r in self.db.query("SELECT * FROM positions WHERE status='open'")}
        self._closed_realized = {r["strategy_id"]: r["s"] or 0.0
                                 for r in self.db.query("SELECT strategy_id, SUM(realized_pnl) AS s FROM positions WHERE status='closed' GROUP BY strategy_id")}
        return True

    def save(self, closed: Optional[list] = None) -> None:
        with self.db.tx():
            for p in list(self.positions.values()) + list(closed or []):
                self.db.upsert("positions", {k: getattr(p, k) for k in Position.__dataclass_fields__})
            self.db.upsert("portfolio_state", {
                "id": 1, "cash": self.cash, "initial_capital": self.initial_capital, "peak_equity": self.peak_equity,
                "halted": int(self.halted), "halt_reason": self.halt_reason, "paused_until": self.paused_until,
                "pause_reason": self.pause_reason, "day_start_ts": self.day_start_ts, "day_start_equity": self.day_start_equity,
                "week_start_ts": self.week_start_ts, "week_start_equity": self.week_start_equity, "updated_ts": self.clock()})

    def open_position(self, *, bet_id: str, strategy_id: str, symbol: str, exec_symbol: str, cluster: str, instrument: str,
                      side: int, qty: float, px: float, fee: float, stop_px: float, take_px: float, leverage: float,
                      horizon_ts: float, ts: Optional[float] = None) -> Position:
        if instrument == "spot" and side < 0:
            raise ValueError("шорт на споте невозможен: используйте перп")
        margin = 0.0 if instrument == "spot" else qty * px / leverage
        cost = qty * px if instrument == "spot" else margin
        if cost + fee > self.cash + 1e-9:
            raise ValueError("недостаточно свободного баланса")
        self.cash -= cost + fee
        ts = ts or self.clock()
        p = Position(uuid.uuid4().hex, bet_id, strategy_id, symbol, exec_symbol, cluster, instrument, side, qty, px, ts,
                     stop_px, take_px, margin, leverage, fees_paid=fee, mae_px=px, mfe_px=px, horizon_ts=horizon_ts,
                     last_funding_ts=ts)
        self.positions[p.id] = p
        return p

    def close_position(self, pid: str, qty: float, px: float, fee: float, reason: str, ts: Optional[float] = None):
        p = self.positions[pid]
        q = min(qty, p.qty)
        share = q / p.qty
        gross = p.side * (px - p.entry_px) * q
        if p.instrument == "spot":
            self.cash += q * px - fee
        else:
            self.cash += p.margin * share + gross - fee
        net = gross - fee - p.fees_paid * share - p.funding_paid * share
        p.realized_pnl += net
        p.fees_paid -= p.fees_paid * share
        p.funding_paid -= p.funding_paid * share
        p.margin -= p.margin * share
        p.qty -= q
        full = p.qty <= 1e-12
        if full:
            p.status, p.close_ts, p.close_px, p.exit_reason, p.qty = "closed", ts or self.clock(), px, reason, 0.0
            del self.positions[pid]
            self._closed_realized[p.strategy_id] = self._closed_realized.get(p.strategy_id, 0.0) + p.realized_pnl
        return p, net, full

    def apply_funding(self, pid: str, rate: float, mark: float, ts: float) -> float:
        p = self.positions[pid]
        if p.instrument != "perp":
            return 0.0
        payment = p.side * p.qty * mark * rate
        self.cash -= payment
        p.funding_paid += payment
        p.last_funding_ts = ts
        return payment

    def update_extremes(self, pid: str, low: float, high: float) -> None:
        p = self.positions[pid]
        p.mae_px = min(p.mae_px, low) if p.side > 0 else max(p.mae_px, high)
        p.mfe_px = max(p.mfe_px, high) if p.side > 0 else min(p.mfe_px, low)

    def _px(self, p: Position, prices: dict) -> float:
        return prices.get(p.exec_symbol, prices.get(p.symbol, p.entry_px))

    def equity(self, prices: dict) -> float:
        return self.cash + sum(p.value(self._px(p, prices)) for p in self.positions.values())

    def unrealized(self, prices: dict) -> float:
        return sum(p.unrealized(self._px(p, prices)) for p in self.positions.values())

    def exposure(self, prices: dict) -> float:
        return sum(p.notional(self._px(p, prices)) for p in self.positions.values())

    def strategy_exposure(self, sid: str, prices: dict) -> float:
        return sum(p.notional(self._px(p, prices)) for p in self.positions.values() if p.strategy_id == sid)

    def cluster_exposure(self, cluster: str, prices: dict) -> tuple[int, float]:
        ps = [p for p in self.positions.values() if p.cluster == cluster and cluster]
        return len(ps), sum(p.notional(self._px(p, prices)) for p in ps)

    def weight(self, sid: str) -> float:
        return self.allocations.get(sid, 1.0 / (len(self.allocations) or 1))

    def strategy_equity(self, sid: str, prices: dict) -> float:
        open_real = sum(p.realized_pnl for p in self.positions.values() if p.strategy_id == sid)
        unreal = sum(p.unrealized(self._px(p, prices)) for p in self.positions.values() if p.strategy_id == sid)
        return self.weight(sid) * self.initial_capital + self._closed_realized.get(sid, 0.0) + open_real + unreal

    def strategy_pnl(self, prices: dict) -> dict:
        sids = set(self.allocations) | {p.strategy_id for p in self.positions.values()} | set(self._closed_realized)
        return {s: round(self.strategy_equity(s, prices) - self.weight(s) * self.initial_capital, 4) for s in sids}

    def roll_periods(self, now: float, equity: float) -> None:
        if now >= self.day_start_ts + DAY:
            self.day_start_ts, self.day_start_equity = day_start(now), equity
        if now >= self.week_start_ts + WEEK:
            self.week_start_ts, self.week_start_equity = week_start(now), equity

    def drawdown(self, equity: float) -> float:
        return max(0.0, (self.peak_equity - equity) / self.peak_equity) if self.peak_equity else 0.0
