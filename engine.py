import threading
import time
import uuid
import datetime as dt

import analytics
import config
import hl
import scoring
import webcheck
from storage import get_storage


def sign(x):
    return (x > 0) - (x < 0)


def new_state(cfg):
    return {"cash": cfg["start_equity"], "start_equity": cfg["start_equity"], "positions": [], "trades": [],
            "signals": [], "events": [], "traders": {"ts": 0, "tracked": [], "scanned": []},
            "snap": {}, "snap_ts": {}, "accum": {}, "curve": [],
            "diag": {"ticks_ok": 0, "ticks_fail": 0, "last_tick": 0, "polls": {}}, "paused": False, "halted": False,
            "day": "", "day_start_eq": cfg["start_equity"], "errors": [], "started": time.time()}


class Engine:
    def __init__(self):
        self.store = get_storage()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.refreshing = False
        self.last_try = 0.0
        self.last_save = time.time()
        self.last_tick = 0.0
        self.dirty = False
        self.pending = []
        self.mids = {}
        stored = self.store.load("config.json", {}) or {}
        # старые версии сохраняли настройки целиком и "приклеивали" прежние умолчания: такие значения игнорируем
        self.cfg = {**config.DEFAULTS, **{k: v for k, v in stored.items()
                                           if k in config.DEFAULTS and config.LEGACY_DEFAULTS.get(k) != v}}
        self.state = self.store.load("state.json") or new_state(self.cfg)
        for k, v in new_state(self.cfg).items():
            self.state.setdefault(k, v)

    # ---------- служебное ----------
    def err(self, msg):
        print(f"[ERROR] {msg}")
        with self.lock:
            self.state["errors"].append({"ts": time.time(), "msg": str(msg)[:300]})
            self.state["errors"] = self.state["errors"][-50:]

    def save(self, force=False):
        if not (force or self.dirty or time.time() - self.last_save >= 300):
            return
        try:
            self.store.save("state.json", self.state)
            self.dirty = False
            self.last_save = time.time()
        except Exception as e:
            self.err(f"save: {e}")

    def equity(self, mids):
        s = self.state
        return s["cash"] + sum((mids.get(p["coin"], p["entry"]) - p["entry"]) * p["qty"] * p["sg"]
                               for p in s["positions"])

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def shutdown(self):
        self.stop.set()
        with self.lock:
            self.save(True)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.maybe_refresh()
                self.tick()
            except Exception as e:
                self.err(f"tick: {e}")
                with self.lock:
                    self.state["diag"]["ticks_fail"] += 1
            self.stop.wait(self.cfg["poll_sec"])

    # ---------- отбор трейдеров ----------
    def maybe_refresh(self, force=False):
        s = self.state
        due = time.time() - s["traders"]["ts"] > self.cfg["trader_refresh_hours"] * 3600
        if self.refreshing or not (force or (due and time.time() - self.last_try > 600)):
            return False
        self.refreshing = True
        self.last_try = time.time()

        def work():
            try:
                res = scoring.build(self.cfg)
                with self.lock:
                    s["traders"] = res
                    keep = {t["addr"] for t in res["tracked"]}
                    s["snap"] = {a: v for a, v in s["snap"].items() if a in keep}
                    self.dirty = True
                    self.save(True)
            except Exception as e:
                self.err(f"refresh traders: {e}")
            finally:
                self.refreshing = False

        threading.Thread(target=work, daemon=True).start()
        return True

    # ---------- основной цикл ----------
    def tick(self):
        mids = hl.mids()
        with self.lock:
            tracked = [t["addr"] for t in self.state["traders"]["tracked"]]
        snaps = {}
        for a in tracked:
            try:
                snaps[a] = hl.positions(a)
                self.poll_stat(a, None)
            except Exception as e:
                self.err(f"positions {a[:8]}: {e}")
                self.poll_stat(a, e)
            time.sleep(0.15)
        with self.lock:
            self.mids = mids
            self.process(self.cfg, mids, snaps)
            pending, self.pending = self.pending, []
            self.last_tick = time.time()
            self.state["diag"]["ticks_ok"] += 1
            self.state["diag"]["last_tick"] = self.last_tick
            self.save()
        for item in pending:  # веб-проверка идёт в отдельном потоке, чтобы не задерживать SL/TP
            threading.Thread(target=self.review, args=item, daemon=True).start()

    def poll_stat(self, addr, exc):
        with self.lock:
            d = self.state["diag"]["polls"].setdefault(addr, {"ok": 0, "fail": 0, "last_ok": 0, "last_err": ""})
            if exc is None:
                d["ok"] += 1
                d["last_ok"] = time.time()
            else:
                d["fail"] += 1
                d["last_err"] = str(exc)[:150]

    def review(self, sig, by, opp):
        veto, _, log = webcheck.review(sig)
        with self.lock:
            sig["ai"] = log
            if veto:
                sig["decision"] = "ai_veto"
            else:
                sig["decision"] = self.precheck(self.cfg, sig, by, opp) or self.open_pos(self.cfg, self.mids, sig, by)
            self.dirty = True
            self.save()

    def process(self, cfg, mids, snaps):
        s, t = self.state, time.time()
        win = cfg["signal_window_min"] * 60
        # 1. события: набор позиции трейдером. Приросты копятся в окне сигнала, чтобы не терять набор мелкими долями
        for a, pos in snaps.items():
            old = s["snap"].get(a)
            fresh = t - s["snap_ts"].get(a, 0) <= max(win, cfg["poll_sec"] * 3 + 30)  # после долгого простоя - только базовый снимок
            if old is not None and fresh:
                for coin in set(pos) | set(old):
                    o = old.get(coin, {}).get("szi", 0.0)
                    n = pos.get(coin, {}).get("szi", 0.0)
                    d = n - o
                    if n and sign(d) == sign(n) and coin in mids:
                        usd = abs(n if sign(o) != sign(n) else d) * mids[coin]
                        side = "LONG" if n > 0 else "SHORT"
                        key = f"{a}|{coin}|{side}"
                        acc = [x for x in s["accum"].get(key, []) if t - x[0] <= win] + [[t, usd]]
                        total = sum(x[1] for x in acc)
                        if total >= cfg["event_min_usd"]:
                            s["events"].append({"ts": t, "addr": a, "coin": coin, "usd": round(total), "side": side,
                                                "trader_entry": pos[coin]["entry"]})
                            s["accum"].pop(key, None)
                        else:
                            s["accum"][key] = acc
            s["snap"][a] = pos
            s["snap_ts"][a] = t
        for key in [k for k, v in s["accum"].items() if all(t - x[0] > win for x in v)]:
            del s["accum"][key]
        s["events"] = s["events"][-500:]
        # 2. сигналы: K разных трейдеров в одну сторону по одной монете за окно
        groups = {}
        for e in s["events"]:
            if t - e["ts"] <= win:
                groups.setdefault((e["coin"], e["side"]), {})[e["addr"]] = e
        for (coin, side), by in groups.items():
            if len(by) < cfg["signal_k"]:
                continue
            if any(g["coin"] == coin and g["side"] == side and t - g["ts"] < win for g in s["signals"][-30:]):
                continue
            opp = len(groups.get((coin, "SHORT" if side == "LONG" else "LONG"), {}))
            sig = {"ts": t, "coin": coin, "side": side, "n": len(by), "addrs": list(by), "px": mids[coin],
                   "usd": sum(e["usd"] for e in by.values())}
            sig["decision"] = self.precheck(cfg, sig, by, opp)
            if not sig["decision"]:
                if cfg["ai_web"]:
                    sig["decision"] = "pending"
                    self.pending.append((sig, by, opp))
                else:
                    sig["decision"] = self.open_pos(cfg, mids, sig, by)
            s["signals"].append(sig)
            self.dirty = True
        s["signals"] = s["signals"][-300:]
        # 3. исходы сигналов (для оценки качества сигнала независимо от входа)
        for g in s["signals"][-100:]:
            for h in (5, 15, 60):
                if f"px_{h}" not in g and t - g["ts"] >= h * 60 and g["coin"] in mids:
                    g[f"px_{h}"] = mids[g["coin"]]
        # 4. управление позициями
        for p in list(s["positions"]):
            px = mids.get(p["coin"])
            if not px:
                continue
            sg, why = p["sg"], None
            if (px - p["sl"]) * sg <= 0:
                why = "sl"
            elif (px - p["tp"]) * sg >= 0:
                why = "tp"
            elif t - p["opened"] > cfg["max_hold_hours"] * 3600:
                why = "time"
            elif t - p["opened"] > 120 and not any(
                    sign(s["snap"].get(a, {}).get(p["coin"], {}).get("szi", 0)) == sg for a in p["triggers"]):
                why = "traders_exit"
            if why:
                self.close(p, px, why, cfg)
        # 5. день, лимит убытка, кривая капитала
        eq = self.equity(mids)
        day = dt.datetime.utcnow().strftime("%Y-%m-%d")
        if s["day"] != day:
            s["day"], s["day_start_eq"], s["halted"] = day, eq, False
        if not s["halted"] and eq < s["day_start_eq"] * (1 - cfg["daily_loss_pct"] / 100):
            s["halted"] = True
            self.err(f"дневной лимит убытка достигнут (equity {eq:.2f}), новые входы до 00:00 UTC")
        if not s["curve"] or t - s["curve"][-1][0] >= 600:
            s["curve"].append([int(t), round(eq, 2)])
            s["curve"] = s["curve"][-4000:]

    def precheck(self, cfg, sig, by, opp):
        s, coin = self.state, sig["coin"]
        if s["paused"]:
            return "paused"
        if s["halted"]:
            return "daily_loss_halt"
        if cfg["coins_whitelist"] and coin not in cfg["coins_whitelist"]:
            return "not_whitelisted"
        if any(p["coin"] == coin for p in s["positions"]):
            return "already_open"
        if len(s["positions"]) >= cfg["max_open"]:
            return "max_open"
        if opp >= len(by):
            return "conflict"
        return None

    def open_pos(self, cfg, mids, sig, by):
        s, coin = self.state, sig["coin"]
        px = mids.get(coin, sig["px"])
        eq = self.equity(mids)
        notional = min(eq * cfg["risk_pct"] / cfg["sl_pct"],
                       eq * cfg["max_leverage"] - sum(p["notional"] for p in s["positions"]))
        if notional < 10:
            return "leverage_cap"
        sg = 1 if sig["side"] == "LONG" else -1
        entry = px * (1 + sg * cfg["slippage_bps"] / 1e4)
        fee = notional * cfg["fee_bps"] / 1e4
        s["cash"] -= fee
        tr_entry = sum(e["trader_entry"] for e in by.values()) / len(by)
        s["positions"].append({
            "id": uuid.uuid4().hex[:8], "coin": coin, "side": sig["side"], "sg": sg, "entry": entry,
            "qty": notional / entry, "notional": notional, "fee_in": fee,
            "sl": entry * (1 - sg * cfg["sl_pct"] / 100), "tp": entry * (1 + sg * cfg["tp_pct"] / 100),
            "opened": time.time(), "triggers": list(by), "n_traders": len(by),
            "latency_sec": round(time.time() - min(e["ts"] for e in by.values()), 1),
            "vs_trader_pct": round((entry / tr_entry - 1) * 100 * sg, 3)})
        self.dirty = True
        return "opened"

    def close(self, p, px, why, cfg):
        s = self.state
        exit_px = px * (1 - p["sg"] * cfg["slippage_bps"] / 1e4)
        gross = (exit_px - p["entry"]) * p["qty"] * p["sg"]
        fee_out = p["qty"] * exit_px * cfg["fee_bps"] / 1e4
        s["cash"] += gross - fee_out
        pnl = gross - fee_out - p["fee_in"]
        s["trades"].append({"id": p["id"], "coin": p["coin"], "side": p["side"], "entry": p["entry"], "exit": exit_px,
                            "notional": round(p["notional"], 2), "pnl": round(pnl, 2),
                            "ret_pct": round(pnl / p["notional"] * 100, 3), "fees": round(p["fee_in"] + fee_out, 2),
                            "reason": why, "opened": p["opened"], "closed": time.time(), "triggers": p["triggers"],
                            "n_traders": p["n_traders"], "latency_sec": p["latency_sec"],
                            "vs_trader_pct": p["vs_trader_pct"]})
        s["trades"] = s["trades"][-2000:]
        s["positions"].remove(p)
        self.dirty = True

    # ---------- команды со страницы управления ----------
    def command(self, action):
        if action in ("pause", "resume"):
            with self.lock:
                self.state["paused"] = action == "pause"
                self.dirty = True
                self.save()
            return "ok"
        if action == "clear_errors":
            with self.lock:
                self.state["errors"] = []
                self.dirty = True
                self.save()
            return "ok"
        if action == "refresh_traders":
            return "started" if self.maybe_refresh(force=True) else "already running"
        if action == "close_all":
            mids = hl.mids()
            with self.lock:
                for p in list(self.state["positions"]):
                    self.close(p, mids.get(p["coin"], p["entry"]), "manual", self.cfg)
                self.save()
            return "ok"
        if action == "reset":
            with self.lock:
                old = self.state["traders"]
                self.state = new_state(self.cfg)
                self.state["traders"] = old
                self.dirty = True
                self.save(True)
            return "ok"
        raise ValueError("unknown action")

    def set_config(self, d):
        new = dict(self.cfg)
        for k, v in d.items():
            if k not in config.DEFAULTS:
                raise ValueError(f"неизвестный параметр: {k}")
            dv = config.DEFAULTS[k]
            if isinstance(dv, bool):
                if not isinstance(v, bool):
                    raise ValueError(f"{k}: нужен true/false")
            elif isinstance(dv, list):
                if not isinstance(v, list):
                    raise ValueError(f"{k}: нужен список")
            else:
                v = type(dv)(v)
            new[k] = v
        if new["poll_sec"] < 10:
            raise ValueError("poll_sec не меньше 10")
        with self.lock:
            self.cfg = new
            # храним только отличия от умолчаний: новые умолчания следующих версий подхватываются сами
            self.store.save("config.json", {k: v for k, v in new.items() if v != config.DEFAULTS[k]})

    # ---------- данные для страницы ----------
    def status(self):
        with self.lock:
            s, mids = self.state, self.mids
            return {
                "paused": s["paused"], "halted": s["halted"], "refreshing": self.refreshing,
                "equity": round(self.equity(mids), 2), "start_equity": s["start_equity"],
                "day_start_eq": round(s["day_start_eq"], 2), "n_trades": len(s["trades"]),
                "last_tick": self.last_tick, "traders_ts": s["traders"]["ts"],
                "storage": type(self.store).__name__,
                "positions": [{"coin": p["coin"], "side": p["side"], "entry": p["entry"], "notional": round(p["notional"]),
                               "px": mids.get(p["coin"]), "opened": p["opened"],
                               "upnl": round((mids.get(p["coin"], p["entry"]) - p["entry"]) * p["qty"] * p["sg"], 2)}
                              for p in s["positions"]],
                "tracked": [{k: t.get(k) for k in ("addr", "name", "score", "precision", "decisive", "opens_per_day",
                                                   "account", "month_pnl")} for t in s["traders"]["tracked"]],
                "scanned": len(s["traders"]["scanned"]),
                "signals": [{k: g[k] for k in ("ts", "coin", "side", "n", "decision")} for g in s["signals"][-15:][::-1]],
                "errors": s["errors"][-10:][::-1],
            }

    def analytics(self, version):
        with self.lock:
            return analytics.build(self.state, self.cfg, version, self.equity(self.mids), self.mids)
