"""Торговый движок (paper): объединяет Этапы 1–6 (биржи, стратегии, MetaSelector, ИИ, SourceRegistry, CopyTrading, Telegram)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from typing import Callable, Optional

import numpy as np

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
from .market.indicators import atr, closes, detect_regime, ema, log_returns, rsi
from .market.universe import UniverseResult, select_universe
from .ml.meta_labeler import MetaLabeler, extract_features
from .models import Signal
from .notify.telegram import TelegramNotifier
from .portfolio import Portfolio, Position
from .risk import RiskEngine
from .signals.aggregator import SignalAggregator
from .sources.registry import SourceRegistry
from .strategies.meta_selector import MetaSelector
from .strategies.pool import MEAN_REV_STRATEGIES, TREND_STRATEGIES, build_strategies

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
        if self.llm is not None:
            self.llm.attach_db(db)
        self.ai_suite = AIAnalystSuite(self.llm)
        self.aggregator = SignalAggregator()
        self.aggregator.calibrator.load_from_db(db)
        self.copy_source = (
            HyperliquidCopySource(cfg.copytrading.tracked_max, cfg.copytrading.min_score, cfg.copytrading.copy_delay_min)
            if cfg.copytrading.enabled else None
        )
        self.meta_labeler = MetaLabeler(min_trades=cfg.features.meta_label_min_trades)
        self.meta_labeler.fit_from_db(db)
        self.notifier = TelegramNotifier()
        self.universe: Optional[UniverseResult] = None
        self._uni_ts = 0.0
        self.prices: dict[str, float] = {}
        self._next_poll: dict[tuple, float] = {}
        self._bar_done: dict[tuple, int] = {}
        self._last_snapshot = 0.0
        self._last_xsec_ts = 0.0
        self._last_funding_scan_ts = 0.0
        self._btc_vol_scalar_val = 1.0
        self._btc_annual_vol = 0.0
        self._last_regime = ""

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
        self._load_features()
        self.reconcile()

    def _load_features(self) -> None:
        raw = self.db.get_meta("features_json")
        if not raw:
            return
        try:
            saved = json.loads(raw)
        except Exception:
            return
        if isinstance(saved, dict):
            valid = self.cfg.features.toggle_dict()
            for k, v in saved.items():
                if k in valid:
                    setattr(self.cfg.features, k, bool(v))

    def feature_enabled(self, name: str) -> bool:
        return bool(getattr(self.cfg.features, name, False))

    def features_state(self) -> dict[str, bool]:
        return self.cfg.features.toggle_dict()

    def set_feature(self, name: str, enabled: bool) -> dict[str, bool]:
        valid = self.cfg.features.toggle_dict()
        if name not in valid:
            raise ValueError(f"неизвестная технология: {name}")
        setattr(self.cfg.features, name, bool(enabled))
        state = self.cfg.features.toggle_dict()
        self.db.set_meta("features_json", json.dumps(state, ensure_ascii=False))
        self.db.log_risk_event("feature_toggle", {"feature": name, "enabled": bool(enabled)}, self.clock())
        if name == "copytrading" and enabled and self.copy_source is None:
            self.copy_source = HyperliquidCopySource(
                self.cfg.copytrading.tracked_max, self.cfg.copytrading.min_score, self.cfg.copytrading.copy_delay_min
            )
        if name == "ai_review" and enabled and self.llm is None:
            self.llm = LLMProvider(cache_ttl_sec=self.cfg.ai.cache_ttl_sec)
            self.llm.attach_db(self.db)
            self.ai_suite = AIAnalystSuite(self.llm)
        return state

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
        tf_set = {s.timeframe for s in self.strategies}
        if self.feature_enabled("mtf_filter") and "4h" in self.cfg.timeframes:
            tf_set.add("4h")
        tfs = sorted(tf_set)

        async def one(sym: str, tf: str):
            key = (sym, tf)
            if now < self._next_poll.get(key, 0):
                return
            async with sem:
                try:
                    lim = 120 if tf == "4h" else 300
                    await self.md.refresh_candles(sym, tf, lim)
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
        if not (self.cfg.ai.enabled and self.feature_enabled("ai_review")):
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
        if not self.copy_source or not self.feature_enabled("copytrading"):
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

    def _mtf_trend(self, symbol: str) -> int:
        """Направление старшего таймфрейма 4h по EMA20/EMA50: +1 (вверх), -1 (вниз), 0 (нейтрально)."""
        cs4 = self.md.closed_candles(symbol, "4h", 70)
        if len(cs4) < 55:
            return 0
        c = closes(cs4)
        e20, e50 = float(ema(c, 20)[-1]), float(ema(c, 50)[-1])
        if e50 <= 0:
            return 0
        ratio = e20 / e50 - 1.0
        if ratio > 0.002:
            return 1
        if ratio < -0.002:
            return -1
        return 0

    def _update_vol_targeting(self) -> float:
        """Этап B: Portfolio Volatility Targeting на базе реализованной 48h волатильности BTC."""
        btc_cs = self.md.closed_candles("BTC/USDT", self.cfg.strategies.timeframe, 60)
        if len(btc_cs) < 30:
            self._btc_vol_scalar_val = 1.0
            return 1.0
        rets = log_returns(closes(btc_cs[-49:]))
        if len(rets) < 20:
            self._btc_vol_scalar_val = 1.0
            return 1.0
        bars_per_year = 365.0 * 86400.0 / TF_SEC.get(self.cfg.strategies.timeframe, 3600)
        ann_vol = float(np.std(rets, ddof=1)) * math.sqrt(bars_per_year)
        self._btc_annual_vol = round(ann_vol, 4)
        if ann_vol <= 1e-6:
            self._btc_vol_scalar_val = 1.0
            return 1.0
        scalar = max(0.35, min(1.25, self.cfg.features.target_annual_vol / ann_vol))
        self._btc_vol_scalar_val = round(scalar, 3)
        return self._btc_vol_scalar_val

    async def manage_positions(self) -> None:
        now = self.clock()
        trailing_on = self.feature_enabled("trailing_stop")
        for p in list(self.pf.positions.values()):
            px = self.prices.get(p.exec_symbol) or self.prices.get(p.symbol)
            if not px:
                continue
            self.pf.update_extremes(p.id, px, px)

            # Начисление / списание 8-часового фандинга для открытых перп-позиций
            if p.instrument == "perp" and now - p.last_funding_ts >= 8 * 3600:
                try:
                    fr = await self.hub.fetch_funding_rate(p.exec_symbol)
                    if fr is not None:
                        self.pf.apply_funding(p.id, fr.rate, px, now)
                except Exception:
                    pass

            # Этап A: Breakeven + Partial Take-Profit (50%) + Chandelier Trailing Stop
            if trailing_on:
                cs = self.md.closed_candles(p.symbol, self.cfg.strategies.timeframe, 30)
                cur_atr = atr(cs, 14) if len(cs) >= 16 else abs(p.entry_px - p.stop_px) / 2.0
                if cur_atr > 0:
                    gain = p.side * (px - p.entry_px)
                    # 1) Перевод в безубыток (+0.15% на покрытие комиссий) после +1.5 ATR
                    if gain >= 1.5 * cur_atr:
                        be_px = p.entry_px * (1.0 + p.side * 0.0015)
                        if (p.side > 0 and be_px > p.stop_px) or (p.side < 0 and be_px < p.stop_px):
                            p.stop_px = be_px

                    # 2) Для трендовых стратегий: частичная фиксация 50% на +2.5 ATR и Chandelier Trailing Stop
                    if p.strategy_id in TREND_STRATEGIES:
                        partial_key = f"partial:{p.id}"
                        partial_done = self.db.get_meta(partial_key) == "1"
                        if not partial_done and gain >= 2.5 * cur_atr and p.qty * px >= self.cfg.execution.min_notional_usd * 2.2:
                            await self._close(p, px, "partial_tp", now, qty=p.qty * 0.5)
                            self.db.set_meta(partial_key, "1")
                            partial_done = True
                            if p.id in self.pf.positions:
                                p.take_px = 0.0  # даём второй половине забрать длинный тренд по трейлингу
                        if partial_done or p.side * (p.mfe_px - p.entry_px) >= 2.0 * cur_atr:
                            trail_px = p.mfe_px - p.side * 2.5 * cur_atr
                            if (p.side > 0 and trail_px > p.stop_px) or (p.side < 0 and trail_px < p.stop_px):
                                p.stop_px = trail_px

            if p.id not in self.pf.positions:
                continue
            reason = None
            if (p.side > 0 and px <= p.stop_px) or (p.side < 0 and px >= p.stop_px):
                reason = "trailing_stop" if (trailing_on and p.side * (p.stop_px - p.entry_px) > 0) else "stop"
            elif p.take_px and ((p.side > 0 and px >= p.take_px) or (p.side < 0 and px <= p.take_px)):
                reason = "take_profit"
            elif p.horizon_ts and now >= p.horizon_ts:
                reason = "horizon"
            if reason:
                await self._close(p, px, reason, now)

    def _record_closed_learning(self, pos: Position, bet_row: dict, exit_px: float, now: float) -> None:
        """Этап A/C: замыкает обратную связь в ConfidenceCalibrator, SourceRegistry и переобучает MetaLabeler."""
        if not self.feature_enabled("closed_loop_learning"):
            return
        try:
            blob = json.loads(bet_row.get("signals_json") or "{}")
        except Exception:
            blob = {}
        sig_blob = blob.get("signal") or {}
        raw_conf = float(sig_blob.get("confidence", 0.5))
        if not sig_blob.get("benchmark"):
            self.aggregator.calibrator.record(raw_conf, pos.realized_pnl > 0)
            self.aggregator.calibrator.save_to_db(self.db)
            sources = json.loads(bet_row.get("source_ids_json") or "[]")
            pos_ret_pct = (exit_px / pos.entry_px - 1.0) * 100.0 if pos.entry_px else 0.0
            btc_cs = self.md.closed_candles("BTC/USDT", self.cfg.strategies.timeframe, 48)
            btc_ret_pct = 0.0
            if len(btc_cs) >= 2:
                # Доходность BTC за близкое к длительности сделки окно
                bars = max(1, min(len(btc_cs) - 1, int((now - pos.entry_ts) / 3600) or 1))
                btc_ret_pct = (btc_cs[-1].c / btc_cs[-1 - bars].c - 1.0) * 100.0
            horizon_min = int(sig_blob.get("horizon_min", 24 * 60))
            for src_id in sources:
                self.sources.record_outcome(src_id, pos.symbol, pos.side, horizon_min, pos_ret_pct, btc_ret_pct)
        if self.feature_enabled("meta_labeling"):
            self.meta_labeler.fit_from_db(self.db)

    async def _close(self, p: Position, px: float, reason: str, now: float, qty: Optional[float] = None) -> None:
        close_qty = min(qty, p.qty) if qty is not None else p.qty
        if close_qty <= 0:
            return
        attempt = self.db.one("SELECT COUNT(*) AS n FROM orders WHERE client_id LIKE ?", (f"exit:{p.id}:%",))["n"]
        req = OrderRequest(client_id=f"exit:{p.id}:{attempt}", symbol=p.exec_symbol, instrument=p.instrument,
                           side="sell" if p.side > 0 else "buy", qty=close_qty, ref_price=px, strategy_id=p.strategy_id,
                           bet_id=p.bet_id, reduce_only=True, reason=reason)
        res = await self.executor.submit(req)
        if res.qty <= 0:
            return
        entry_notional = p.qty * p.entry_px
        bet_row = self.db.one("SELECT fees, notional, signals_json, source_ids_json FROM bets WHERE id=?", (p.bet_id,)) or {
            "fees": 0.0, "notional": entry_notional, "signals_json": "{}", "source_ids_json": "[]"
        }
        with self.db.tx():
            pos, _, full = self.pf.close_position(p.id, res.qty, res.avg_px, res.fee, reason, now)
            if full:
                mae = pos.side * (pos.mae_px - pos.entry_px) / pos.entry_px * 100
                mfe = pos.side * (pos.mfe_px - pos.entry_px) / pos.entry_px * 100
                self.journal.close_bet(pos.bet_id, res.avg_px, now, reason, pos.realized_pnl, bet_row["notional"] or entry_notional,
                                       mae, mfe, (bet_row["fees"] or 0.0) + res.fee, pos.funding_paid)
            self.pf.save(closed=[pos] if full else None)
        if full:
            self.stats["closed"] += 1
            self._record_closed_learning(pos, bet_row, res.avg_px, now)
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

    async def _run_cross_sectional(self, now: float, regime: str) -> None:
        """Этап B: кросс-секционный моментум с BTC-нейтрализацией (раз в 4 часа)."""
        if not self.feature_enabled("cross_sectional_momentum") or not self.universe:
            return
        if "cross_sectional_momentum" not in self.cfg.strategies.enabled:
            return
        if now - self._last_xsec_ts < 4 * 3600:
            return
        tf = self.cfg.strategies.timeframe
        btc_cs = self.md.closed_candles("BTC/USDT", tf, 30)
        if len(btc_cs) < 25:
            return
        self._last_xsec_ts = now
        btc_ret = btc_cs[-1].c / btc_cs[-25].c - 1.0
        scored: list[tuple[float, str, float]] = []
        for sym in self.universe.symbols:
            if sym == "BTC/USDT":
                continue
            cs = self.md.closed_candles(sym, tf, 30)
            if len(cs) < 25:
                continue
            c = closes(cs[-25:])
            vol = float(np.std(log_returns(c))) * math.sqrt(24.0)
            if vol <= 1e-6:
                continue
            rel = (c[-1] / c[0] - 1.0) - btc_ret
            z = rel / vol
            scored.append((z, sym, rel * 100.0))
        if len(scored) < 4:
            return
        scored.sort(key=lambda x: -x[0])
        # Лонг самого сильного лидера относительно BTC и шорт самого слабого аутсайдера
        top_z, top_sym, top_rel = scored[0]
        bot_z, bot_sym, bot_rel = scored[-1]
        if top_z >= 1.1:
            sig = Signal(
                strategy_id="cross_sectional_momentum", strategy_version="1.0", symbol=top_sym,
                direction="long", confidence=min(0.75, 0.52 + 0.08 * top_z), timeframe=tf,
                horizon_min=36 * 60, stop_atr_mult=2.2, take_atr_mult=3.8, ts=now, regime=regime,
                thesis=f"Кросс-секционный лидер к BTC: rel={top_rel:+.2f}% (z={top_z:+.2f})",
                evidence=[f"rel_btc={top_rel:+.2f}%", f"z={top_z:+.2f}"],
                invalidation="потеря относительной силы / ATR-стоп",
            )
            self.stats["signals"] += 1
            await self._handle_signal(sig, now)
        if bot_z <= -1.1:
            sig = Signal(
                strategy_id="cross_sectional_momentum", strategy_version="1.0", symbol=bot_sym,
                direction="short", confidence=min(0.75, 0.52 + 0.08 * abs(bot_z)), timeframe=tf,
                horizon_min=36 * 60, stop_atr_mult=2.2, take_atr_mult=3.8, ts=now, regime=regime,
                thesis=f"Кросс-секционный аутсайдер к BTC: rel={bot_rel:+.2f}% (z={bot_z:+.2f})",
                evidence=[f"rel_btc={bot_rel:+.2f}%", f"z={bot_z:+.2f}"],
                invalidation="разворот силы к BTC / ATR-стоп",
            )
            self.stats["signals"] += 1
            await self._handle_signal(sig, now)

    async def _run_funding_carry(self, now: float, regime: str) -> None:
        """Этап B: деривативная альфа — вход в сторону получения экстремального фандинга (раз в 4 часа)."""
        if not self.feature_enabled("funding_alpha") or not self.universe or not self.cfg.instruments.perp:
            return
        if "funding_carry" not in self.cfg.strategies.enabled:
            return
        if now - self._last_funding_scan_ts < 4 * 3600:
            return
        self._last_funding_scan_ts = now
        tf = self.cfg.strategies.timeframe
        for sym in self.universe.symbols[:8]:
            perp = self.hub.perp_symbol(sym)
            if not perp:
                continue
            try:
                fr = await self.hub.fetch_funding_rate(perp)
            except Exception:
                continue
            if not fr or abs(fr.rate) < 0.0003:  # >= 0.03% за 8ч (~33% годовых)
                continue
            cs = self.md.closed_candles(sym, tf, 30)
            if len(cs) < 20:
                continue
            r_now = float(rsi(closes(cs), 14)[-1])
            if fr.rate >= 0.0003 and r_now >= 55.0:
                direction = "short"
            elif fr.rate <= -0.0003 and r_now <= 45.0:
                direction = "long"
            else:
                continue
            sig = Signal(
                strategy_id="funding_carry", strategy_version="1.0", symbol=sym,
                direction=direction, confidence=min(0.74, 0.55 + abs(fr.rate) * 200.0), timeframe=tf,
                horizon_min=48 * 60, stop_atr_mult=2.5, take_atr_mult=3.2, ts=now, regime=regime,
                thesis=f"Funding carry/crowding ({fr.rate:+.4%}/8ч, RSI={r_now:.1f})",
                evidence=[f"funding={fr.rate:+.4%}", f"rsi={r_now:.1f}"],
                invalidation="нормализация фандинга / ATR-стоп",
            )
            self.stats["signals"] += 1
            await self._handle_signal(sig, now)

    async def generate_and_trade(self) -> None:
        if not self.universe:
            return
        now = self.clock()
        btc_cs = self.md.closed_candles("BTC/USDT", self.cfg.strategies.timeframe, 60)
        regime = detect_regime(btc_cs) if btc_cs else ""
        self._last_regime = regime
        self._update_vol_targeting()
        self.pf.allocations = self.meta.update_weights(regime=regime)

        regime_gate = self.feature_enabled("regime_gating")
        mtf_on = self.feature_enabled("mtf_filter")

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

                # Этап A: режимный фильтр стратегий (трендовые не торгуют в flat, контртрендовые — только в flat)
                if regime_gate and not sig.benchmark and regime:
                    if strat.id in TREND_STRATEGIES and regime in ("flat", "panic"):
                        self.stats["rejected"] += 1
                        self.journal.reject(sig, [f"regime_gate: {strat.id} отключена в {regime}"], None)
                        continue
                    if strat.id in MEAN_REV_STRATEGIES and regime != "flat":
                        self.stats["rejected"] += 1
                        self.journal.reject(sig, [f"regime_gate: {strat.id} только в flat (сейчас {regime})"], None)
                        continue

                # Этап A: MTF-фильтр старшего таймфрейма 4h для трендовых стратегий
                if mtf_on and not sig.benchmark and strat.id in TREND_STRATEGIES:
                    mtf = self._mtf_trend(sym)
                    if (sig.direction == "long" and mtf < 0) or (sig.direction == "short" and mtf > 0):
                        self.stats["rejected"] += 1
                        self.journal.reject(sig, [f"mtf_4h_conflict: 4h={mtf:+d}"], None)
                        continue

                await self._handle_signal(sig, now)

        await self._run_cross_sectional(now, regime)
        await self._run_funding_carry(now, regime)

    async def _handle_signal(self, sig, now: float) -> None:
        # Прогон через ИИ-аналитиков и агрегатор (кроме контрольного случайного бенчмарка)
        if self.cfg.ai.enabled and self.feature_enabled("ai_review") and not sig.benchmark:
            try:
                merged = await asyncio.to_thread(self._run_ai_review, sig)
                if merged is None:
                    self.stats["rejected"] += 1
                    self.journal.reject(sig, ["ai_veto_or_conflict"], None)
                    return
                sig = merged
            except Exception as e:
                log.warning("ИИ-проверка пропущена из-за ошибки", extra={"error": str(e)[:120]})

        inst = "perp" if (sig.strategy_id == "funding_carry" or sig.direction == "short" or not self.cfg.instruments.spot) else "spot"
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

        ctx.mtf_trend = self._mtf_trend(sig.symbol) if self.feature_enabled("mtf_filter") else 0
        ctx.vol_scalar = self._btc_vol_scalar_val if self.feature_enabled("vol_targeting") else 1.0
        feats = extract_features(
            confidence=sig.confidence, direction=sig.direction, regime=sig.regime or self._last_regime,
            mtf_trend=ctx.mtf_trend, obi=ctx.obi, funding_rate=ctx.funding_rate,
            spread_bps=ctx.spread_bps, atr=ctx.atr, price=ctx.price,
        )
        ctx.ml_features = feats
        if self.feature_enabled("meta_labeling") and not sig.benchmark:
            prob, scale = self.meta_labeler.predict_scale(feats)
            ctx.ml_prob, ctx.ml_scale = prob, scale
        else:
            ctx.ml_prob, ctx.ml_scale = None, 1.0

        dec = self.risk.evaluate(sig, ctx, self.prices)
        if not dec.approved:
            self.stats["rejected"] += 1
            self.journal.reject(sig, dec.reasons, ctx, dec.checks)
            return
        bet_id = uuid.uuid4().hex
        req = OrderRequest(client_id=f"entry:{bet_id}", symbol=exec_sym, instrument=inst,
                           side="buy" if sig.direction == "long" else "sell", qty=dec.qty, ref_price=ctx.price,
                           strategy_id=sig.strategy_id, bet_id=bet_id, reason=sig.thesis[:100],
                           maker_preferred=bool(self.feature_enabled("maker_first") and not sig.benchmark))
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
