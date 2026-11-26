"""Риск-движок: единственная точка допуска сделок (не обходится стратегией или ИИ)."""
from __future__ import annotations

import logging
import time
from typing import Callable

from .config import Settings
from .journal import Journal
from .models import MarketContext, RiskDecision, Signal
from .portfolio import DAY, WEEK, Portfolio

log = logging.getLogger("bot.risk")


class RiskEngine:
    def __init__(self, cfg: Settings, portfolio: Portfolio, journal: Journal, clock: Callable[[], float] = time.time):
        self.cfg, self.pf, self.journal, self.clock = cfg, portfolio, journal, clock

    def update_state(self, prices: dict) -> dict:
        r, pf, now = self.cfg.risk, self.pf, self.clock()
        eq = pf.equity(prices)
        pf.roll_periods(now, eq)
        pf.peak_equity = max(pf.peak_equity, eq)
        dd = pf.drawdown(eq) * 100
        day_loss = (pf.day_start_equity - eq) / pf.day_start_equity * 100 if pf.day_start_equity else 0.0
        week_loss = (pf.week_start_equity - eq) / pf.week_start_equity * 100 if pf.week_start_equity else 0.0
        info = {"equity": eq, "drawdown_pct": dd, "day_loss_pct": day_loss, "week_loss_pct": week_loss}
        if not pf.halted and dd >= r.max_drawdown_pct:
            pf.halted, pf.halt_reason = True, f"просадка {dd:.1f}% >= {r.max_drawdown_pct}%"
            pf.db.log_risk_event("max_drawdown_halt", info, now)
        if day_loss >= r.daily_loss_limit_pct and pf.paused_until < pf.day_start_ts + DAY:
            pf.paused_until, pf.pause_reason = pf.day_start_ts + DAY, f"дневной лимит убытка {day_loss:.1f}%"
            pf.db.log_risk_event("daily_loss_pause", info, now)
        if week_loss >= r.weekly_loss_limit_pct and pf.paused_until < pf.week_start_ts + WEEK:
            pf.paused_until, pf.pause_reason = pf.week_start_ts + WEEK, f"недельный лимит убытка {week_loss:.1f}%"
            pf.db.log_risk_event("weekly_loss_pause", info, now)
        return info

    def reset_halt(self, prices: dict | None = None) -> None:
        self.pf.halted, self.pf.halt_reason = False, ""
        self.pf.peak_equity = self.pf.equity(prices or {})
        self.pf.paused_until, self.pf.pause_reason = 0.0, ""
        self.pf.db.log_risk_event("manual_reset", {}, self.clock())

    def evaluate(self, sig: Signal, ctx: MarketContext, prices: dict) -> RiskDecision:
        r, ex, feat, now = self.cfg.risk, self.cfg.execution, self.cfg.features, self.clock()
        pf = self.pf
        dec = RiskDecision(False, instrument=ctx.instrument)
        why = dec.reasons
        eq = pf.equity(prices)
        dec.checks = {
            "equity": round(eq, 2),
            "obi": round(ctx.obi, 3),
            "vol_scalar": round(ctx.vol_scalar, 3),
            "mtf_trend": ctx.mtf_trend,
            "ml_prob": ctx.ml_prob,
            "ml_scale": round(ctx.ml_scale, 3),
            "ml_features": dict(ctx.ml_features) if ctx.ml_features else {},
        }
        side = 1 if sig.direction == "long" else (-1 if sig.direction == "short" else 0)

        if sig.direction == "flat":
            why.append("flat_signal")
        if pf.halted:
            why.append(f"halted: {pf.halt_reason}")
        if pf.paused_until > now:
            why.append(f"paused: {pf.pause_reason}")
        for a, b in r.blackout_windows:
            if a <= now <= b:
                why.append("news_blackout")
        n, last = self.journal.consecutive_losses(sig.strategy_id)
        if n >= r.cooldown_losses and now - last < r.cooldown_minutes * 60:
            why.append(f"cooldown: {n} убытков подряд у {sig.strategy_id}")
        if ctx.instrument == "spot" and (sig.direction == "short" or not self.cfg.instruments.spot):
            why.append("spot_short_or_disabled")
        if ctx.instrument == "perp" and not self.cfg.instruments.perp:
            why.append("perp_disabled")
        if ctx.instrument == "perp":
            # При включённой funding_alpha блокируем только НЕБЛАГОПРИЯТНЫЙ высокий фандинг (который платим мы)
            if feat.funding_alpha:
                if side * ctx.funding_rate > r.max_funding_rate_abs:
                    why.append(f"adverse_funding: {ctx.funding_rate:+.4%}")
            elif abs(ctx.funding_rate) > r.max_funding_rate_abs:
                why.append(f"funding_too_high: {ctx.funding_rate:.4%}")
        if ctx.data_age_sec > r.max_data_age_sec:
            why.append(f"stale_data: {ctx.data_age_sec:.0f}s")
        if ctx.spread_bps > r.max_spread_bps:
            why.append(f"wide_spread: {ctx.spread_bps:.1f}bps")
        if ctx.gap_pct > r.max_gap_pct:
            why.append(f"price_gap: {ctx.gap_pct:.1f}%")
        if ctx.alt_price and ctx.price:
            div = abs(ctx.alt_price / ctx.price - 1) * 1e4
            dec.checks["divergence_bps"] = round(div, 1)
            if div > r.max_divergence_bps:
                why.append(f"source_divergence: {div:.0f}bps")
        if ctx.atr <= 0 or ctx.price <= 0:
            why.append("no_atr_or_price")
        if any(p.symbol == sig.symbol for p in pf.positions.values()):
            why.append("position_already_open")
        if len(pf.positions) >= r.max_positions:
            why.append("max_positions")

        # Этап A: фильтр «потенциал тейка vs комиссия + спред» (не применяется к бенчмарку и funding_carry)
        if feat.fee_edge_filter and not sig.benchmark and sig.strategy_id != "funding_carry" and ctx.atr > 0 and ctx.price > 0 and sig.take_atr_mult:
            fee_one_way = (ex.perp_maker_fee_bps if feat.maker_first else ex.perp_taker_fee_bps) if ctx.instrument == "perp" else (ex.maker_fee_bps if feat.maker_first else ex.taker_fee_bps)
            cost_bps = max(1.0, 2.0 * fee_one_way + max(0.0, ctx.spread_bps))
            take_bps = (ctx.atr * sig.take_atr_mult / ctx.price) * 1e4
            dec.checks["edge_to_cost"] = round(take_bps / cost_bps, 2)
            if take_bps < feat.min_edge_to_cost * cost_bps:
                why.append(f"low_edge_to_cost: {take_bps:.0f}bps < {feat.min_edge_to_cost:.1f}x*{cost_bps:.0f}bps")

        # Этап A: микроструктура стакана (OBI) — не входить прямо в сильную стену против направления
        if feat.obi_filter and not sig.benchmark and side * ctx.obi < -feat.obi_veto_threshold:
            why.append(f"adverse_obi: {ctx.obi:+.2f}")

        # Этап C: Meta-Labeling ML-фильтр (блокирует вход при низкой вероятности успеха)
        if feat.meta_labeling and not sig.benchmark and ctx.ml_scale <= 0.0:
            why.append(f"meta_label_low_prob: {ctx.ml_prob}")

        if why:
            return dec

        mult = sig.stop_atr_mult or r.default_stop_atr_mult
        stop_dist = ctx.atr * mult
        stop_px = ctx.price - side * stop_dist
        if stop_px <= 0:
            why.append("stop_below_zero")
            return dec
        qty = eq * r.risk_per_trade_pct / 100 / stop_dist
        # Этап B (Portfolio Volatility Targeting) + Этап C (Meta-Labeling sizing)
        if feat.vol_targeting and not sig.benchmark:
            qty *= max(0.35, min(1.25, ctx.vol_scalar))
        if feat.meta_labeling and not sig.benchmark:
            qty *= max(0.0, min(1.0, ctx.ml_scale))
        notional = qty * ctx.price
        caps = {
            "risk": notional,
            "max_position": eq * r.max_position_pct / 100,
            "strategy_alloc": max(0.0, pf.strategy_equity(sig.strategy_id, prices) - pf.strategy_exposure(sig.strategy_id, prices)),
            "gross_exposure": max(0.0, eq * r.max_gross_exposure_pct / 100 - pf.exposure(prices)),
            "liquidity": ctx.depth_usd * ex.max_participation if ctx.depth_usd else notional,
        }
        if ctx.cluster:
            cn, cexp = pf.cluster_exposure(ctx.cluster, prices)
            if cn >= r.max_cluster_positions:
                why.append(f"cluster_positions: {cn} в {ctx.cluster}")
                return dec
            caps["cluster"] = max(0.0, eq * r.max_cluster_exposure_pct / 100 - cexp)
        leverage = 1.0 if ctx.instrument == "spot" else min(2.0, self.cfg.instruments.max_leverage)
        margin_room = pf.cash * leverage if ctx.instrument == "perp" else pf.cash
        caps["cash"] = max(0.0, margin_room * 0.98)
        binding = min(caps, key=caps.get)
        notional = min(caps.values())
        dec.checks["caps"] = {k: round(v, 2) for k, v in caps.items()}
        dec.checks["binding_cap"] = binding
        if notional < ex.min_notional_usd:
            why.append(f"size_too_small: ${notional:.2f} (ограничивает {binding})")
            return dec
        qty = notional / ctx.price
        dec.approved = True
        dec.qty, dec.notional, dec.stop_px, dec.leverage = qty, notional, stop_px, leverage
        dec.take_px = ctx.price + side * ctx.atr * sig.take_atr_mult if sig.take_atr_mult else 0.0
        dec.checks["risk_amount"] = round(qty * stop_dist, 2)
        dec.checks["risk_pct_actual"] = round(qty * stop_dist / eq * 100, 3)
        return dec
