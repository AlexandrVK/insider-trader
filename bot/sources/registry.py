"""Реестр источников информации: лимиты, дедупликация, forward return с поправкой на BTC, Thompson sampling."""
from __future__ import annotations

import hashlib
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..db import Database


@dataclass
class SourceSpec:
    source_id: str
    kind: str
    rate_limit_per_min: int
    tos_note: str
    alpha: float = 2.0
    beta: float = 2.0
    enabled: bool = True
    failures: int = 0
    disabled_until: float = 0.0
    req_timestamps: list[float] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zа-я0-9]{3,}", text.lower()))


class SourceRegistry:
    def __init__(self, db: Database, exploration_floor: float = 0.10, clock: Callable[[], float] = time.time):
        self.db = db
        self.exploration_floor = max(0.10, exploration_floor)
        self.clock = clock
        self.sources: dict[str, SourceSpec] = {}
        self._seen_hashes: set[str] = set()
        self._seen_tokens: list[set[str]] = []

    def register(self, spec: SourceSpec) -> None:
        self.sources[spec.source_id] = spec

    def register_defaults(self) -> None:
        for s in (
            SourceSpec("exchange_public", "exchange", 600, "CCXT public market data, free"),
            SourceSpec("coindesk_rss", "news", 10, "Public RSS feed"),
            SourceSpec("cointelegraph_rss", "news", 10, "Public RSS feed"),
            SourceSpec("google_news_rss", "news", 15, "Public RSS search"),
            SourceSpec("alternative_me_fng", "macro", 10, "Free public API"),
            SourceSpec("defillama", "onchain", 30, "Free public API"),
            SourceSpec("mempool_space", "onchain", 30, "Free public API"),
            SourceSpec("fred", "macro", 30, "Free API key"),
            SourceSpec("hyperliquid_copy", "copytrading", 100, "Public info & leaderboard API"),
        ):
            self.register(s)

    def can_request(self, source_id: str) -> bool:
        s = self.sources.get(source_id)
        if not s:
            return False
        now = self.clock()
        if not s.enabled and now < s.disabled_until:
            return False
        if not s.enabled and now >= s.disabled_until:
            s.enabled = True
            s.failures = 0
        s.req_timestamps = [t for t in s.req_timestamps if now - t < 60.0]
        if len(s.req_timestamps) >= s.rate_limit_per_min:
            return False
        s.req_timestamps.append(now)
        return True

    def record_failure(self, source_id: str, cooldown_sec: float = 1800.0) -> None:
        s = self.sources.get(source_id)
        if not s:
            return
        s.failures += 1
        if s.failures >= 5:
            s.enabled = False
            s.disabled_until = self.clock() + cooldown_sec
            self.db.log_risk_event("source_disabled", {"source_id": source_id, "failures": s.failures}, self.clock())

    def is_duplicate(self, text: str, jaccard_threshold: float = 0.65) -> bool:
        h = hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]
        if h in self._seen_hashes:
            return True
        toks = _tokens(text)
        for prev in self._seen_tokens[-200:]:
            union = len(toks | prev)
            if union > 0 and len(toks & prev) / union >= jaccard_threshold:
                return True
        self._seen_hashes.add(h)
        self._seen_tokens.append(toks)
        if len(self._seen_tokens) > 500:
            self._seen_tokens = self._seen_tokens[-300:]
        return False

    def record_outcome(
        self, source_id: str, symbol: str, direction: int, horizon_min: int,
        forward_ret_pct: float, btc_ret_pct: float
    ) -> None:
        adj_ret = (forward_ret_pct - btc_ret_pct) * direction
        hit = 1 if adj_ret > 0 else 0
        s = self.sources.get(source_id)
        if s:
            if hit:
                s.alpha += 1.0
            else:
                s.beta += 1.0
        self.db.execute(
            "INSERT INTO source_evals (id, source_id, ts, symbol, direction, horizon_min, forward_ret_pct, btc_ret_pct, adj_ret_pct, hit) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, source_id, self.clock(), symbol, direction, horizon_min, forward_ret_pct, btc_ret_pct, adj_ret, hit),
        )

    def sample_weights(self, seed: Optional[int] = None) -> dict[str, float]:
        rng = random.Random(seed) if seed is not None else random
        active = [s for s in self.sources.values() if s.enabled]
        if not active:
            return {}
        draws = {s.source_id: rng.betavariate(s.alpha, s.beta) for s in active}
        tot = sum(draws.values()) or 1.0
        expl = self.exploration_floor / len(active)
        exploit = 1.0 - self.exploration_floor
        return {sid: round(expl + exploit * (val / tot), 4) for sid, val in draws.items()}

    def ranking(self) -> list[dict]:
        out = []
        for s in self.sources.values():
            mean_hit = s.alpha / (s.alpha + s.beta)
            out.append({"source_id": s.source_id, "kind": s.kind, "enabled": s.enabled,
                        "bayes_hit_rate": round(mean_hit, 3), "samples": int(s.alpha + s.beta - 4)})
        return sorted(out, key=lambda x: -x["bayes_hit_rate"])
