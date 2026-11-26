"""Этап C: Meta-Labeling (по Маркосу Лопесу де Прадо) на чистом numpy.
Оценивает вероятность успеха сигнала базовой стратегии P(win) по контексту рынка
и масштабирует размер позиции (0..1x) без обхода RiskEngine."""
from __future__ import annotations

import json
import math
from typing import Optional

import numpy as np

from ..db import Database

FEATURE_NAMES = (
    "confidence",
    "dir_sign",
    "is_trend",
    "is_flat",
    "is_high_vol",
    "mtf_align",
    "obi_signed",
    "funding_signed_bps",
    "spread_bps",
    "atr_pct",
)


def extract_features(
    *,
    confidence: float,
    direction: str,
    regime: str,
    mtf_trend: int,
    obi: float,
    funding_rate: float,
    spread_bps: float,
    atr: float,
    price: float,
) -> dict[str, float]:
    sgn = 1.0 if direction == "long" else (-1.0 if direction == "short" else 0.0)
    mtf_align = float(sgn * mtf_trend) if mtf_trend else 0.0
    atr_pct = (atr / price * 100.0) if price > 0 else 0.0
    return {
        "confidence": round(float(confidence), 4),
        "dir_sign": sgn,
        "is_trend": 1.0 if regime == "trend" else 0.0,
        "is_flat": 1.0 if regime == "flat" else 0.0,
        "is_high_vol": 1.0 if regime == "high_vol" else 0.0,
        "mtf_align": mtf_align,
        "obi_signed": round(float(sgn * obi), 4),
        "funding_signed_bps": round(float(sgn * funding_rate * 1e4), 4),
        "spread_bps": round(float(min(spread_bps, 50.0)), 4),
        "atr_pct": round(float(min(atr_pct, 20.0)), 4),
    }


def _vec(feats: dict[str, float]) -> np.ndarray:
    return np.array([float(feats.get(k, 0.0)) for k in FEATURE_NAMES], dtype=float)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0)))


class MetaLabeler:
    """L2-регуляризованная логистическая регрессия (walk-forward) для оценки P(win)."""

    def __init__(self, min_trades: int = 40, l2: float = 1.5, lr: float = 0.15, steps: int = 250):
        self.min_trades = max(10, int(min_trades))
        self.l2 = l2
        self.lr = lr
        self.steps = steps
        self.w: Optional[np.ndarray] = None
        self.b: float = 0.0
        self.mean: Optional[np.ndarray] = None
        self.std: Optional[np.ndarray] = None
        self.n_samples: int = 0
        self.train_acc: float = 0.0
        self.last_prob: Optional[float] = None
        self.blocked_count: int = 0

    @property
    def active(self) -> bool:
        return self.w is not None and self.n_samples >= self.min_trades

    def fit(self, X: list[dict[str, float]], y: list[int]) -> bool:
        n = len(X)
        self.n_samples = n
        if n < self.min_trades or len(set(y)) < 2:
            self.w = None
            return False
        xm = np.vstack([_vec(f) for f in X])
        yv = np.asarray(y, dtype=float)
        mean = xm.mean(axis=0)
        std = xm.std(axis=0)
        std = np.where(std > 1e-6, std, 1.0)
        xn = (xm - mean) / std

        w = np.zeros(xn.shape[1], dtype=float)
        b = math.log(max(1e-3, yv.mean()) / max(1e-3, 1.0 - yv.mean()))
        for _ in range(self.steps):
            p = _sigmoid(xn @ w + b)
            err = p - yv
            grad_w = (xn.T @ err) / n + (self.l2 / n) * w
            grad_b = float(err.mean())
            w -= self.lr * grad_w
            b -= self.lr * grad_b

        preds = (_sigmoid(xn @ w + b) >= 0.5).astype(float)
        self.w, self.b, self.mean, self.std = w, float(b), mean, std
        self.train_acc = round(float((preds == yv).mean()) * 100.0, 1)
        return True

    def fit_from_db(self, db: Database, limit: int = 600) -> bool:
        rows = db.query(
            "SELECT signals_json, pnl FROM bets WHERE status='closed' AND benchmark=0 AND pnl IS NOT NULL "
            "ORDER BY exit_ts DESC LIMIT ?",
            (limit,),
        )
        X, y = [], []
        for r in reversed(rows):
            try:
                blob = json.loads(r["signals_json"] or "{}")
            except Exception:
                continue
            feats = (blob.get("risk") or {}).get("ml_features") or blob.get("ml_features")
            if isinstance(feats, dict) and feats:
                X.append(feats)
                y.append(1 if float(r["pnl"]) > 0 else 0)
        return self.fit(X, y)

    def predict_scale(self, feats: dict[str, float]) -> tuple[Optional[float], float]:
        """Возвращает (P(win), scale в [0.0, 1.0]). Пока мало сделок — (None, 1.0)."""
        if not self.active or self.w is None or self.mean is None or self.std is None:
            return None, 1.0
        x = (_vec(feats) - self.mean) / self.std
        prob = float(_sigmoid(np.array([float(x @ self.w + self.b)]))[0])
        self.last_prob = round(prob, 3)
        if prob < 0.42:
            self.blocked_count += 1
            return prob, 0.0
        # Плавное масштабирование размера позиции от 0.5x (при 0.42) до 1.0x (при >= 0.65)
        scale = min(1.0, max(0.5, 0.5 + (prob - 0.42) / 0.23 * 0.5))
        return prob, round(scale, 3)

    def status(self) -> dict:
        return {
            "active": self.active,
            "samples": self.n_samples,
            "min_trades": self.min_trades,
            "train_acc_pct": self.train_acc,
            "last_prob": self.last_prob,
            "blocked": self.blocked_count,
        }
