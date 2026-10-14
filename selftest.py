"""Офлайн-проверка логики на подставных данных Hyperliquid: python selftest.py"""
import os
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_PASSWORD"] = "correct-horse-battery"

import hl
import scoring
from engine import Engine

A, B = "0x" + "a" * 40, "0x" + "b" * 40
now_ms = int(time.time() * 1000)
price = {"BTC": 100000.0}
pos = {A: {}, B: {}}
hl.mids = lambda: dict(price)
hl.positions = lambda a: pos[a]

# --- 1. scoring: 20 входов в лонг, цена через 30 мин выше на 1% (16 раз) или ниже (4 раза) ---
fills, t0 = [], now_ms - 3 * 86400000
for i in range(20):
    fills.append({"coin": "BTC", "dir": "Open Long", "px": "100", "time": t0 + i * 3600000})
hl.fills = lambda a: fills
def fake_candles(coin, s, e, interval="5m"):
    out = []
    for t in range(s - s % 300000, e, 300000):
        i = (t - t0) // 3600000
        up = i % 5 != 0  # каждый пятый вход проигрышный
        out.append({"t": t, "c": "101.0" if up else "99.0"})
    return out
hl.candles = fake_candles
cfg = {**__import__("config").DEFAULTS}
r = scoring.score_one({"addr": A, "name": "x", "account": 1e6, "month_pnl": 1, "alltime_pnl": 1}, cfg, {})
print("score:", r["decisive"], r["precision"], r["score"])
assert r["decisive"] >= 15 and r["precision"] > 0.7 and r["score"] > 0.5

# --- 2. engine: два трейдера открывают BTC long -> сигнал -> вход -> TP ---
e = Engine()
e.cfg["ai_web"] = False
e.state["traders"]["tracked"] = [{"addr": a, "name": "<b>x</b>", "score": .6, "precision": .8, "decisive": 20,
                                  "opens_per_day": 5, "account": 1e6, "month_pnl": 1e5} for a in (A, B)]
def tick(): e.tick()
tick()                                    # базовые снимки, событий нет
assert not e.state["events"]
for a in (A, B):
    pos[a] = {"BTC": {"szi": 5.0, "entry": 100000.0, "lev": 10}}   # +$500k
tick()
sig = e.state["signals"][-1]
print("signal:", sig["decision"], len(e.state["positions"]))
assert sig["decision"] == "opened" and len(e.state["positions"]) == 1
p = e.state["positions"][0]
assert abs(p["notional"] - 10000 * 1.0 / 2.0) < 1  # risk 1% / sl 2% = 50% капитала
price["BTC"] = 104500.0
tick()
tr = e.state["trades"][-1]
print("trade:", tr["reason"], tr["pnl"])
assert tr["reason"] == "tp" and tr["pnl"] > 0 and not e.state["positions"]

# дневной лимит / дубликат сигнала не создают вход повторно
tick()
assert len([g for g in e.state["signals"]]) == 1

# --- 3. выход вслед за трейдерами ---
e.state["events"].clear(); e.state["signals"].clear()
price["BTC"] = 100000.0
for a in (A, B):
    pos[a] = {}
tick()
for a in (A, B):
    pos[a] = {"BTC": {"szi": -3.0, "entry": 100000.0, "lev": 10}}
tick()
assert e.state["signals"][-1]["side"] == "SHORT" and e.state["positions"]
e.state["positions"][0]["opened"] -= 200
for a in (A, B):
    pos[a] = {}
tick()
assert e.state["trades"][-1]["reason"] == "traders_exit"

# --- 4. аналитика и сохранение ---
a = e.analytics("0.2")
assert a["summary"]["trades"] == 2 and "by_exit_reason" in a
e.save(True)
e2 = Engine()
assert len(e2.state["trades"]) == 2
e.set_config({"signal_k": 2, "ai_web": False})
try:
    e.set_config({"bogus": 1}); raise SystemExit("must fail")
except ValueError:
    pass
print("engine ok")

# --- 4b. веб-проверка ---
import llm, webcheck
sig = {"addrs": [A, B], "side": "LONG", "coin": "BTC", "usd": 1e6}
answers = [{"queries": ["BTC hack"], "veto": False, "reason": ""}, {"queries": [], "veto": True, "reason": "hack"}]
llm.ask_json = lambda p: answers.pop(0)
webcheck.headlines = lambda q: ["Mon | X | BTC exchange hacked"]
v, r, log = webcheck.review(sig)
assert v and log["queries"] == ["BTC hack"] and log["headlines"] == 1
llm.ask_json = lambda p: {"queries": [], "veto": True, "reason": "x"}       # вето без поиска не засчитывается
assert webcheck.review(sig)[0] is False
llm.ask_json = lambda p: None                                                # ИИ недоступен = нет вето
assert webcheck.review(sig)[0] is False

e.cfg["ai_web"] = True
price.update({"ETH": 3000.0, "SOL": 100.0})
for verdict, coin, want in ((False, "ETH", "opened"), (True, "SOL", "ai_veto")):
    webcheck.review = lambda sg, v=verdict: (v, "r", {"verdict": "x"})
    for a in (A, B):
        pos[a] = {}
    e.tick()
    for a in (A, B):
        pos[a] = {coin: {"szi": 1000.0, "entry": price[coin], "lev": 5}}
    e.tick()
    time.sleep(0.5)
    g = e.state["signals"][-1]
    assert g["coin"] == coin and g["decision"] == want and "ai" in g, g
    assert any(p["coin"] == coin for p in e.state["positions"]) == (want == "opened")
print("web-check ok")

# --- 5. авторизация и веб ---
from fastapi.testclient import TestClient
import app as appmod
with TestClient(appmod.app) as c:
    assert c.get("/health").text.startswith("Insider")
    assert c.head("/health").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/api/status").status_code == 401
    assert c.get("/analytics.json").status_code == 401
    assert c.post("/login", data={"password": "wrong"}).status_code == 401
    r = c.post("/login", data={"password": "correct-horse-battery"}, follow_redirects=False)
    assert r.status_code == 303 and "session" in r.cookies
    assert c.get("/api/status").status_code == 200
    assert c.post("/api/cmd", json={"action": "pause"}).status_code == 403          # нет X-Requested-With
    assert c.post("/api/cmd", json={"action": "pause"}, headers={"X-Requested-With": "fetch"}).json()["result"] == "ok"
    assert c.get("/analytics.json").headers["content-disposition"].startswith("attachment")
    c.cookies.set("session", "1.2.deadbeef")
    assert c.get("/api/status").status_code == 401                                  # подделка
    for _ in range(5):
        c.post("/login", data={"password": "x"})
    assert c.post("/login", data={"password": "correct-horse-battery"}).status_code == 429  # блокировка
print("web ok")
