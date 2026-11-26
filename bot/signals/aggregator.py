"""Агрегатор сигналов с калибровкой уверенности по реальной истории точности."""
from __future__ import annotations

import json
from typing import Optional

from ..analysis.ai import AnalystSignal
from ..models import Signal


class ConfidenceCalibrator:
    def __init__(self, prior_strength: float = 10.0):
        self.prior_strength = prior_strength
        self._bins: dict[int, list[int]] = {}

    @staticmethod
    def _bin(conf: float) -> int:
        return min(9, max(0, int(conf * 10)))

    def record(self, raw_conf: float, hit: bool) -> None:
        bucket = self._bins.setdefault(self._bin(raw_conf), [])
        bucket.append(1 if hit else 0)
        if len(bucket) > 500:
            del bucket[:-500]

    def save_to_db(self, db) -> None:
        payload = {str(k): v[-500:] for k, v in self._bins.items()}
        db.set_meta("calibrator_bins", json.dumps(payload))

    def load_from_db(self, db) -> None:
        raw = db.get_meta("calibrator_bins")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        if isinstance(data, dict):
            self._bins = {
                int(k): [1 if int(x) else 0 for x in v][-500:]
                for k, v in data.items()
                if str(k).isdigit() and isinstance(v, list)
            }

    def calibrate(self, raw_conf: float) -> float:
        obs = self._bins.get(self._bin(raw_conf), [])
        if not obs:
            return raw_conf
        return (sum(obs) + 0.5 * self.prior_strength) / (len(obs) + self.prior_strength)


class SignalAggregator:
    def __init__(self, calibrator: Optional[ConfidenceCalibrator] = None):
        self.calibrator = calibrator or ConfidenceCalibrator()

    def aggregate(
        self,
        base_signal: Signal,
        ai_signals: list[AnalystSignal],
        source_weights: Optional[dict[str, float]] = None,
    ) -> Optional[Signal]:
        if not ai_signals:
            return base_signal
        weights = source_weights or {}
        score, total_w = 0.0, 1.0
        extra_sources = list(base_signal.sources)
        extra_evidence = list(base_signal.evidence)

        base_sign = 1.0 if base_signal.direction == "long" else (-1.0 if base_signal.direction == "short" else 0.0)
        score += base_sign * self.calibrator.calibrate(base_signal.confidence)

        for s in ai_signals:
            if s.analyst == "risk_critic" and s.direction == "flat" and s.confidence >= 0.75:
                return None
            w = weights.get(s.analyst, 0.35)
            sgn = 1.0 if s.direction == "long" else (-1.0 if s.direction == "short" else 0.0)
            score += sgn * self.calibrator.calibrate(s.confidence) * w
            total_w += w
            extra_sources.extend(s.source_ids)
            if s.thesis:
                extra_evidence.append(f"{s.analyst}: {s.thesis[:80]}")

        norm = score / total_w if total_w > 0 else 0.0
        if abs(norm) < 0.15:
            return None

        merged = base_signal.model_copy(deep=True)
        merged.direction = "long" if norm > 0 else "short"
        merged.confidence = min(0.95, round(0.5 + abs(norm) * 0.45, 3))
        merged.sources = sorted(set(extra_sources))
        merged.evidence = extra_evidence[:10]
        merged.ai_assisted = True
        return merged
