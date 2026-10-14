"""Сводка для анализа: то, что можно скачать и передать Claude для улучшения бота."""
import time


def _stat(items):
    n = len(items)
    w = sum(1 for x in items if x["pnl"] > 0)
    return {"n": n, "win_rate": round(w / n * 100, 1) if n else None, "pnl": round(sum(x["pnl"] for x in items), 2)}


def _group(trades, key):
    d = {}
    for x in trades:
        d.setdefault(str(key(x)), []).append(x)
    return {k: _stat(v) for k, v in sorted(d.items())}


def _max_dd(curve):
    peak, dd = 0.0, 0.0
    for _, e in curve:
        peak = max(peak, e)
        if peak > 0:
            dd = max(dd, (peak - e) / peak * 100)
    return round(dd, 2)


def _hold_bucket(x):
    h = (x["closed"] - x["opened"]) / 3600
    return "<1h" if h < 1 else "1-6h" if h < 6 else "6-24h" if h < 24 else ">=24h"


def _signal_outcomes(signals):
    """Куда пошла цена после сигнала (в сторону сигнала), независимо от того, входили мы или нет."""
    res = {}
    for h in (5, 15, 60):
        mv = [(g[f"px_{h}"] / g["px"] - 1) * 100 * (1 if g["side"] == "LONG" else -1)
              for g in signals if f"px_{h}" in g and g["px"]]
        res[f"{h}m"] = {"n": len(mv),
                        "avg_move_pct": round(sum(mv) / len(mv), 3) if mv else None,
                        "up_rate_pct": round(sum(1 for m in mv if m > 0) / len(mv) * 100, 1) if mv else None}
    return res


def build(s, cfg, version, equity, mids):
    tr = s["trades"]
    wins = [x for x in tr if x["pnl"] > 0]
    losses = [x for x in tr if x["pnl"] <= 0]
    gp, gl = sum(x["pnl"] for x in wins), -sum(x["pnl"] for x in losses)
    unreal = equity - s["cash"] - 0.0
    traders = {}
    for t in s["traders"]["tracked"]:
        traders[t["addr"]] = {"name": t.get("name"), "score": t["score"], "precision": t["precision"],
                              "decisive": t["decisive"], "opens_per_day": t["opens_per_day"],
                              "account_usd": round(t["account"]), "month_pnl": round(t["month_pnl"]),
                              "events": 0, "trades": 0, "attributed_pnl": 0.0}
    for e in s["events"]:
        if e["addr"] in traders:
            traders[e["addr"]]["events"] += 1
    for x in tr:
        for a in x["triggers"]:
            if a in traders:
                traders[a]["trades"] += 1
                traders[a]["attributed_pnl"] = round(traders[a]["attributed_pnl"] + x["pnl"] / len(x["triggers"]), 2)
    decisions = {}
    for g in s["signals"]:
        decisions[g["decision"]] = decisions.get(g["decision"], 0) + 1
    curve = s["curve"]
    step = max(1, len(curve) // 300)
    return {
        "version": version,
        "generated_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "notes": "Paper-торговля: вход по mid-цене Hyperliquid +/- проскальзывание, комиссия и SL/TP проверяются раз в poll_sec. "
                 "latency_sec - задержка от первого события трейдера до нашего входа; vs_trader_pct > 0 - вошли хуже трейдера.",
        "since_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(s["started"])),
        "summary": {
            "equity": round(equity, 2), "start_equity": s["start_equity"],
            "return_pct": round((equity / s["start_equity"] - 1) * 100, 2),
            "realized_pnl": round(sum(x["pnl"] for x in tr), 2), "unrealized_pnl": round(unreal, 2),
            "open_positions": len(s["positions"]), "trades": len(tr),
            "win_rate_pct": round(len(wins) / len(tr) * 100, 1) if tr else None,
            "avg_win": round(gp / len(wins), 2) if wins else None,
            "avg_loss": round(-gl / len(losses), 2) if losses else None,
            "profit_factor": round(gp / gl, 2) if gl else None,
            "expectancy_per_trade": round(sum(x["pnl"] for x in tr) / len(tr), 2) if tr else None,
            "max_drawdown_pct": _max_dd(curve),
            "fees_total": round(sum(x["fees"] for x in tr), 2),
            "avg_latency_sec": round(sum(x["latency_sec"] for x in tr) / len(tr), 1) if tr else None,
            "avg_vs_trader_pct": round(sum(x["vs_trader_pct"] for x in tr) / len(tr), 3) if tr else None,
        },
        "by_coin": _group(tr, lambda x: x["coin"]),
        "by_side": _group(tr, lambda x: x["side"]),
        "by_exit_reason": _group(tr, lambda x: x["reason"]),
        "by_traders_count": _group(tr, lambda x: x["n_traders"]),
        "by_hold_time": _group(tr, _hold_bucket),
        "signals": {"total": len(s["signals"]), "by_decision": decisions, "outcomes": _signal_outcomes(s["signals"]),
                    "outcomes_ai_vetoed": _signal_outcomes([g for g in s["signals"] if g["decision"] == "ai_veto"]),
                    "outcomes_opened": _signal_outcomes([g for g in s["signals"] if g["decision"] == "opened"])},
        "traders": traders,
        "scanned_not_selected": len([t for t in s["traders"]["scanned"]]) - len(traders),
        "open_positions": [{k: x[k] for k in ("coin", "side", "entry", "notional", "opened", "n_traders")} for x in s["positions"]],
        "config": cfg,
        "recent_errors": s["errors"][-20:],
        "trades_last_200": tr[-200:],
        "signals_last_100": s["signals"][-100:],
        "equity_curve": curve[::step],
    }
