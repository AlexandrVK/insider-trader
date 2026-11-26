"""Торговый движок (paper): объединяет Этапы 1–6 (биржи, стратегии, MetaSelector, ИИ, SourceRegistry, CopyTrading, Telegram)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Callable, Optional

from .analysis.ai import AIAnalystSuite, LLMProvider
from .config import Settings
from .copytrading.hyperliquid import HyperliquidCopySource
from .data.news import RSS_FEEDS, fetch_fear_and_greed, fetch_fred_series, fetch_rss
from .data.onchain import fetch_btc_mempool_fees, fetch_defillama_tvl
from .db import Database
from .exchange.adapter import ExchangeAdapter, Unavailable
from .execution.base import Executor, OrderRequest
from .execution.paper import PaperExecutor
from .journal import Journal
from .market.data import TF_SEC, MarketData
from .market.indicators import detect_regime
from .market.universe import UniverseResult, select_universe
from .notify.telegram import TelegramNotifier
from .portfolio import Portfolio, Position
from .risk import RiskEngine
from .signals.aggregator import SignalAggregator
from .sources.registry import SourceRegistry
from .strategies.meta_selector import MetaSelector
from .strategies.pool import build_strategies

log = logging.getLogger("bot.engine")


class TradingEngine:
    def __init__(self, cfg: Settings, db: Database, hub: ExchangeAdapter, clock: Callable[[], float] = time.time,
                 sleep: Callable = asyncio.sleep, executor: Optional[Executor] = None, stream=None):
        self.cfg, self.db, self.hub, self.clock = cfg, db, hub, clock
        self.strategies = build_strategies(cfg.strategies)
        self.meta = MetaSelector(
            db, [s.id for s in self.strategies],
            n_params_map={s.id: getattr(s, "n_params", 2) for s in self.strategies},
            exploration_floor=cfg.strategies.exploration_floor,
            rebalance_days=cfg.strategies.rebalance_days,
            clock=clock,
        )
        alloc = cfg.strategies.allocations or self.meta.weights
        self.md = MarketData(hub, db, clock, stream=stream)
        self.pf = Portfolio(db, cfg.capital.initial_usdt, alloc, clock)
        self.journal = Journal(db, clock)
        self.risk = RiskEngine(cfg, self.pf, self.journal, clock)
        self.executor = executor or PaperExecutor(db, lambda sym: hub.fetch_order_book(sym, 50), cfg.execution, clock, sleep)
        self.sources = SourceRegistry(db, clock=clock)
        self.sources.register_defaults()
        self.llm = LLMProvider(cache_ttl_sec=cfg.ai.cache_ttl_sec) if cfg.ai.enabled else None
        self.ai_suite = AIAnalystSuite(self.llm)
        self.aggregator = SignalAggregator()
        self.copy_source = (
            HyperliquidCopySource(cfg.copytrading.tracked_max, cfg.copytrading.min_score, cfg.copytrading.copy_delay_min)
            if cfg.copytrading.enabled else None
        )
        self.notifier = TelegramNotifier()
        self.universe: Optional[UniverseResult] = None
        self._uni_ts = 0.0
        self.prices: dict[str, float] = {}
        self._next_poll: dict[tuple, float] = {}
        self._bar_done: dict[tuple, int] = {}
        self._last_snapshot = 0.0

        # Кэш внешних данных (новости, макро, ончейн, копитрейдинг)
        self._last_ext_ts = 0.0
        self._last_copy_refresh_ts = 0.0
        self._last_copy_poll_ts = 0.0
        self._headlines: list[tuple[str, str]] = []  # (source_id, title)
        self._fear_greed: Optional[int] = None
        self._fed_rate: Optional[float] = None
        self._tvl_chg_pct: Optional[float] = None
        self._btc_fee_sat: Optional[float] = None
        self._halt_notified = False

        self.stats = {"cycles": 0, "signals": 0, "rejected": 0, "opened": 0, "closed": 0, "errors": 0}
        self.last_cycle_ts = 0.0
        self.reconcile()

    def reconcile(self) -> None:
        open_ids = {p.bet_id for p in self.pf.positions.values()}
        for b in self.db.query("SELECT id FROM bets WHERE status='open'"):
            if b["id"] not in open_ids:
                self.db.execute("UPDATE bets SET status='lost_state' WHERE id=?", (b["id"],))

    async def _notify(self, text: str) -> None:
        if self.notifier.enabled:
            try:
                await asyncio.to_thread(self.notifier.send, text)
            except Exception:
                pass

    async def _safe(self, name: str, fn) -> None:
        try:
            await fn()
        except Unavailable as e:
            log.warning("шаг пропущен: биржи недоступны", extra={"step": name, "error": str(e)[:200]})
        except Exception:
            self.stats["errors"] += 1
            log.exception("ошибка шага цикла", extra={"step": name})

    def equity(self) -> float:
        return self.pf.equity(self.prices)

    async def refresh_universe(self, force: bool = False) -> None:
        now = self.clock()
        if not force and self.universe and now - self._uni_ts < self.cfg.universe.refresh_minutes * 60:
            return
        self.universe = await select_universe(self.hub, self.cfg.universe, now)
        self._uni_ts = now
        self.db.set_meta("universe", self.universe.to_json())

    def _symbols(self) -> list[str]:
        syms = list(self.universe.symbols) if self.universe else []
        for p in self.pf.positions.values():
            if p.symbol not in syms:
                syms.append(p.symbol)
        return syms

    async def refresh_data(self) -> None:
        now = self.clock()
        sem = asyncio.Semaphore(4)
        tfs = sorted({s.timeframe for s in self.strategies})

        async def one(sym: str, tf: str):
            key = (sym, tf)
            if now < self._next_poll.get(key, 0):
                return
            async with sem:
                try:
                    await self.md.refresh_candles(sym, tf, 300)
                    sec = TF_SEC[tf]
                    self._next_poll[key] = (now // sec + 1) * sec + 5
                except Unavailable:
                    self._next_poll[key] = now + 30
                except Exception:
                    self.stats["errors"] += 1
                    self._next_poll[key] = now + 60

        await asyncio.gather(*(one(s, tf) for s in self._symbols() for tf in tfs))

    async def refresh_external_data(self) -> None:
        """Раз в 15 минут обновляет бесплатные новости, макро и ончейн с соблюдением лимитов SourceRegistry."""
        if not self.cfg.ai.enabled:
            return
        now = self.clock()
        if now - self._last_ext_ts < 900.0:
            return
        self._last_ext_ts = now

        # 1. RSS-новости с дедупликацией
        for feed_key, url in RSS_FEEDS.items():
            sid = f"{feed_key}_rss"
            if sid in self.sources.sources and not self.sources.can_request(sid):
                continue
            try:
                items = await asyncio.to_thread(fetch_rss, sid, url, 8)
                for it in items:
                    if not self.sources.is_duplicate(it.title):
                        self._headlines.append((sid, it.title))
                self._headlines = self._headlines[-80:]
            except Exception:
                self.sources.record_failure(sid)

        # 2. Fear & Greed
        if self.sources.can_request("alternative_me_fng"):
            try:
                fng = await asyncio.to_thread(fetch_fear_and_greed)
                if fng:
                    self._fear_greed = fng["value"]
            except Exception:
                self.sources.record_failure("alternative_me_fng")

        # 3. FRED (если задан бесплатный ключ)
        if self.sources.can_request("fred"):
            try:
                val = await asyncio.to_thread(fetch_fred_series, "DFF")
                if val is not None:
                    self._fed_rate = val
            except Exception:
                self.sources.record_failure("fred")

        # 4. DeFiLlama TVL & mempool.space BTC fees
        if self.sources.can_request("defillama"):
            try:
                tvl = await asyncio.to_thread(fetch_defillama_tvl)
                if tvl:
                    self._tvl_chg_pct = tvl["change_24h_pct"]
            except Exception:
                self.sources.record_failure("defillama")

        if self.sources.can_request("mempool_space"):
            try:
                fees = await asyncio.to_thread(fetch_btc_mempool_fees)
                if fees:
                    self._btc_fee_sat = fees["fastest_sat_vb"]
            except Exception:
                self.sources.record_failure("mempool_space")

    async def poll_copytrading(self) -> None:
        """Обновляет список трейдеров Hyperliquid раз в сутки и опрашивает их позиции раз в минуту."""
        if not self.copy_source:
            return
        now = self.clock()
        if now - self._last_copy_refresh_ts >= self.cfg.copytrading.refresh_hours * 3600:
            self._last_copy_refresh_ts = now
            if self.sources.can_request("hyperliquid_copy"):
                try:
                    tracked = await asyncio.to_thread(self.copy_source.refresh_traders)
                    log.info("Hyperliquid copytrading: обновлено трейдеров", extra={"count": len(tracked)})
                except Exception:
                    self.sources.record_failure("hyperliquid_copy")
                    return

        if not self.copy_source.tracked or (now - self._last_copy_poll_ts < 60.0):
            return
        self._last_copy_poll_ts = now
        if not self.sources.can_request("hyperliquid_copy"):
            return
        try:
            copy_signals = await asyncio.to_thread(self.copy_source.poll_signals, now)
        except Exception:
            self.sources.record_failure("hyperliquid_copy")
            return

        uni_syms = set(self.universe.symbols) if self.universe else set()
        for sig in copy_signals:
            if uni_syms and sig.symbol not in uni_syms:
                continue
            self.stats["signals"] += 1
            await self._handle_signal(sig, now)

    async def update_marks(self) -> None:
        syms = self._symbols()
        if not syms:
            return
        tickers = await self.hub.fetch_tickers(syms)
        self.md.ingest_tickers(tickers)
        for s, t in tickers.items():
            self.prices[s] = t.mid

    async def manage_positions(self) -> None:
        now = self.clock()
        for p in list(self.pf.positions.values()):
            px = self.prices.get(p.exec_symbol) or self.prices.get(p.symbol)
            if not px:
                continue
            self.pf.update_extremes(p.id, px, px)
            reason = None
            if (p.side > 0 and px <= p.stop_px) or (p.side < 0 and px >= p.stop_px):
                reason = "stop"
            elif p.take_px and ((p.side > 0 and px >= p.take_px) or (p.side < 0 and px <= p.take_px)):
                reason = "take_profit"
            elif p.horizon_ts and now >= p.horizon_ts:
                reason = "horizon"
            if reason:
                await self._close(p, px, reason, now)

    async def _close(self, p: Position, px: float, reason: str, now: float) -> None:
        attempt = self.db.one("SELECT COUNT(*) AS n FROM orders WHERE client_id LIKE ?", (f"exit:{p.id}:%",))["n"]
        req = OrderRequest(client_id=f"exit:{p.id}:{attempt}", symbol=p.exec_symbol, instrument=p.instrument,
                           side="sell" if p.side > 0 else "buy", qty=p.qty, ref_price=px, strategy_id=p.strategy_id,
                           bet_id=p.bet_id, reduce_only=True, reason=reason)
        res = await self.executor.submit(req)
        if res.qty <= 0:
            return
        entry_notional = p.qty * p.entry_px
        with self.db.tx():
            pos, _, full = self.pf.close_position(p.id, res.qty, res.avg_px, res.fee, reason, now)
            if full:
                mae = pos.side * (pos.mae_px - pos.entry_px) / pos.entry_px * 100
                mfe = pos.side * (pos.mfe_px - pos.entry_px) / pos.entry_px * 100
                bet = self.db.one("SELECT fees, notional FROM bets WHERE id=?", (pos.bet_id,)) or {"fees": 0.0, "notional": entry_notional}
                self.journal.close_bet(pos.bet_id, res.avg_px, now, reason, pos.realized_pnl, bet["notional"] or entry_notional,
                                       mae, mfe, (bet["fees"] or 0.0) + res.fee, pos.funding_paid)
            self.pf.save(closed=[pos] if full else None)
        if full:
            self.stats["closed"] += 1
            await self._notify(
                f"CLOSE {pos.symbol} ({'LONG' if pos.side > 0 else 'SHORT'}) | "
                f"PnL: {pos.realized_pnl:+.2f} USDT | reason: {reason} | strat: {pos.strategy_id}"
            )

    def _run_ai_review(self, sig):
        base_coin = sig.symbol.split("/")[0].lower()
        rel_items = [(sid, title) for sid, title in self._headlines if base_coin in title.lower() or "bitcoin" in title.lower() or "crypto" in title.lower()]
        headlines = [t for _, t in rel_items[-10:]]
        src_ids = sorted({sid for sid, _ in rel_items[-10:]}) or ["local_lexicon"]
        ai_signals = [
            self.ai_suite.analyze_news(sig.symbol, headlines, src_ids),
            self.ai_suite.analyze_macro(sig.symbol, self._fear_greed, self._fed_rate),
            self.ai_suite.analyze_onchain(sig.symbol, self._tvl_chg_pct, self._btc_fee_sat),
            self.ai_suite.risk_critic(sig.symbol, sig.direction, headlines),
        ]
        src_weights = self.sources.sample_weights()
        return self.aggregator.aggregate(sig, ai_signals, source_weights=src_weights)

    async def generate_and_trade(self) -> None:
        if not self.universe:
            return
        now = self.clock()
        btc_cs = self.md.closed_candles("BTC/USDT", self.cfg.strategies.timeframe, 60)
        regime = detect_regime(btc_cs) if btc_cs else ""
        self.pf.allocations = self.meta.update_weights(regime=regime)

        for strat in self.strategies:
            for sym in self.universe.symbols:
                cs = self.md.closed_candles(sym, strat.timeframe, 300)
                if len(cs) < strat.min_candles:
                    continue
                key = (strat.id, sym)
                done = self._bar_done.get(key)
                if done is None:
                    m = self.db.get_meta(f"bar:{strat.id}:{sym}")
                    done = int(m) if m else 0
                if cs[-1].ts <= done:
                    continue
                self._bar_done[key] = cs[-1].ts
                self.db.set_meta(f"bar:{strat.id}:{sym}", str(cs[-1].ts))
                sig = strat.generate(sym, cs, now)
                if sig is None:
                    continue
                self.stats["signals"] += 1
                await self._handle_signal(sig, now)

    async def _handle_signal(self, sig, now: float) -> None:
        # Прогон через ИИ-аналитиков и агрегатор (кроме контрольного случайного бенчмарка)
        if self.cfg.ai.enabled and not sig.benchmark:
            try:
                merged = await asyncio.to_thread(self._run_ai_review, sig)
                if merged is None:
                    self.stats["rejected"] += 1
                    self.journal.reject(sig, ["ai_veto_or_conflict"], None)
                    return
                sig = merged
            except Exception as e:
                log.warning("ИИ-проверка пропущена из-за ошибки", extra={"error": str(e)[:120]})

        inst = "spot" if sig.direction == "long" and self.cfg.instruments.spot else "perp"
        exec_sym = sig.symbol if inst == "spot" else self.hub.perp_symbol(sig.symbol)
        cluster = self.universe.clusters.get(sig.symbol, "") if self.universe else ""
        if exec_sym is None:
            self.stats["rejected"] += 1
            self.journal.reject(sig, ["perp_unavailable"], None)
            return
        try:
            ctx = await self.md.context(sig.symbol, sig.timeframe, inst, cluster, book_symbol=exec_sym)
            if inst == "perp":
                fr = await self.hub.fetch_funding_rate(exec_sym)
                ctx.funding_rate = fr.rate if fr else 0.0
        except Unavailable as e:
            self.stats["rejected"] += 1
            self.journal.reject(sig, [f"no_market_data: {str(e)[:80]}"], None)
            return
        dec = self.risk.evaluate(sig, ctx, self.prices)
        if not dec.approved:
            self.stats["rejected"] += 1
            self.journal.reject(sig, dec.reasons, ctx, dec.checks)
            return
        bet_id = uuid.uuid4().hex
        req = OrderRequest(client_id=f"entry:{bet_id}", symbol=exec_sym, instrument=inst,
                           side="buy" if sig.direction == "long" else "sell", qty=dec.qty, ref_price=ctx.price,
                           strategy_id=sig.strategy_id, bet_id=bet_id, reason=sig.thesis[:100])
        res = await self.executor.submit(req)
        if res.qty <= 0:
            self.stats["rejected"] += 1
            self.journal.reject(sig, [f"exec: {res.reject_reason}"], ctx, dec.checks)
            return
        side = 1 if sig.direction == "long" else -1
        try:
            with self.db.tx():
                self.journal.open_bet(bet_id, sig, dec, ctx, res.avg_px, res.qty, res.fee, res.slippage_bps)
                self.pf.open_position(bet_id=bet_id, strategy_id=sig.strategy_id, symbol=sig.symbol, exec_symbol=exec_sym,
                                      cluster=cluster, instrument=inst, side=side, qty=res.qty, px=res.avg_px, fee=res.fee,
                                      stop_px=dec.stop_px, take_px=dec.take_px, leverage=dec.leverage,
                                      horizon_ts=now + sig.horizon_min * 60, ts=now)
                self.pf.save()
        except ValueError as e:
            self.stats["rejected"] += 1
            self.journal.reject(sig, [f"portfolio: {e}"], ctx, dec.checks)
            return
        self.stats["opened"] += 1
        self.prices.setdefault(exec_sym, ctx.price)
        await self._notify(
            f"OPEN {sig.symbol} ({sig.direction.upper()}, {inst}) | px={res.avg_px:.4f} | "
            f"sl={dec.stop_px:.4f} | strat={sig.strategy_id} | ai={sig.ai_assisted}"
        )

    def snapshot(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_snapshot < self.cfg.monitoring.snapshot_minutes * 60:
            return
        eq = self.equity()
        self.db.execute("INSERT OR REPLACE INTO snapshots (ts, equity, cash, exposure, drawdown, strategy_pnl_json) VALUES (?,?,?,?,?,?)",
                        (now, eq, self.pf.cash, self.pf.exposure(self.prices), self.pf.drawdown(eq),
                         json.dumps(self.pf.strategy_pnl(self.prices))))
        self._last_snapshot = now

    async def cycle(self) -> None:
        await self._safe("universe", self.refresh_universe)
        await self._safe("data", self.refresh_data)
        await self._safe("external", self.refresh_external_data)
        await self._safe("marks", self.update_marks)
        self.risk.update_state(self.prices)
        if self.pf.halted and not self._halt_notified:
            self._halt_notified = True
            await self._notify(f"HALT: {self.pf.halt_reason}")
        elif not self.pf.halted:
            self._halt_notified = False
        await self._safe("positions", self.manage_positions)
        await self._safe("copytrading", self.poll_copytrading)
        await self._safe("trade", self.generate_and_trade)
        self.snapshot()
        self.pf.save()
        self.stats["cycles"] += 1
        self.last_cycle_ts = self.clock()

    async def run(self, stop: asyncio.Event) -> None:
        interval = self.cfg.monitoring.loop_interval_sec
        while not stop.is_set():
            await self.cycle()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        self.snapshot(force=True)
        self.pf.save()
