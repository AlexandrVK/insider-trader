"""Индикаторы на numpy и определение рыночного режима (без lookahead)."""
from __future__ import annotations

from typing import Literal, Sequence

import numpy as np

from ..models import Candle

Regime = Literal["trend", "flat", "high_vol", "panic"]


def closes(candles: Sequence[Candle]) -> np.ndarray:
    return np.array([c.c for c in candles], dtype=float)


def ema(values: Sequence[float], n: int) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    out = np.empty_like(v)
    if len(v) == 0:
        return out
    a = 2.0 / (n + 1)
    out[0] = v[0]
    for i in range(1, len(v)):
        out[i] = a * v[i] + (1 - a) * out[i - 1]
    return out


def sma(values: Sequence[float], n: int) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    out = np.full(len(v), np.nan)
    if len(v) >= n:
        c = np.cumsum(np.insert(v, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def true_range(candles: Sequence[Candle]) -> np.ndarray:
    tr = np.empty(len(candles))
    for i, c in enumerate(candles):
        tr[i] = c.h - c.l if i == 0 else max(c.h - c.l, abs(c.h - candles[i - 1].c), abs(c.l - candles[i - 1].c))
    return tr


def atr(candles: Sequence[Candle], n: int = 14) -> float:
    if len(candles) < n + 1:
        return 0.0
    tr = true_range(candles)
    a = tr[1:n + 1].mean()
    for x in tr[n + 1:]:
        a = (a * (n - 1) + x) / n
    return float(a)


def rsi(values: Sequence[float], n: int = 14) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    out = np.full(len(v), np.nan)
    if len(v) < n + 1:
        return out
    diff = np.diff(v)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_g, avg_l = gains[:n].mean(), losses[:n].mean()
    out[n] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(n + 1, len(v)):
        avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
        avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(values, fast) - ema(values, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(values: Sequence[float], n: int = 20, k: float = 2.0):
    v = np.asarray(values, dtype=float)
    mid = sma(v, n)
    std = np.full(len(v), np.nan)
    for i in range(n - 1, len(v)):
        std[i] = np.std(v[i - n + 1:i + 1], ddof=0)
    return mid - k * std, mid, mid + k * std


def donchian(candles: Sequence[Candle], n: int = 20) -> tuple[float, float]:
    if len(candles) < n + 1:
        return 0.0, 0.0
    window = candles[-(n + 1):-1]
    return min(c.l for c in window), max(c.h for c in window)


def adx(candles: Sequence[Candle], n: int = 14) -> float:
    if len(candles) < 2 * n + 1:
        return 0.0
    highs = np.array([c.h for c in candles], dtype=float)
    lows = np.array([c.l for c in candles], dtype=float)
    tr = true_range(candles)
    up, dn = np.diff(highs), -np.diff(lows)
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr_s = ema(tr[1:], n)
    p_di = 100.0 * ema(plus_dm, n) / np.where(tr_s > 0, tr_s, 1.0)
    m_di = 100.0 * ema(minus_dm, n) / np.where(tr_s > 0, tr_s, 1.0)
    denom = p_di + m_di
    dx = 100.0 * np.abs(p_di - m_di) / np.where(denom > 0, denom, 1.0)
    return float(ema(dx, n)[-1])


def log_returns(values: Sequence[float]) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    return np.diff(np.log(v)) if len(v) > 1 else np.array([])


def detect_regime(candles: Sequence[Candle]) -> Regime:
    if len(candles) < 50:
        return "flat"
    c = closes(candles)
    rets = log_returns(c[-50:])
    vol_recent = float(np.std(rets[-14:])) if len(rets) >= 14 else 0.0
    vol_base = float(np.std(rets)) if len(rets) >= 20 else 1e-6
    dd_24 = (c[-1] / float(np.max(c[-24:])) - 1.0) * 100.0
    if dd_24 <= -6.0 and vol_recent > 1.5 * vol_base:
        return "panic"
    if vol_base > 0 and vol_recent > 1.6 * vol_base:
        return "high_vol"
    if adx(candles, 14) >= 22.0:
        return "trend"
    return "flat"


def correlation_clusters(series: dict[str, Sequence[float]], threshold: float = 0.8, min_len: int = 30) -> dict[str, str]:
    names = [k for k, v in series.items() if len(v) >= min_len]
    rets = {k: log_returns(series[k]) for k in names}
    n = min((len(r) for r in rets.values()), default=0)
    parent = {k: k for k in series}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    if n >= min_len - 1:
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                ra, rb = rets[a][-n:], rets[b][-n:]
                if ra.std() > 0 and rb.std() > 0 and np.corrcoef(ra, rb)[0, 1] >= threshold:
                    parent[find(a)] = find(b)
    roots, out = {}, {}
    for k in series:
        r = find(k)
        roots.setdefault(r, f"c{len(roots) + 1}")
        out[k] = roots[r]
    return out


def suspicious_volume_ratio(candles: Sequence[Candle], vol_mult: float = 3.0, max_move_pct: float = 0.3) -> float:
    if len(candles) < 20:
        return 0.0
    vols = np.array([c.v * c.c for c in candles])
    med = float(np.median(vols))
    if med <= 0:
        return 1.0
    bad = sum(1 for c, v in zip(candles, vols) if v >= vol_mult * med and abs(c.c / c.o - 1) * 100 < max_move_pct)
    return bad / len(candles)
