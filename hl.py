"""Публичное API Hyperliquid (ключи не нужны)."""
import threading
import time

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
_s = requests.Session()


class Limiter:
    """Токен-бакет по весу запросов. У Hyperliquid лимит порядка 1200 единиц веса в минуту на IP; делим его на два бюджета,
    чтобы долгое сканирование трейдеров не мешало опросу позиций."""

    def __init__(self, per_min):
        self.rate, self.cap = per_min / 60.0, float(per_min)
        self.tokens, self.t, self.lock = float(per_min), time.time(), threading.Lock()

    def take(self, weight):
        weight = min(weight, self.cap)
        while True:
            with self.lock:
                now = time.time()
                self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                wait = (weight - self.tokens) / self.rate
            time.sleep(min(wait, 5))

    def penalize(self):
        """После ответа 429 - уйти в минус, чтобы выдержать паузу."""
        with self.lock:
            self.tokens = min(self.tokens, -self.cap / 4)


LIVE = Limiter(400)   # опрос позиций и цен
SCAN = Limiter(700)   # история сделок и свечи при отборе трейдеров


def _req(method, url, limiter=None, weight=0, **kw):
    for i in range(5):
        if limiter:
            limiter.take(weight)
        try:
            r = _s.request(method, url, timeout=20, **kw)
            if r.status_code == 429:
                if limiter:
                    limiter.penalize()
                time.sleep(3 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == 4:
                raise
            time.sleep(1 + i)
    raise RuntimeError("rate limited")


def info(payload, limiter, weight):
    return _req("POST", INFO_URL, limiter, weight, json=payload)


def leaderboard():
    return _req("GET", LEADERBOARD_URL)["leaderboardRows"]


def mids():
    return {k: float(v) for k, v in info({"type": "allMids"}, LIVE, 2).items()}


def positions(addr):
    """-> {coin: {szi, entry, lev}} открытых перп-позиций."""
    d = info({"type": "clearinghouseState", "user": addr}, LIVE, 2)
    out = {}
    for ap in d.get("assetPositions", []):
        p = ap["position"]
        szi = float(p["szi"])
        if szi:
            out[p["coin"]] = {"szi": szi, "entry": float(p["entryPx"]), "lev": p["leverage"]["value"]}
    return out


def fills(addr):
    return info({"type": "userFills", "user": addr}, SCAN, 20)


def candles(coin, start_ms, end_ms, interval="5m"):
    step = {"1m": 60000, "5m": 300000, "15m": 900000}.get(interval, 300000)
    weight = 20 + int((end_ms - start_ms) / step / 60)  # 20 + 1 за каждые 60 свечей
    return info({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}}, SCAN, weight)
