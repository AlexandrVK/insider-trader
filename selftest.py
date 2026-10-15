"""Самотест.
  python selftest.py        - офлайн-проверка логики на подставных данных (без сети)
  python selftest.py live   - боевая проверка: хранилище (S3/локально), ИИ, Hyperliquid, Google News (нужны ваши ключи/.env)"""
import os
import sys
import tempfile
import time

if len(sys.argv) > 1 and sys.argv[1] == "live":
    import diag
    diag.run_all()
    sys.exit(0)

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_PASSWORD"] = "correct-horse-battery"

from fastapi.testclient import TestClient
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
print("score:", r["decisive"], r["precision"], r["baseline"], r["edge"], r["score"])
assert r["decisive"] >= 15 and r["precision"] > 0.7 and r["baseline"] is not None

# рынок, который только растёт: любой лонг "угадывает" -> база высокая, edge ~0, score не проходит порог
hl.candles = lambda coin, s, e, interval="5m": [{"t": t, "c": str(100 + (t - s) / 3e6)} for t in range(s - s % 300000, e, 300000)]
r2 = scoring.score_one({"addr": A, "name": "x", "account": 1e6, "month_pnl": 1, "alltime_pnl": 1}, cfg, {})
print("trend market:", r2["precision"], r2["baseline"], r2["score"])
assert r2["baseline"] > 0.95 and r2["score"] < 0.5
hl.candles = fake_candles

# причины отсева в build()
good, bad, empty = ({"addr": x, "name": x, "account": 1e6, "month_pnl": 1.0, "alltime_pnl": 1.0} for x in ("0xgood", "0xbad", "0xempty"))
scoring.candidates = lambda c: [good, bad, empty]
def fills_by(addr):
    if addr == "0xbad":
        raise RuntimeError("boom")
    return fills if addr == "0xgood" else []
hl.fills = fills_by
res = scoring.build(cfg)
rej = {x["addr"]: x["reject"] for x in res["scanned"]}
print("reject:", rej)
assert rej["0xbad"].startswith("ошибка") and "мало" in rej["0xempty"]
hl.fills = lambda a: fills

# --- 2. engine: два трейдера открывают BTC long -> сигнал -> вход -> TP ---
# --- 1b. ограничитель частоты: пустой бюджет 1200/мин (20/с), запрос веса 10 ждёт ~0.5 с ---
lim = hl.Limiter(1200)
lim.tokens = 0.0
t_ = time.time(); lim.take(10); dt_ = time.time() - t_
assert 0.3 < dt_ < 1.5, dt_
lim.penalize(); assert lim.tokens < 0
print("limiter ok", round(dt_, 2))

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
a = e.analytics("0.4")
assert a["summary"]["trades"] == 2 and "by_exit_reason" in a
import copy, analytics
st_ = copy.deepcopy(e.state)
st_["traders"] = res
a = analytics.build(st_, e.cfg, "0.4", 10000.0, {})
assert len(a["scanned_candidates"]) == 3 and a["scanned_candidates"][1]["reject"].startswith("ошибка")
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
llm.ask_json = lambda p, fast=False, log=None: answers.pop(0)
webcheck.headlines = lambda q: ["Mon | X | BTC exchange hacked"]
v, r, log = webcheck.review(sig)
assert v and log["queries"] == ["BTC hack"] and log["headlines"] == 1
llm.ask_json = lambda p, fast=False, log=None: {"queries": [], "veto": True, "reason": "x"}       # вето без поиска не засчитывается
assert webcheck.review(sig)[0] is False
llm.ask_json = lambda p, fast=False, log=None: None                                                # ИИ недоступен = нет вето
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

# --- 4c. каскад ИИ: AnyModel падает, Groq: первая модель падает, вторая отвечает ---
import importlib, types
importlib.reload(llm)           # вернуть настоящие ask/ask_json (выше они подменены)
import config as cfgmod
cfgmod.GROQ_API_KEY = "gsk_test"
calls = []
class FakeClient:
    def __init__(self, base_url, **kw):
        self.base = base_url
        self.models = types.SimpleNamespace(list=lambda: types.SimpleNamespace(data=[
            types.SimpleNamespace(id="whisper-large", active=True), types.SimpleNamespace(id="llama-3.1-8b-instant", active=True),
            types.SimpleNamespace(id="llama-3.3-70b-versatile", active=True)]))
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
    def create(self, model, **kw):
        calls.append((self.base.split("//")[1].split("/")[0], model))
        if "anymodel" in self.base or model.startswith("llama-3.3"):
            raise RuntimeError("boom")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
            content='<think>x</think>```json {"ok": true} ```'))])
llm.OpenAI = FakeClient
log = []
prov, data = llm.ask('x', parse=llm.extract_json, log=log)
print("cascade:", prov, data, calls)
assert prov == "Groq" and data == {"ok": True}
assert calls[0][0] == "anymodel.org" and calls[1][1].startswith("llama-3.3") and not any("whisper" in c[1] for c in calls)
assert any("AnyModel" in x and "boom" in x for x in log)

# --- 4d. диагностика S3: ключ 003, endpoint 004 ---
from botocore.exceptions import ClientError
cfgmod.USE_S3, cfgmod.S3_ENDPOINT_URL = True, "https://s3.us-west-004.backblazeb2.com"
cfgmod.S3_ACCESS_KEY, cfgmod.S3_SECRET_KEY, cfgmod.S3_BUCKET = "003dd3cd354fd700000000002", "K003abcdefghijklmnopqrstuvwxyz1", "b"
import diag
ex = ClientError({"Error": {"Code": "InvalidAccessKeyId", "Message": "The key is not valid"},
                  "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "r1"}}, "GetObject")
txt = diag.explain(ex)
print(txt)
assert "кластере 003" in txt and "eu-central-003" in txt and "K003abc" not in txt
cfgmod.USE_S3 = False

# --- 4e. деградированный старт: хранилище недоступно, сервис жив и показывает причину ---
import storage
import engine as _e0
orig = _e0.get_storage
def broken(): raise ex
import engine as engmod
engmod.get_storage = broken
import app as appmod2
diag.run_all = lambda: "REPORT"
with TestClient(appmod2.app) as c:
    assert c.get("/health").status_code == 200
    c.post("/login", data={"password": "correct-horse-battery"})
    assert c.get("/api/status").status_code == 503
    assert "не запустился" in c.get("/").text
    assert c.post("/api/selftest", headers={"X-Requested-With": "fetch"}).json()["report"] == "REPORT"
engmod.get_storage = orig
print("diagnostics ok")

# --- 5. авторизация и веб ---
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
