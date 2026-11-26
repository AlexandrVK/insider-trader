"""Честный бэктестер и walk-forward валидация (без lookahead, с комиссиями и проскальзыванием)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from ..market.indicators import atr
from ..models import Candle
from ..strategies.base import Strategy
from ..strategies.meta_selector import deflated_sharpe_ratio


@dataclass
class BacktestResult:
    trades: int
    net_return_pct: float
    sharpe: float
    deflated_sharpe: float
    max_drawdown_pct: float
    returns_pct: list[float]


def run_backtest(
    strategy: Strategy,
    symbol: str,
    candles: Sequence[Candle],
    *,
    fee_bps: float = 10.0,
    slippage_bps: float = 5.0,
    n_trials: int = 1,
) -> BacktestResult:
    round_trip_cost = 2.0 * (fee_bps + slippage_bps) / 1e4
    pos = None
    trade_rets: list[float] = []
    equity = 1.0
    curve = [1.0]

    for i in range(strategy.min_candles, len(candles) - 1):
        bar, nxt = candles[i], candles[i + 1]
        if pos is not None:
            hit_sl = (bar.l <= pos["stop"]) if pos["side"] > 0 else (bar.h >= pos["stop"])
            hit_tp = pos["take"] and ((bar.h >= pos["take"]) if pos["side"] > 0 else (bar.l <= pos["take"]))
            if hit_sl or hit_tp or i >= pos["exit_i"]:
                exit_px = pos["stop"] if hit_sl else (pos["take"] if hit_tp else bar.c)
                net_ret = pos["side"] * (exit_px / pos["entry"] - 1.0) - round_trip_cost
                trade_rets.append(net_ret * 100.0)
                equity *= (1.0 + net_ret)
                curve.append(equity)
                pos = None

        if pos is None:
            sig = strategy.generate(symbol, candles[:i + 1], bar.ts / 1000.0)
            if sig is not None and sig.direction != "flat":
                a = atr(candles[:i + 1], 14)
                if a <= 0:
                    continue
                side = 1 if sig.direction == "long" else -1
                entry = nxt.o
                stop = entry - side * a * sig.stop_atr_mult
                take = entry + side * a * sig.take_atr_mult if sig.take_atr_mult else 0.0
                pos = {"side": side, "entry": entry, "stop": stop, "take": take, "exit_i": i + 24}

    arr = np.asarray(trade_rets, dtype=float)
    sharpe = float(np.mean(arr) / np.std(arr, ddof=1)) if len(arr) > 1 and np.std(arr, ddof=1) > 0 else 0.0
    dsr = deflated_sharpe_ratio(trade_rets, n_trials=n_trials)
    peak, dd = 1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak * 100.0)
    return BacktestResult(len(trade_rets), (equity - 1.0) * 100.0, sharpe, dsr, dd, trade_rets)


def walk_forward_validate(
    factory: Callable[[dict], Strategy],
    param_grid: list[dict],
    symbol: str,
    candles: Sequence[Candle],
    train_bars: int = 300,
    test_bars: int = 120,
) -> dict:
    oos_returns: list[float] = []
    folds, idx, n_trials = 0, 0, max(1, len(param_grid))

    while idx + train_bars + test_bars <= len(candles):
        train_slice = candles[idx:idx + train_bars]
        test_slice = candles[idx + train_bars:idx + train_bars + test_bars]
        best_p, best_sr = param_grid[0], float("-inf")
        for p in param_grid:
            res = run_backtest(factory(p), symbol, train_slice, n_trials=n_trials)
            if res.sharpe > best_sr:
                best_sr, best_p = res.sharpe, p
        oos = run_backtest(factory(best_p), symbol, test_slice, n_trials=n_trials)
        oos_returns.extend(oos.returns_pct)
        folds += 1
        idx += test_bars

    arr = np.asarray(oos_returns, dtype=float)
    oos_sharpe = float(np.mean(arr) / np.std(arr, ddof=1)) if len(arr) > 1 and np.std(arr, ddof=1) > 0 else 0.0
    return {
        "folds": folds,
        "oos_trades": len(oos_returns),
        "oos_sharpe": round(oos_sharpe, 3),
        "oos_dsr": round(deflated_sharpe_ratio(oos_returns, n_trials=n_trials), 3),
        "oos_mean_ret_pct": round(float(np.mean(arr)), 3) if len(arr) else 0.0,
    }
