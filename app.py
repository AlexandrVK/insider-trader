"""
Insider Copy Trader — веб-сервис: отслеживает трейдеров Hyperliquid и копирует их входы на виртуальном (paper) счёте.
v0.2
"""
import collections
import hashlib
import hmac
import re
import secrets
import time

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import config
import llm
import ui
from engine import Engine

VERSION = re.search(r"v(\d+\.\d+)", __doc__).group(1)
engine = None

# ---------- авторизация ----------
# На Render админка работает только с паролем >= 12 символов; локально без пароля - открыта (слушаем только 127.0.0.1).
AUTH_ON = config.IS_RENDER or bool(config.ADMIN_PASSWORD)
LOCKED = config.IS_RENDER and len(config.ADMIN_PASSWORD) < 12
SECRET = (config.SESSION_SECRET or hashlib.sha256(("sess|" + config.ADMIN_PASSWORD).encode()).hexdigest()).encode()
fails = collections.defaultdict(list)   # ip -> метки времени неудачных входов
all_fails = []                          # глобальный счётчик (защита от перебора с разных IP)


def _sig(payload):
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_token():
    p = f"{int(time.time()) + config.SESSION_HOURS * 3600}.{secrets.token_hex(8)}"
    return f"{p}.{_sig(p)}"


def authed(request):
    if not AUTH_ON:
        return True
    tok = request.cookies.get("session", "")
    p, _, sig = tok.rpartition(".")
    try:
        return bool(p) and hmac.compare_digest(sig, _sig(p)) and int(p.split(".")[0]) > time.time()
    except ValueError:
        return False


def api_guard(request: Request):
    if LOCKED or not authed(request):
        raise HTTPException(401)
    if request.method == "POST" and request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403)


def client_ip(request):
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or request.client.host


def is_locked_out(ip):
    now = time.time()
    fails[ip] = [t for t in fails[ip] if now - t < 900]
    all_fails[:] = [t for t in all_fails if now - t < 900]
    return len(fails[ip]) >= 5 or len(all_fails) >= 30


# ---------- приложение ----------
@asynccontextmanager
async def lifespan(app):
    global engine
    engine = Engine()
    engine.start()
    yield
    engine.shutdown()  # uvicorn вызывает это при SIGTERM


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def headers(request, call_next):
    r = await call_next(request)
    r.headers["Cache-Control"] = "no-store"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    return r


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return PlainTextResponse("Insider trader is alive!")  # для UptimeRobot


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if LOCKED:
        return HTMLResponse("Админка отключена: задайте ADMIN_PASSWORD (не короче 12 символов).", status_code=503)
    return ui.LOGIN.replace("__ERR__", "")


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if LOCKED:
        raise HTTPException(503)
    ip = client_ip(request)
    if is_locked_out(ip):
        return HTMLResponse(ui.LOGIN.replace("__ERR__", '<div class="e">Слишком много попыток. Подождите 15 минут.</div>'), status_code=429)
    ok = hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(config.ADMIN_PASSWORD.encode()).digest())
    if not ok:
        fails[ip].append(time.time())
        all_fails.append(time.time())
        time.sleep(1)
        return HTMLResponse(ui.LOGIN.replace("__ERR__", '<div class="e">Неверный пароль</div>'), status_code=401)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie("session", make_token(), max_age=config.SESSION_HOURS * 3600, httponly=True,
                 secure=config.IS_RENDER, samesite="strict")
    return r


@app.post("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("session")
    return r


@app.get("/", response_class=HTMLResponse)
def panel(request: Request):
    if LOCKED:
        return HTMLResponse("Админка отключена: задайте ADMIN_PASSWORD (не короче 12 символов).", status_code=503)
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    warn = "" if AUTH_ON else '<div class="w">Локальный режим: авторизация выключена (сервис слушает только 127.0.0.1).</div>'
    if engine.status()["storage"] == "LocalStorage" and config.IS_RENDER:
        warn += '<div class="w">Внимание: на Render данные пишутся локально и пропадут при рестарте - проверьте S3_* переменные.</div>'
    return ui.PANEL.replace("__VERSION__", VERSION).replace("__WARN__", warn)


@app.get("/api/status")
def status(request: Request):
    api_guard(request)
    return engine.status()


@app.post("/api/cmd")
async def cmd(request: Request):
    api_guard(request)
    try:
        return {"result": engine.command((await request.json()).get("action"))}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/config")
def get_config(request: Request):
    api_guard(request)
    return engine.cfg


@app.post("/api/config")
async def set_config(request: Request):
    api_guard(request)
    try:
        engine.set_config(await request.json())
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/analytics.json")
def analytics_json(request: Request):
    api_guard(request)
    name = time.strftime("analytics_%Y%m%d_%H%M.json", time.gmtime())
    return JSONResponse(engine.analytics(VERSION), headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/ai_summary")
def ai_summary(request: Request):
    api_guard(request)
    a = engine.analytics(VERSION)
    brief = {k: a[k] for k in ("summary", "by_coin", "by_exit_reason", "by_traders_count", "signals")}
    provider, text = llm.ask("Это результаты paper-торговли копированием трейдеров Hyperliquid. Кратко (до 12 строк) "
                             "оцени, что работает, что нет, и что стоит поменять в параметрах.\n" + str(brief)[:6000])
    return {"provider": provider, "text": text or "ИИ недоступен (проверьте OPENROUTER_API_KEY / ANYMODEL_API_KEY)."}


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)
