"""Бесплатные ончейн-данные: mempool.space (BTC комиссии) и DeFiLlama (TVL)."""
from __future__ import annotations

import time
from typing import Optional

import requests


def fetch_btc_mempool_fees(timeout: float = 10.0) -> Optional[dict]:
    r = requests.get("https://mempool.space/api/v1/fees/recommended", timeout=timeout)
    r.raise_for_status()
    d = r.json()
    return {
        "fastest_sat_vb": float(d.get("fastestFee", 0)),
        "half_hour_sat_vb": float(d.get("halfHourFee", 0)),
        "hour_sat_vb": float(d.get("hourFee", 0)),
        "ts": time.time(),
    }


def fetch_defillama_tvl(timeout: float = 10.0) -> Optional[dict]:
    r = requests.get("https://api.llama.fi/v2/historicalChainTvl", timeout=timeout)
    r.raise_for_status()
    rows = r.json()
    if len(rows) < 2:
        return None
    cur, prev = float(rows[-1]["tvl"]), float(rows[-2]["tvl"])
    chg_pct = ((cur / prev) - 1.0) * 100.0 if prev > 0 else 0.0
    return {"tvl_usd": cur, "change_24h_pct": round(chg_pct, 3), "ts": time.time()}
