"""Публичное API Hyperliquid (ключи не нужны)."""
import time

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
_s = requests.Session()


def _req(method, url, **kw):
    for i in range(4):
        try:
            r = _s.request(method, url, timeout=20, **kw)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == 3:
                raise
            time.sleep(1 + i)
    raise RuntimeError("rate limited")


def info(payload):
    return _req("POST", INFO_URL, json=payload)


def leaderboard():
    return _req("GET", LEADERBOARD_URL)["leaderboardRows"]


def mids():
    return {k: float(v) for k, v in info({"type": "allMids"}).items()}


def positions(addr):
    """-> {coin: {szi, entry, lev}} открытых перп-позиций."""
    d = info({"type": "clearinghouseState", "user": addr})
    out = {}
    for ap in d.get("assetPositions", []):
        p = ap["position"]
        szi = float(p["szi"])
        if szi:
            out[p["coin"]] = {"szi": szi, "entry": float(p["entryPx"]), "lev": p["leverage"]["value"]}
    return out


def fills(addr):
    return info({"type": "userFills", "user": addr})


def candles(coin, start_ms, end_ms, interval="5m"):
    return info({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}})
