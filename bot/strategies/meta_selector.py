"""Мета-селектор стратегий: режимы, exploration>=10%, карантин, CUSUM, Deflated Sharpe Ratio."""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from ..db import Database

log = logging.getLogger("bot.meta")


def deflated_sharpe_ratio(returns: list[float], n_trials: int = 1) -> float:
    n = len(returns)
    if n < 5:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    std = float(np.std(arr, ddof=1))
    if std <= 1e-12:
        return 0.0
    sr = float(np.mean(arr)) / std
    sr0 = math.sqrt(2.0 * math.log(max(1, n_trials))) / math.sqrt(n) if n_trials > 1 else 0.0
    z = (sr - sr0) * math.sqrt(n - 1)
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass
class CUSUMDetector:
    threshold: float = 3.5
    drift: float = 0.25
    neg_sum: float = 0.0
    degraded: bool = False
    degraded_since: float = 0.0

    def update(self, standardized_return: float, now: float) -> bool:
        self.neg_sum = max(0.0, self.neg_sum - standardized_return - self.drift)
        if self.neg_sum >= self.threshold:
            self.degraded = True
            self.degraded_since = now
        return self.degraded

    def reset(self) -> None:
        self.neg_sum = 0.0
        self.degraded = False
        self.degraded_since = 0.0


@dataclass
class StrategyMetaState:
    strategy_id: str
    n_params: int = 2
    cusum: CUSUMDetector = field(default_factory=CUSUMDetector)
    quarantine_min_trades: int = 10


class MetaSelector:
    def __init__(
        self,
        db: Database,
        strategy_ids: list[str],
        *,
        n_params_map: Optional[dict[str, int]] = None,
        exploration_floor: float = 0.10,
        quarantine_weight: float = 0.03,
        rebalance_days: float = 3.0,
        recheck_days: float = 7.0,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.strategy_ids = list(strategy_ids)
        self.exploration_floor = max(0.10, exploration_floor)
        self.quarantine_weight = quarantine_weight
        self.rebalance_sec = rebalance_days * 86400
        self.recheck_sec = recheck_days * 86400
        self.clock = clock
        self.last_rebalance_ts: float = 0.0
        self.states = {
            sid: StrategyMetaState(sid, n_params=(n_params_map or {}).get(sid, 2))
            for sid in self.strategy_ids
        }
        self.weights: dict[str, float] = {sid: 1.0 / len(self.strategy_ids) for sid in self.strategy_ids}

    def _load_returns_by_regime(self, regime: str, lookback: int = 50) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
        reg_out: dict[str, list[float]] = {sid: [] for sid in self.strategy_ids}
        all_out: dict[str, list[float]] = {sid: [] for sid in self.strategy_ids}
        rows = self.db.query(
            "SELECT strategy_id, pnl_pct, regime FROM bets WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?",
            (lookback * len(self.strategy_ids),),
        )
        for r in reversed(rows):
            sid = r["strategy_id"]
            if sid not in all_out or r["pnl_pct"] is None:
                continue
            val = float(r["pnl_pct"])
            all_out[sid].append(val)
            if not regime or not r["regime"] or r["regime"] == regime:
                reg_out[sid].append(val)
        return reg_out, all_out

    def update_weights(self, regime: str = "", force: bool = False) -> dict[str, float]:
        now = self.clock()
        if not force and self.last_rebalance_ts and (now - self.last_rebalance_ts) < self.rebalance_sec:
            return dict(self.weights)

        reg_map, all_map = self._load_returns_by_regime(regime)
        n_trials = max(1, len(self.strategy_ids))
        raw_scores: dict[str, float] = {}
        reasons: dict[str, str] = {}

        for sid in self.strategy_ids:
            if sid == "flat":
                raw_scores[sid] = 0.0
                reasons[sid] = "cash_baseline"
                continue
            st = self.states[sid]
            # Предпочитаем статистику текущего режима, но если в нём мало сделок — используем общую историю
            rets = reg_map.get(sid, [])
            if len(rets) < st.quarantine_min_trades:
                rets = all_map.get(sid, [])
            if len(rets) < st.quarantine_min_trades:
                raw_scores[sid] = -10.0
                reasons[sid] = f"quarantine({len(rets)}/{st.quarantine_min_trades})"
                continue

            arr = np.asarray(rets, dtype=float)
            std = float(np.std(arr, ddof=1)) or 1.0
            meta_key = f"cusum_degraded_since:{sid}"
            prev_deg = float(self.db.get_meta(meta_key, "0") or 0.0)
            st.cusum.reset()
            for x in arr[-20:]:
                st.cusum.update(x / std, now)

            if st.cusum.degraded:
                deg_since = prev_deg if prev_deg > 0 else now
                if prev_deg <= 0:
                    self.db.set_meta(meta_key, str(deg_since))
                st.cusum.degraded_since = deg_since
                if now - deg_since >= self.recheck_sec:
                    # Повторный шанс через 7 дней в режиме карантина
                    self.db.set_meta(meta_key, "0")
                    st.cusum.reset()
                    raw_scores[sid] = -10.0
                    reasons[sid] = "recheck_after_7d"
                    continue
                raw_scores[sid] = -20.0
                reasons[sid] = "cusum_degraded"
                continue
            elif prev_deg > 0:
                self.db.set_meta(meta_key, "0")

            dsr = deflated_sharpe_ratio(rets, n_trials=n_trials)
            penalty = 0.02 * st.n_params
            mean_ret = float(np.mean(arr))
            raw_scores[sid] = mean_ret * dsr - penalty
            reasons[sid] = f"mean={mean_ret:.3f}%, dsr={dsr:.2f}"

        new_w: dict[str, float] = {}
        active = [s for s, sc in raw_scores.items() if sc > -5.0]
        quarantined = [s for s, sc in raw_scores.items() if -15.0 < sc <= -5.0]
        degraded = [s for s, sc in raw_scores.items() if sc <= -15.0]

        for s in degraded:
            new_w[s] = 0.0
        for s in quarantined:
            new_w[s] = self.quarantine_weight

        rem = max(0.0, 1.0 - sum(new_w.values()))
        if active:
            max_sc = max(raw_scores[s] for s in active)
            exps = {s: math.exp(max(-5.0, min(5.0, raw_scores[s] - max_sc))) for s in active}
            tot_exp = sum(exps.values())
            expl_each = (rem * self.exploration_floor) / len(active)
            exploit_pool = rem * (1.0 - self.exploration_floor)
            for s in active:
                new_w[s] = expl_each + exploit_pool * (exps[s] / tot_exp)
        else:
            for s in self.strategy_ids:
                new_w[s] = new_w.get(s, 0.0) + rem / len(self.strategy_ids)

        s_tot = sum(new_w.values()) or 1.0
        new_w = {k: round(v / s_tot, 4) for k, v in new_w.items()}
        self.weights = new_w
        self.last_rebalance_ts = now
        self.db.log_risk_event("meta_rebalance", {"regime": regime, "weights": new_w, "reasons": reasons}, now)
        return dict(self.weights)
