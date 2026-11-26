"""Бумажное исполнение: ордер идёт по стакану, полученному ПОСЛЕ задержки (без lookahead)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict
from typing import Awaitable, Callable

from ..config import ExecutionCfg
from ..db import Database
from ..models import OrderBook
from .base import Executor, Fill, OrderRequest, OrderResult

log = logging.getLogger("bot.exec")
BookProvider = Callable[[str], Awaitable[OrderBook]]


class PaperExecutor(Executor):
    def __init__(self, db: Database, book_provider: BookProvider, cfg: ExecutionCfg, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.db, self.book, self.cfg, self.clock, self.sleep = db, book_provider, cfg, clock, sleep

    def _stored(self, client_id: str):
        r = self.db.one("SELECT req_json FROM orders WHERE client_id=?", (client_id,))
        if not r:
            return None
        d = json.loads(r["req_json"])["result"]
        return OrderResult(d["status"], [Fill(**f) for f in d["fills"]], d.get("reject_reason", ""))

    def _persist(self, req: OrderRequest, res: OrderResult) -> None:
        oid = uuid.uuid4().hex
        payload = json.dumps({"req": asdict(req), "result": {"status": res.status, "reject_reason": res.reject_reason,
                                                              "fills": [asdict(f) for f in res.fills]}}, ensure_ascii=False)
        with self.db.tx():
            self.db.execute("INSERT INTO orders (id, client_id, ts, symbol, instrument, side, type, qty, status, strategy_id, "
                            "bet_id, reason, req_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (oid, req.client_id, self.clock(), req.symbol, req.instrument, req.side, "market", req.qty,
                             res.status, req.strategy_id, req.bet_id, res.reject_reason or req.reason, payload))
            for f in res.fills:
                self.db.execute("INSERT INTO fills (id, order_id, ts, symbol, side, price, qty, fee, slippage_bps, ref_price, "
                                "liquidity) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (f.id, oid, f.ts, req.symbol, req.side, f.price, f.qty, f.fee, f.slippage_bps, req.ref_price, f.liquidity))

    def _reject(self, req: OrderRequest, why: str) -> OrderResult:
        res = OrderResult("rejected", [], why)
        self._persist(req, res)
        return res

    async def submit(self, req: OrderRequest) -> OrderResult:
        prev = self._stored(req.client_id)
        if prev is not None:
            return prev
        c = self.cfg
        if req.qty <= 0:
            return self._reject(req, "qty<=0")
        if not req.reduce_only and req.qty * req.ref_price < c.min_notional_usd:
            return self._reject(req, "notional_below_min")
        if c.latency_ms > 0:
            await self.sleep(c.latency_ms / 1000)
        try:
            book = await self.book(req.symbol)
        except Exception as e:
            return self._reject(req, f"no_book: {type(e).__name__}")
        if not book.bids or not book.asks:
            return self._reject(req, "empty_book")
        liquidity = "taker"
        if req.reduce_only:
            w = book.walk(req.side, req.qty, None)
        else:
            avail = book.walk(req.side, 1e18, c.max_slippage_bps).filled
            allowed = min(req.qty, avail * c.max_participation)
            if allowed < req.qty * c.min_fill_ratio:
                return self._reject(req, "insufficient_liquidity")
            # Maker-First (Post-Only): если стакан не убегает резко от нас, моделируем пассивный вход по лучшей цене
            sgn = 1.0 if req.side == "buy" else -1.0
            signed_obi = sgn * book.imbalance(10)
            if req.maker_preferred and signed_obi < 0.45 and book.best_bid > 0 and book.best_ask > 0:
                px = book.best_bid if req.side == "buy" else book.best_ask
                from ..models import WalkResult
                w = WalkResult(allowed, px, allowed * px, 1, "")
                liquidity = "maker"
            else:
                w = book.walk(req.side, allowed, c.max_slippage_bps)
        if w.filled <= 0:
            return self._reject(req, "no_fill")
        if liquidity == "maker":
            bps = c.perp_maker_fee_bps if req.instrument == "perp" else c.maker_fee_bps
        else:
            bps = c.perp_taker_fee_bps if req.instrument == "perp" else c.taker_fee_bps
        fee = w.notional * bps / 1e4
        ref = req.ref_price or book.mid
        slip = ((w.avg_px / ref - 1) if req.side == "buy" else (1 - w.avg_px / ref)) * 1e4
        fill = Fill(uuid.uuid4().hex, self.clock(), w.avg_px, w.filled, fee, slip, liquidity=liquidity)
        status = "filled" if w.filled >= req.qty * (1 - 1e-9) else "partial"
        res = OrderResult(status, [fill])
        self._persist(req, res)
        return res
