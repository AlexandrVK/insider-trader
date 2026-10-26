"""Отбор трейдеров: лидерборд -> проверка по истории сделок -> оценка "аномальности" тайминга."""
import math
import statistics
import time
from bisect import bisect_right

import hl

CANDLE_MS = 300000  # свечи 5m


def wilson(h, n, z=1.96):
    """Нижняя граница доверительного интервала доли h/n."""
    if n == 0:
        return 0.0
    p = h / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n)


def _perf(row, window):
    for name, v in row["windowPerformances"]:
        if name == window:
            return float(v["pnl"]), float(v["roi"]), float(v.get("vlm") or 0)
    return 0.0, 0.0, 0.0


SKIPPED = []  # кандидаты, отсеянные предфильтром по обороту (для аналитики)


def candidates(cfg):
    out = []
    SKIPPED.clear()
    for r in hl.leaderboard():
        acc = float(r["accountValue"])
        all_pnl, _, _ = _perf(r, "allTime")
        m_pnl, m_roi, m_vlm = _perf(r, "month")
        if acc < cfg["min_account_usd"] or all_pnl < cfg["min_alltime_pnl"]:
            continue
        if m_pnl <= 0 or m_roi < cfg["min_month_roi"]:
            continue
        turnover = m_vlm / acc if acc else 0.0
        why = ("оборот ниже min_turnover (пассивный держатель/хранилище)" if turnover < cfg["min_turnover"] else
               "оборот выше max_turnover (маркет-мейкер/HFT)" if cfg["max_turnover"] and turnover > cfg["max_turnover"] else None)
        if why:  # отсекаем до дорогой проверки истории, но запоминаем - чтобы в аналитике было видно, кого и почему
            SKIPPED.append({"addr": r["ethAddress"], "name": r.get("displayName") or "", "account": acc, "month_pnl": m_pnl,
                            "turnover": round(turnover, 1), "reason": why})
            continue
        out.append({"addr": r["ethAddress"], "name": r.get("displayName") or "", "account": acc,
                    "month_pnl": m_pnl, "alltime_pnl": all_pnl, "turnover": round(turnover, 1)})
    out.sort(key=lambda x: -x["month_pnl"])
    return out[:cfg["scan_candidates"]]


def _candles(coin, since_ms, now_ms, cache):
    if coin not in cache:
        try:
            cs = sorted(hl.candles(coin, since_ms, now_ms), key=lambda c: c["t"])
            cache[coin] = ([c["t"] for c in cs], [float(c["c"]) for c in cs])
        except Exception:
            cache[coin] = None
        time.sleep(0.25)
    return cache[coin]


def _close_at(cs, t_ms):
    """Цена закрытия свечи, в которую попадает t_ms (точность до 5 минут)."""
    times, closes = cs
    i = bisect_right(times, t_ms) - 1
    if i < 0 or t_ms >= times[i] + CANDLE_MS:
        return None
    return closes[i]


def _baseline(coin, side, cs, cfg, cache):
    """Доля "верных" решающих движений при входе в случайный момент в эту же сторону: учитывает общий дрейф рынка."""
    key = (coin, side)
    if key not in cache:
        k = max(1, round(cfg["hit_horizon_min"] * 60000 / CANDLE_MS))
        closes, up, down = cs[1], 0, 0
        for i in range(len(closes) - k):
            m = (closes[i + k] / closes[i] - 1) * 100
            if m >= cfg["hit_pct"]:
                up += 1
            elif m <= -cfg["hit_pct"]:
                down += 1
        cache[key] = 0.5 if up + down == 0 else (up if side == "Open Long" else down) / (up + down)
    return cache[key]


def score_one(c, cfg, cache):
    now_ms = int(time.time() * 1000)
    since = now_ms - cfg["lookback_days"] * 86400000
    raw = [f for f in hl.fills(c["addr"]) if f["time"] >= since]
    spot_n = sum(1 for f in raw if f["coin"].startswith("@"))  # спотовые сделки не учитываем, но считаем - чтобы видеть причину "нет входов"
    allf = [f for f in raw if not f["coin"].startswith("@")]
    # частичные исполнения одного ордера (одна минута) считаем одним входом
    opens = {}
    for f in allf:
        if f["dir"] in ("Open Long", "Open Short"):
            opens.setdefault((f["coin"], f["dir"], f["time"] // 60000), f)
    span_days = max((now_ms - min((f["time"] for f in allf), default=now_ms)) / 86400000, 0.04)
    horizon = cfg["hit_horizon_min"] * 60000
    hit = anti = 0
    base_sum = 0.0
    for f in opens.values():
        if f["time"] + horizon > now_ms - CANDLE_MS:
            continue
        cs = _candles(f["coin"], since, now_ms, cache)
        px_after = _close_at(cs, f["time"] + horizon) if cs else None
        if px_after is None:
            continue
        move = (px_after / float(f["px"]) - 1) * 100 * (1 if f["dir"] == "Open Long" else -1)
        if move >= cfg["hit_pct"]:
            hit += 1
        elif move <= -cfg["hit_pct"]:
            anti += 1
        else:
            continue
        base_sum += _baseline(f["coin"], f["dir"], cs, cfg, cache)
    dec = hit + anti
    base = base_sum / dec if dec else None
    # score: нижняя граница Вилсона минус превышение рыночной базы над 0.5 (на растущем рынке лонги "угадывают" и так)
    score = wilson(hit, dec) - max(0.0, base - 0.5) if dec >= cfg["min_decisive"] else 0.0
    orders = {}  # реальный размер ордера: фрагменты одной минуты суммируются
    for f in allf:
        if f["dir"] in ("Open Long", "Open Short"):
            k = (f["coin"], f["dir"], f["time"] // 60000)
            orders[k] = orders.get(k, 0.0) + float(f["px"]) * float(f.get("sz", 0))
    med = statistics.median(orders.values()) if orders else 0
    # индивидуальный порог события: несколько типичных ордеров трейдера, но не выше общего потолка
    event_usd = round(min(cfg["event_min_usd"], max(cfg["event_floor_usd"], cfg["event_order_mult"] * med))) if med else None
    return {**c, "fills_n": len(allf), "spot_n": spot_n, "median_order_usd": round(med) if med else None, "event_usd": event_usd, "opens": len(opens), "opens_per_day": round(len(opens) / span_days, 1), "decisive": dec,
            "precision": round(hit / dec, 3) if dec else None, "baseline": round(base, 3) if dec else None,
            "edge": round(hit / dec - base, 3) if dec else None, "score": round(score, 3)}


def _reject(r, cfg):
    if r.get("error"):
        return "ошибка: " + r["error"][:80]
    if r.get("fills_n") == 0:
        return f"нет сделок за {cfg['lookback_days']} дн."
    if r["opens_per_day"] > cfg["max_opens_per_day"]:
        return f"HFT ({r['opens_per_day']} входов/день)"
    if r["decisive"] < cfg["min_decisive"]:
        return f"мало решающих входов ({r['decisive']}<{cfg['min_decisive']})"
    if r["score"] < cfg["min_score"]:
        return f"score {r['score']}<{cfg['min_score']}"
    return None


def build(cfg):
    cache, scanned = {}, []
    for c in candidates(cfg):
        try:
            scanned.append(score_one(c, cfg, cache))
        except Exception as e:
            scanned.append({**c, "error": f"{type(e).__name__}: {e}", "score": 0.0, "decisive": 0, "opens_per_day": 0})
        time.sleep(0.1)
    for r in scanned:
        r["reject"] = _reject(r, cfg)
    ok = sorted([r for r in scanned if not r["reject"]], key=lambda r: -r["score"])
    for r in ok[cfg["tracked_max"]:]:
        r["reject"] = "вне топ-" + str(cfg["tracked_max"])
    skipped = sorted(SKIPPED, key=lambda x: -x["month_pnl"])
    return {"ts": time.time(), "tracked": ok[:cfg["tracked_max"]], "scanned": scanned,
            "skipped_total": len(skipped), "skipped": skipped[:30]}
