"""Журнал ставок и отклонённых сигналов."""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Optional

from .db import Database
from .models import MarketContext, RiskDecision, Signal


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


class Journal:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def open_bet(self, bet_id: str, sig: Signal, dec: RiskDecision, ctx: MarketContext, entry_px: float, qty: float,
                 fee: float, slippage_bps: float) -> None:
        rationale = sig.thesis or "; ".join(sig.evidence)
        self.db.execute(
            "INSERT INTO bets (id, ts, symbol, instrument, side, strategy_id, strategy_version, benchmark, ai_assisted, "
            "signals_json, source_ids_json, regime, entry_px, stop_px, take_px, horizon_min, qty, notional, fees, "
            "slippage_bps, rationale, status, funding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bet_id, self.clock(), sig.symbol, dec.instrument, sig.direction, sig.strategy_id, sig.strategy_version,
             int(sig.benchmark), int(sig.ai_assisted),
             _j({"signal": sig.model_dump(), "risk": dec.checks, "context": ctx.__dict__}),
             _j(sig.sources), sig.regime, entry_px, dec.stop_px, dec.take_px, sig.horizon_min, qty, qty * entry_px, fee,
             slippage_bps, rationale, "open", 0.0))

    def close_bet(self, bet_id: str, exit_px: float, exit_ts: float, reason: str, pnl: float, notional: float, mae_pct: float,
                  mfe_pct: float, fees_total: float, funding: float) -> None:
        self.db.execute(
            "UPDATE bets SET status='closed', exit_px=?, exit_ts=?, exit_reason=?, pnl=?, pnl_pct=?, mae_pct=?, mfe_pct=?, "
            "fees=?, funding=? WHERE id=?",
            (exit_px, exit_ts, reason, pnl, pnl / notional * 100 if notional else 0.0, mae_pct, mfe_pct, fees_total, funding, bet_id))

    def reject(self, sig: Signal, reasons: list, ctx: Optional[MarketContext], checks: Optional[dict] = None) -> None:
        self.db.execute(
            "INSERT INTO rejected_signals (id, ts, strategy_id, symbol, side, reasons_json, signal_json, context_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, self.clock(), sig.strategy_id, sig.symbol, sig.direction, _j(reasons), _j(sig.model_dump()),
             _j({"context": ctx.__dict__ if ctx else None, "checks": checks or {}})))

    def closed_bets(self, strategy_id: Optional[str] = None, limit: int = 1000) -> list[dict]:
        if strategy_id:
            return self.db.query("SELECT * FROM bets WHERE status='closed' AND strategy_id=? ORDER BY exit_ts DESC LIMIT ?",
                                 (strategy_id, limit))
        return self.db.query("SELECT * FROM bets WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?", (limit,))

    def consecutive_losses(self, strategy_id: str) -> tuple[int, float]:
        n, last = 0, 0.0
        for b in self.closed_bets(strategy_id, 20):
            if b["pnl"] is None or b["pnl"] >= 0:
                break
            n += 1
            last = max(last, b["exit_ts"] or 0.0)
        return n, last
