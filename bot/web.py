"""Веб-панель состояния бота на aiohttp:
- GET/HEAD /health и GET /favicon.ico — без пароля (для Render и UptimeRobot);
- Локально (не Render): авторизация полностью отключена (слушаем только 127.0.0.1);
- На Render: форма входа /login с ОДНИМ полем пароля (без логина) и подписанной cookie-сессией (как в insider-trade)."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import html
import logging
import os
import secrets
import time
from typing import Callable, Optional

from aiohttp import web

from .storage_s3 import is_render

log = logging.getLogger("bot.web")

SESSION_HOURS = 12

ICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='6' fill='#111'/>"
    "<path d='M5 22l7-8 5 5 10-12' fill='none' stroke='#2ecc71' stroke-width='3' "
    "stroke-linecap='round' stroke-linejoin='round'/></svg>"
)

LOGIN_HTML = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<title>Вход — Crypto Bot</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#eee;display:flex;height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#1c1c1c;padding:24px;border-radius:8px;width:280px;border:1px solid #2a2a2a}
input,button{width:100%;padding:10px;margin-top:10px;box-sizing:border-box;border-radius:4px;border:1px solid #444;background:#111;color:#eee}
button{background:#2a6;border:0;cursor:pointer;font-weight:600}
.e{color:#f66;margin-top:10px;font-size:13px}
</style></head><body>
<form method="post" action="/login">
  <b>Autonomous Crypto Bot</b>
  <input type="password" name="password" placeholder="Пароль" autofocus required>
  <button type="submit">Войти</button>
  __ERR__
</form></body></html>"""

PANEL_HTML = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<title>Crypto Bot (Paper)</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#ddd;margin:0;padding:16px;max-width:1150px;margin:auto}
h1{font-size:18px;margin:0 0 12px}h2{font-size:15px;margin:22px 0 8px;color:#9ab}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.c{background:#1c1c1c;padding:10px 14px;border-radius:6px;min-width:125px;border:1px solid #262626}
.c b{display:block;font-size:17px;color:#fff}.c span{font-size:12px;color:#888}
button,a.b{background:#2a3a4a;color:#fff;border:0;padding:8px 12px;border-radius:4px;cursor:pointer;margin:2px;font-size:13px;text-decoration:none;display:inline-block}
button:hover,a.b:hover{background:#364b60}button.r{background:#6a2a2a}button.r:hover{background:#823333}
table{border-collapse:collapse;width:100%;font-size:12px;background:#161616;border-radius:6px;overflow:hidden}
td,th{border-bottom:1px solid #262626;padding:6px 8px;text-align:left}th{color:#888;background:#1c1c1c}
.g{color:#5c5}.x{color:#e55}.m{color:#888}
.w{background:#4a3a10;padding:8px 12px;border-radius:4px;font-size:13px;margin-bottom:10px}
.t{overflow-x:auto}
</style></head><body>
<h1>Autonomous Crypto Bot v__VERSION__ <small class="m">· paper (виртуальный счёт 24/7)</small></h1>
__WARN__
<div class="cards" id="cards"></div>
<p>
  <button id="bp" onclick="cmd(S.paused?'resume':'pause')">Пауза</button>
  <button onclick="cmd('backup')">Бэкап в S3</button>
  <button class="r" onclick="if(confirm('Сбросить риск-стоп и паузу?'))cmd('reset_halt')">Сброс риск-стопа</button>
  <a class="b" href="/report">Отчёт (MD/HTML)</a>
  <a class="b" href="/analytics.json" download>Скачать JSON</a>
  <button onclick="load()">Обновить</button>
  __LOGOUT__
  <span id="msg" class="m" style="margin-left:8px"></span>
</p>

<h2>Открытые позиции</h2><div class="t" id="pos"></div>
<h2>Веса стратегий (Мета-селектор) и PnL</h2><div class="t" id="strats"></div>
<h2>Последние закрытые сделки</h2><div class="t" id="trades"></div>
<h2>Последние отклонённые сигналы</h2><div class="t" id="rej"></div>
<h2>События риск-контура</h2><div class="t" id="ev"></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tm=t=>t?new Date(t*1000).toLocaleString():'-';
const tbl=(h,rows)=>'<table><tr>'+h.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr>'+
  (rows.length?rows.map(r=>'<tr>'+r.map(x=>'<td>'+(x&&x.h?x.h:esc(x))+'</td>').join('')+'</tr>').join(''):'<tr><td colspan="'+h.length+'" class="m">нет данных</td></tr>')+'</table>';
const pn=v=>({h:'<span class="'+(Number(v)>=0?'g':'x')+'">'+esc(Number(v).toFixed(2))+'</span>'});

let S={};
async function load(){
  try{
    const r=await fetch('/api/status',{headers:{'X-Requested-With':'fetch'}});
    if(r.status===401){location='/login';return}
    if(!r.ok)return;
    S=await r.json();
    const eq=S.equity??0, init=S.initial_capital??10000;
    const ret=((eq/init-1)*100).toFixed(2);
    const stText=S.halted?'РИСК-СТОП':S.paused?'ПАУЗА':(S.phase||'работает');
    const s3=S.s3||{};
    const s3Text=s3.enabled?('S3: '+s3.saves+' ('+s3.uploaded_mb+' MB)'):'Локально';
    const ai=S.ai||{};
    const aiText=ai.last_provider?(ai.last_provider+' ('+ai.llm_ok+' ok)'):('локально');
    const cd=[
      ['Капитал $', eq.toFixed(2)],
      ['Доход, %', ret+'%'],
      ['Net PnL $', Number(S.realized_pnl??0).toFixed(2)],
      ['Gross PnL $', Number(S.gross_pnl_before_fees??0).toFixed(2)],
      ['Комиссии $', Number(S.fees_total??0).toFixed(2)],
      ['Просадка, %', (S.drawdown_pct??0).toFixed(2)+'%'],
      ['Открыто поз.', (S.open_positions||[]).length],
      ['Закрыто сд.', S.n_trades??(S.stats||{}).closed??0],
      ['Биржа', S.exchange||'-'],
      ['ИИ', aiText],
      ['Статус', stText],
      ['Последний цикл', tm(S.last_cycle_ts)],
      ['Хранилище', s3Text]
    ];
    $('cards').innerHTML=cd.map(c=>'<div class="c"><b>'+esc(c[1])+'</b><span>'+esc(c[0])+'</span></div>').join('');
    $('bp').textContent=S.paused?'Продолжить':'Пауза';

    $('pos').innerHTML=tbl(
      ['Символ','Инстр.','Сторона','Стратегия','Вход','Текущая','Стоп','Тейк','uPnL $','Открыта'],
      (S.open_positions||[]).map(p=>[p.symbol,p.instrument,p.side,p.strategy_id,p.entry_px,p.mark_px,p.stop_px,p.take_px||'-',pn(p.upnl),tm(p.entry_ts)])
    );
    $('strats').innerHTML=tbl(
      ['Стратегия','Вес %','PnL $'],
      (S.strategies||[]).map(x=>[x.id,(x.weight*100).toFixed(1)+'%',pn(x.pnl)])
    );
    $('trades').innerHTML=tbl(
      ['Закрыта','Символ','Сторона','Стратегия','Вход','Выход','PnL $','Комиссии','Причина'],
      (S.recent_trades||[]).map(t=>[tm(t.exit_ts),t.symbol,t.side,t.strategy_id,t.entry_px,t.exit_px,pn(t.pnl),Number(t.fees||0).toFixed(2),t.exit_reason])
    );
    $('rej').innerHTML=tbl(
      ['Время','Символ','Сторона','Стратегия','Причины'],
      (S.recent_rejected||[]).map(r=>[tm(r.ts),r.symbol,r.side,r.strategy_id,r.reasons])
    );
    $('ev').innerHTML=tbl(
      ['Время','Событие','Детали'],
      (S.recent_events||[]).map(e=>[tm(e.ts),e.kind,e.detail])
    );
  }catch(e){$('msg').textContent='Ошибка обновления: '+e}
}
async function cmd(action){
  $('msg').textContent='Выполняю…';
  try{
    const r=await fetch('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'fetch'},body:JSON.stringify({action})});
    if(r.status===401){location='/login';return}
    const d=await r.json();
    $('msg').textContent=d.result||d.error||'ok';
    load();
  }catch(e){$('msg').textContent='Ошибка: '+e}
}
load();
setInterval(()=>{if(!document.hidden)load()},30000);
</script></body></html>"""


async def run_web_server(
    stop: asyncio.Event,
    status_fn: Callable[[], dict],
    report_fn: Callable[[], str],
    cmd_fn: Optional[Callable[[str], str]] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> None:
    render = is_render()
    bind_host = host or ("0.0.0.0" if render else "127.0.0.1")
    bind_port = port or int(os.getenv("PORT", "8080"))

    # Локально пароль НЕ спрашивается; на Render требуется ADMIN_PASSWORD >= 12 символов
    pwd = os.getenv("ADMIN_PASSWORD", "")
    auth_on = render
    locked = render and len(pwd) < 12
    secret = (os.getenv("SESSION_SECRET", "") or hashlib.sha256(("sess|" + pwd).encode()).hexdigest()).encode()
    fails: dict[str, list[float]] = collections.defaultdict(list)
    all_fails: list[float] = []

    def _sig(payload: str) -> str:
        return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()

    def _make_token() -> str:
        p = f"{int(time.time()) + SESSION_HOURS * 3600}.{secrets.token_hex(8)}"
        return f"{p}.{_sig(p)}"

    def _authed(req: web.Request) -> bool:
        if not auth_on:
            return True
        tok = req.cookies.get("session", "")
        p, _, sig = tok.rpartition(".")
        try:
            return bool(p) and hmac.compare_digest(sig, _sig(p)) and int(p.split(".")[0]) > time.time()
        except ValueError:
            return False

    def _client_ip(req: web.Request) -> str:
        fwd = req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return fwd or (req.remote or "unknown")

    def _is_locked_out(ip: str) -> bool:
        now = time.time()
        fails[ip] = [t for t in fails[ip] if now - t < 900]
        all_fails[:] = [t for t in all_fails if now - t < 900]
        return len(fails[ip]) >= 5 or len(all_fails) >= 30

    app = web.Application()

    async def handle_health(_req: web.Request) -> web.Response:
        return web.Response(text="Crypto bot is alive!", status=200)

    async def handle_favicon(_req: web.Request) -> web.Response:
        return web.Response(text=ICON_SVG, content_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    async def handle_login_get(_req: web.Request) -> web.Response:
        if not auth_on:
            raise web.HTTPFound("/")
        if locked:
            return web.Response(text="Админка отключена: задайте ADMIN_PASSWORD (>= 12 символов).", status=503)
        return web.Response(text=LOGIN_HTML.replace("__ERR__", ""), content_type="text/html")

    async def handle_login_post(req: web.Request) -> web.Response:
        if not auth_on:
            raise web.HTTPFound("/")
        if locked:
            return web.Response(text="Админка отключена: задайте ADMIN_PASSWORD (>= 12 символов).", status=503)
        ip = _client_ip(req)
        if _is_locked_out(ip):
            return web.Response(
                text=LOGIN_HTML.replace("__ERR__", '<div class="e">Слишком много попыток. Подождите 15 минут.</div>'),
                content_type="text/html", status=429,
            )
        form = await req.post()
        given = str(form.get("password", ""))
        ok = hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(pwd.encode()).digest())
        if not ok:
            fails[ip].append(time.time())
            all_fails.append(time.time())
            await asyncio.sleep(1.0)
            return web.Response(
                text=LOGIN_HTML.replace("__ERR__", '<div class="e">Неверный пароль</div>'),
                content_type="text/html", status=401,
            )
        resp = web.HTTPSeeOther("/")
        resp.set_cookie(
            "session", _make_token(), max_age=SESSION_HOURS * 3600,
            httponly=True, secure=render, samesite="Strict",
        )
        return resp

    async def handle_logout(_req: web.Request) -> web.Response:
        resp = web.HTTPSeeOther("/login" if auth_on else "/")
        resp.del_cookie("session")
        return resp

    async def handle_status(req: web.Request) -> web.Response:
        if locked or not _authed(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(status_fn())

    async def handle_analytics(req: web.Request) -> web.Response:
        if locked or not _authed(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        stamp = time.strftime("analytics_%Y%m%d_%H%M.json", time.gmtime())
        return web.json_response(status_fn(), headers={"Content-Disposition": f'attachment; filename="{stamp}"'})

    async def handle_cmd(req: web.Request) -> web.Response:
        if locked or not _authed(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        if req.headers.get("X-Requested-With") != "fetch":
            return web.json_response({"error": "forbidden"}, status=403)
        if not cmd_fn:
            return web.json_response({"error": "commands disabled"}, status=400)
        try:
            body = await req.json()
            res = await asyncio.to_thread(cmd_fn, str(body.get("action", "")))
            return web.json_response({"result": res})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_report(req: web.Request) -> web.Response:
        if locked:
            return web.Response(text="Админка отключена: задайте ADMIN_PASSWORD (>= 12 символов).", status=503)
        if not _authed(req):
            raise web.HTTPSeeOther("/login")
        md = await asyncio.to_thread(report_fn)
        body = (
            "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Crypto Bot Report</title>"
            "<body style='font-family:system-ui;max-width:960px;margin:24px auto;background:#111;color:#eee;padding:16px'>"
            "<p><a href='/' style='color:#6cf'>&larr; Назад к панели</a></p>"
            f"<pre style='white-space:pre-wrap'>{html.escape(md)}</pre></body></html>"
        )
        return web.Response(text=body, content_type="text/html")

    async def handle_index(req: web.Request) -> web.Response:
        if locked:
            return web.Response(text="Админка отключена: задайте ADMIN_PASSWORD (>= 12 символов).", status=503)
        if not _authed(req):
            raise web.HTTPSeeOther("/login")
        st = status_fn()
        warn = ""
        if not render:
            warn += '<div class="w">Локальный режим: авторизация отключена (сервис слушает только 127.0.0.1).</div>'
        if render and not (st.get("s3") or {}).get("enabled"):
            warn += '<div class="w">Внимание: на Render S3 не настроен — база данных пропадёт при рестарте. Проверьте S3_* переменные.</div>'
        logout_btn = '<form method="post" action="/logout" style="display:inline"><button type="submit">Выйти</button></form>' if auth_on else ""
        page = (
            PANEL_HTML
            .replace("__VERSION__", html.escape(str(st.get("version", ""))))
            .replace("__WARN__", warn)
            .replace("__LOGOUT__", logout_btn)
        )
        return web.Response(text=page, content_type="text/html")

    app.router.add_route("GET", "/health", handle_health)
    app.router.add_route("HEAD", "/health", handle_health)
    app.router.add_get("/favicon.ico", handle_favicon)
    app.router.add_get("/login", handle_login_get)
    app.router.add_post("/login", handle_login_post)
    app.router.add_post("/logout", handle_logout)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/api/cmd", handle_cmd)
    app.router.add_get("/report", handle_report)
    app.router.add_get("/analytics.json", handle_analytics)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, bind_host, bind_port)
    await site.start()
    log.info("Веб-панель запущена", extra={"host": bind_host, "port": bind_port, "auth": auth_on})
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
