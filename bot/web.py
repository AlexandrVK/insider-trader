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
import json
import logging
import math
import os
import secrets
import time
from typing import Any, Callable, Optional

from aiohttp import web

from .storage_s3 import is_render

log = logging.getLogger("bot.web")

SESSION_HOURS = 12


def _sanitize_json(obj: Any) -> Any:
    """Заменяет невалидные для RFC 8259 JSON значения NaN/Infinity на None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj


def _strict_json_dumps(obj: Any) -> str:
    return json.dumps(_sanitize_json(obj), ensure_ascii=False, allow_nan=False)

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
.fg{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px;margin-bottom:12px}
.fi{background:#161616;border:1px solid #262626;border-radius:6px;padding:8px 10px;display:flex;gap:8px;align-items:flex-start;cursor:pointer;position:relative}
.fi:hover{border-color:#4b6584;background:#1a2028}
.fi input{margin-top:3px;width:16px;height:16px;accent-color:#2ecc71;cursor:pointer;flex:0 0 auto}
.fi>div{flex:1;min-width:0}
.fi b{font-size:13px;color:#eee;display:block;padding-right:18px}.fi small{font-size:11px;color:#888;display:block;margin-top:2px}
.tag{display:inline-block;font-size:10px;padding:1px 5px;border-radius:3px;margin-right:4px;background:#243447;color:#9cf}
.q{position:absolute;top:7px;right:8px;width:16px;height:16px;line-height:16px;text-align:center;border-radius:50%;font-size:11px;font-weight:700;background:#243447;color:#9cf}
#ftip{position:fixed;z-index:9999;display:none;pointer-events:none;max-width:360px;background:#0d141c;color:#e6edf3;border:1px solid #3b5570;border-radius:8px;padding:10px 12px;box-shadow:0 8px 24px rgba(0,0,0,.65);font-size:12px;line-height:1.45}
#ftip .ft-h{font-weight:700;color:#fff;margin-bottom:4px;font-size:13px}
#ftip .ft-t{color:#9cf;font-size:11px;margin-bottom:6px}
#ftip .ft-p{margin:4px 0;color:#d0d7de}
#ftip .ft-off{margin-top:6px;padding-top:6px;border-top:1px solid #243447;color:#f2cc60;font-size:11px}
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
  <a class="b" href="/api/agent/report" download>Отчёт API (JSON)</a>
  <button onclick="load()">Обновить</button>
  __LOGOUT__
  <span id="msg" class="m" style="margin-left:8px"></span>
</p>

<h2>Технологии и модули (вкл/выкл · наведите для справки)</h2><div class="fg" id="feats"></div>
<div id="ftip" role="tooltip"></div>
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

const FEAT_META=[
  ['regime_gating','A','Режимный фильтр','Трендовые в trend/high_vol, RSI/BB только в flat',
   'Regime Gating — фильтр рыночного режима (ADX, волатильность, паника)',
   'Разрешает контртрендовые стратегии (RSI/Bollinger) только в боковике (flat, ADX < 22) и блокирует их в тренде, высокой волатильности и панике.',
   'Если выключить: RSI/BB начнут входить против сильных трендов — сделок станет больше, но вырастет риск серий стопов.'],
  ['mtf_filter','A','MTF-фильтр 4h','Сверка трендовых входов со старшим трендом 4h EMA',
   'Multi-Timeframe Filter — фильтр старшего таймфрейма (4h EMA20/EMA50)',
   'Проверяет направление 4h-графика (порог ±0.2% между EMA20 и EMA50) для трендовых стратегий на 1h. Стоит ДО ИИ-аналитиков, отсекая контртренд и экономя квоту LLM.',
   'Если выключить: вырастет число входов и расход ИИ-запросов, но появятся сделки против 4-часового тренда.'],
  ['trailing_stop','A','Безубыток + Chandelier','Перевод в БУ (+1.5 ATR), 50% TP и трейлинг-стоп',
   'Breakeven + Partial TP + Chandelier Trailing Stop',
   'При движении позиции в плюс на +1.5 ATR переводит стоп в безубыток, фиксирует 50% объёма и ведёт остаток скользящим стопом Chandelier.',
   'Если выключить: позиции будут закрываться только по исходным жёстким стопу и тейку без частичной фиксации.'],
  ['fee_edge_filter','A','Фильтр комиссий ≥3x','Отсев сделок с малым потенциалом к издержкам',
   'Fee-to-Edge Filter — отношение потенциала сделки к комиссиям и спреду',
   'Сравнивает расстояние до тейк-профита с полными издержками входа/выхода (2× комиссия + спред) и блокирует входы с запасом меньше 3.0x.',
   'Если выключить: бот начнёт брать узкие движения, где комиссии могут съедать всю прибыль.'],
  ['obi_filter','A','Стакан (OBI фильтр)','Блокировка входа против сильной стены в стакане',
   'Order Book Imbalance — дисбаланс топ-10 уровней биржевого стакана',
   'Оценивает перевес лимитных заявок на покупку vs продажу. Если против направления входа стоит крупная стена (|OBI| > 0.65), сделка отклоняется.',
   'Если выключить: бот перестанет учитывать локальные стены заявок в стакане при открытии позиции.'],
  ['maker_first','A','Maker-First исполнение','Пассивный лимитный вход (низкие maker-комиссии)',
   'Maker-First Execution — приоритет пассивных лимитных ордеров',
   'При спокойном стакане (signed OBI < 0.45) ставит лимитный вход по лучшей цене бида/аска: комиссия 4 bps вместо 10 bps на споте и 2 bps вместо 5 bps на перпах.',
   'Если выключить: все входы идут рыночными taker-ордерами с повышенной комиссией и проскальзыванием.'],
  ['closed_loop_learning','A','Самообучение + CUSUM','Калибратор уверенности, Thompson Sampling, возврат CUSUM',
   'Closed-Loop Learning — замкнутый контур адаптации по реальным сделкам',
   'Калибрует уверенность ИИ (ConfidenceCalibrator), обновляет байесовские веса источников (Thompson Sampling) и включает детектор деградации CUSUM с 7-дневным возвратом.',
   'Если выключить: веса источников и калибровка уверенности перестанут адаптироваться к фактическим исходам.'],
  ['funding_alpha','B','Funding Carry / знак','Сбор высокого фандинга + знак фандинга в RiskEngine',
   'Funding Alpha — учёт знака 8-часового фандинга и стратегия Funding Carry',
   'Блокирует только невыгодный высокий фандинг (>0.1%, который платим мы), пропускает выгодный и активирует стратегию funding_carry для сбора повышенных ставок.',
   'Если выключить: высокий фандинг режется по модулю в обе стороны, а стратегия funding_carry отключается.'],
  ['cross_sectional_momentum','B','Кросс-моментум к BTC','Лонг лидеров и шорт аутсайдеров относительно BTC',
   'Cross-Sectional Momentum — относительная сила альткоинов к BTC',
   'Раз в 4 часа ранжирует топ-12 монет относительно BTC/USDT: открывает лонги по самым сильным активам и шорты по самым слабым.',
   'Если выключить: 4-часовой сканер кросс-секционного моментума перестаёт генерировать сигналы.'],
  ['vol_targeting','B','Volatility Targeting','Масштаб риска (0.35–1.25x) по волатильности BTC',
   'Portfolio Volatility Targeting — таргетирование 20% годовой волатильности',
   'По 48-часовой волатильности BTC масштабирует размер новых позиций в пределах 0.35x–1.25x: режет плечо при шторме и слегка повышает на спокойном рынке.',
   'Если выключить: размер позиции рассчитывается без поправки на общую волатильность BTC (множитель 1.0x).'],
  ['meta_labeling','C','ML Meta-Labeling','Оценка P(win) по контексту и скейлинг позиции',
   'ML Meta-Labeling — логистическая регрессия вероятности успеха сигнала',
   'Оценивает P(win) по режиму, MTF, OBI, фандингу, спреду и ATR; режет слабые сигналы и масштабирует размер. Автоматически ждёт 40 закрытых сделок и до этого не мешает.',
   'Если выключить: ML-фильтрация и скейлинг не включатся даже после накопления 40 сделок.'],
  ['ai_review','ИИ','ИИ-аналитики (LLM)','Каскад Groq → AnyModel → OpenRouter → лексикон',
   'AI Multi-Analyst Review — 4 ИИ-аналитика (News, Macro, On-chain, Risk Critic)',
   'Прогоняет каждый сигнал (кроме random_benchmark) через каскад LLM и агрегатор; может скорректировать уверенность или наложить вето (ai_veto_or_conflict).',
   'Если выключить: бот переходит на чистую алгоритмическую торговлю без запросов к LLM и обновления RSS.'],
  ['copytrading','Copy','Hyperliquid Копитрейдинг','Согласованные входы топ-трейдеров Hyperliquid',
   'Hyperliquid Copytrading — многооконный отбор и копирование топ-трейдеров',
   'Раз в сутки отбирает до 10 устойчивых трейдеров (week/month/allTime) и раз в минуту проверяет их позиции; при согласованном входе ≥2 трейдеров шлёт сигнал в риск-контур.',
   'Если выключить: мониторинг лидерборда и позиций Hyperliquid полностью останавливается.']
];
const FEAT_MAP=Object.fromEntries(FEAT_META.map(x=>[x[0],x]));
let tipKey='';
function posTip(ev){
  const t=$('ftip');if(!t||t.style.display==='none')return;
  const pad=12,tw=t.offsetWidth||320,th=t.offsetHeight||120;
  let x=ev.clientX+14,y=ev.clientY-th-14;
  if(x+tw+pad>window.innerWidth)x=Math.max(pad,window.innerWidth-tw-pad);
  if(y<pad)y=Math.min(window.innerHeight-th-pad,ev.clientY+18);
  t.style.left=x+'px';t.style.top=y+'px';
}
function showTip(k,ev){
  const m=FEAT_MAP[k],t=$('ftip');if(!m||!t)return;
  tipKey=k;
  const on=(S.features||{})[k]!==false;
  t.innerHTML='<div class="ft-h">'+esc(m[2])+' <span class="tag">'+(on?'ВКЛ':'ВЫКЛ')+'</span></div>'+
    '<div class="ft-t">'+esc(m[4])+'</div>'+
    '<div class="ft-p">'+esc(m[5])+'</div>'+
    '<div class="ft-off">'+esc(m[6])+'</div>';
  t.style.display='block';
  posTip(ev);
}
function hideTip(){tipKey='';const t=$('ftip');if(t)t.style.display='none';}

let S={};
async function toggleFeat(k,el){
  $('msg').textContent='Сохраняю '+k+'…';
  try{
    const r=await fetch('/api/features',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'fetch'},body:JSON.stringify({feature:k,enabled:el.checked})});
    if(r.status===401){location='/login';return}
    const d=await r.json();
    if(!r.ok){el.checked=!el.checked;$('msg').textContent=d.error||'ошибка';return}
    $('msg').textContent=k+': '+(el.checked?'включено':'выключено');
    load();
  }catch(e){el.checked=!el.checked;$('msg').textContent='Ошибка: '+e}
}
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
    const ml=S.ml||{};
    const mlText=ml.active?('активен ('+ml.train_acc_pct+'%)'):('сбор '+Number(ml.samples||0)+'/'+Number(ml.min_trades||40));
    const vt=S.vol_targeting||{};
    const cd=[
      ['Капитал $', eq.toFixed(2)],
      ['Доход, %', ret+'%'],
      ['Net PnL $', Number(S.realized_pnl??0).toFixed(2)],
      ['Gross PnL $', Number(S.gross_pnl_before_fees??0).toFixed(2)],
      ['Комиссии $', Number(S.fees_total??0).toFixed(2)],
      ['Просадка, %', (S.drawdown_pct??0).toFixed(2)+'%'],
      ['Открыто поз.', (S.open_positions||[]).length],
      ['Закрыто сд.', S.n_trades??(S.stats||{}).closed??0],
      ['Биржа / Режим', (S.exchange||'-')+' / '+(S.regime||'-')],
      ['Vol-скаляр', (vt.vol_scalar??1).toFixed(2)+'x'],
      ['ML Meta-Label', mlText],
      ['ИИ', aiText],
      ['Статус', stText],
      ['Последний цикл', tm(S.last_cycle_ts)],
      ['Хранилище', s3Text]
    ];
    $('cards').innerHTML=cd.map(c=>'<div class="c"><b>'+esc(c[1])+'</b><span>'+esc(c[0])+'</span></div>').join('');
    $('bp').textContent=S.paused?'Продолжить':'Пауза';

    const fm=S.features||{};
    $('feats').innerHTML=FEAT_META.map(([k,st,name,desc])=>{
      const on=fm[k]!==false;
      return '<label class="fi" data-tip="'+esc(k)+'"><input type="checkbox" data-feat="'+esc(k)+'" '+(on?'checked':'')+' onchange="toggleFeat(this.dataset.feat,this)"><div><b><span class="tag">'+esc(st)+'</span>'+esc(name)+'</b><small>'+esc(desc)+'</small></div><span class="q" aria-hidden="true">?</span></label>';
    }).join('');

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
const featsEl=$('feats');
if(featsEl){
  featsEl.addEventListener('mouseover',e=>{const c=e.target.closest('.fi');if(c&&c.dataset.tip)showTip(c.dataset.tip,e);});
  featsEl.addEventListener('mousemove',e=>{if(tipKey)posTip(e);});
  featsEl.addEventListener('mouseout',e=>{const c=e.target.closest('.fi');if(c&&(!e.relatedTarget||!c.contains(e.relatedTarget)))hideTip();});
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
    feature_fn: Optional[Callable[[str, bool], dict]] = None,
    agent_report_fn: Optional[Callable[[], dict]] = None,
) -> None:
    render = is_render()
    bind_host = host or ("0.0.0.0" if render else "127.0.0.1")
    bind_port = port or int(os.getenv("PORT", "8080"))

    # Локально пароль НЕ спрашивается; на Render требуется ADMIN_PASSWORD >= 12 символов
    pwd = os.getenv("ADMIN_PASSWORD", "")
    api_token = os.getenv("REPORT_API_TOKEN", "").strip() or pwd
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

    def _bearer_authed(req: web.Request) -> bool:
        if not auth_on:
            return True
        if len(api_token) < 12:
            return False
        hdr = req.headers.get("Authorization", "")
        if not hdr.startswith("Bearer "):
            return False
        given = hdr[7:].strip()
        if not given:
            return False
        return hmac.compare_digest(
            hashlib.sha256(given.encode()).digest(),
            hashlib.sha256(api_token.encode()).digest(),
        )

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
        return web.json_response(status_fn(), dumps=_strict_json_dumps)

    async def handle_analytics(req: web.Request) -> web.Response:
        if locked or not _authed(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        stamp = time.strftime("analytics_%Y%m%d_%H%M.json", time.gmtime())
        return web.json_response(
            status_fn(),
            headers={"Content-Disposition": f'attachment; filename="{stamp}"'},
            dumps=_strict_json_dumps,
        )

    async def handle_features(req: web.Request) -> web.Response:
        if locked or not _authed(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        if req.headers.get("X-Requested-With") != "fetch":
            return web.json_response({"error": "forbidden"}, status=403)
        if not feature_fn:
            return web.json_response({"error": "feature toggles disabled"}, status=400)
        try:
            body = await req.json()
            state = await asyncio.to_thread(feature_fn, str(body.get("feature", "")), bool(body.get("enabled", False)))
            return web.json_response({"features": state})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_agent_report(req: web.Request) -> web.Response:
        ip = _client_ip(req)
        if auth_on and _is_locked_out(ip):
            return web.json_response({"error": "too_many_attempts"}, status=429)
        if locked:
            return web.json_response({"error": "admin_password_not_configured"}, status=503)
        if not (_authed(req) or _bearer_authed(req)):
            if req.headers.get("Authorization"):
                fails[ip].append(time.time())
                all_fails.append(time.time())
            return web.json_response({"error": "unauthorized"}, status=401)
        if agent_report_fn:
            data = await asyncio.to_thread(agent_report_fn)
        else:
            md = await asyncio.to_thread(report_fn)
            data = {"status": status_fn(), "report_markdown": md}
        if req.path.endswith(".md"):
            return web.Response(text=str(data.get("report_markdown", "")), content_type="text/markdown", charset="utf-8")
        return web.json_response(data, dumps=_strict_json_dumps)

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
    app.router.add_post("/api/features", handle_features)
    app.router.add_get("/api/agent/report", handle_agent_report)
    app.router.add_get("/api/agent/report.md", handle_agent_report)
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
