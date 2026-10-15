LOGIN = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход</title><style>body{font-family:system-ui;background:#111;color:#eee;display:flex;height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#1c1c1c;padding:24px;border-radius:8px;width:280px}input,button{width:100%;padding:10px;margin-top:10px;box-sizing:border-box;border-radius:4px;border:1px solid #444;background:#111;color:#eee}
button{background:#2a6;border:0;cursor:pointer}.e{color:#f66;margin-top:10px}</style>
<form method="post" action="/login"><b>Insider Copy Trader</b>
<input type="password" name="password" placeholder="Пароль" autofocus required>
<button>Войти</button>__ERR__</form></html>"""

PANEL = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Insider Copy Trader</title><style>
body{font-family:system-ui;background:#111;color:#ddd;margin:0;padding:16px;max-width:1100px;margin:auto}
h1{font-size:18px}h2{font-size:15px;margin:22px 0 6px;color:#9ab}
.cards{display:flex;flex-wrap:wrap;gap:8px}.c{background:#1c1c1c;padding:10px 14px;border-radius:6px;min-width:120px}
.c b{display:block;font-size:18px;color:#fff}.c span{font-size:12px;color:#888}
button,a.b{background:#2a3a4a;color:#fff;border:0;padding:8px 12px;border-radius:4px;cursor:pointer;margin:2px;font-size:13px;text-decoration:none;display:inline-block}
button.r{background:#6a2a2a}table{border-collapse:collapse;width:100%;font-size:12px}
td,th{border-bottom:1px solid #2a2a2a;padding:4px 6px;text-align:left}th{color:#888}
.g{color:#5c5}.x{color:#e55}textarea{width:100%;height:260px;background:#1c1c1c;color:#ddd;border:1px solid #333;font-family:monospace;font-size:12px;box-sizing:border-box}
pre{background:#1c1c1c;padding:10px;white-space:pre-wrap;font-size:12px}.w{background:#4a3a10;padding:8px;border-radius:4px;font-size:13px}
.t{overflow-x:auto}</style>
<h1>Insider Copy Trader v__VERSION__ <small style="color:#888">· paper (виртуальный счёт на реальных ценах)</small></h1>
__WARN__
<div class="cards" id="cards"></div>
<p><button id="bp" onclick="cmd(S.paused?'resume':'pause')">Пауза</button>
<button onclick="cmd('refresh_traders')">Обновить трейдеров</button>
<button class="r" onclick="if(confirm('Закрыть все позиции?'))cmd('close_all')">Закрыть всё</button>
<button class="r" onclick="if(confirm('Сбросить счёт и историю? Сначала скачайте аналитику!'))cmd('reset')">Сброс счёта</button>
<a class="b" href="/analytics.json" download>Скачать аналитику</a>
<button onclick="ai()">ИИ-сводка</button>
<button onclick="st()">Самотест</button>
<form method="post" action="/logout" style="display:inline"><button>Выйти</button></form></p>
<pre id="ai" style="display:none"></pre>
<pre id="st" style="display:none"></pre>
<h2>Открытые позиции</h2><div class="t" id="pos"></div>
<h2>Последние сигналы</h2><div class="t" id="sig"></div>
<h2>Отслеживаемые трейдеры</h2><div class="t" id="trd"></div>
<h2>Ошибки</h2><div class="t" id="err"></div>
<h2>Настройки (JSON)</h2><textarea id="cfg"></textarea><br><button onclick="saveCfg()">Сохранить настройки</button> <span id="cm"></span>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tm=t=>t?new Date(t*1000).toLocaleString():'-';
let S={};
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json','X-Requested-With':'fetch'},body:b?JSON.stringify(b):undefined});
 if(r.status==401){location='/login';return null}return r.json()}
const tbl=(h,rows)=>'<table><tr>'+h.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+(x&&x.h?x.h:esc(x))+'</td>').join('')+'</tr>').join('')+'</table>';
const pn=v=>({h:'<span class="'+(v>=0?'g':'x')+'">'+esc(v)+'</span>'});
async function load(){S=await api('/api/status');if(!S)return;
 const ret=((S.equity/S.start_equity-1)*100).toFixed(2);
 const cd=[['Капитал',S.equity],['Доход, %',ret],['За сегодня',(S.equity-S.day_start_eq).toFixed(2)],['Позиций',S.positions.length],['Сделок',S.n_trades],
  ['Трейдеров',S.tracked.length+' из '+S.scanned],['Статус',S.paused?'ПАУЗА':S.halted?'ЛИМИТ УБЫТКА':'работает'],['Последний опрос',tm(S.last_tick)],['Хранилище',S.storage]];
 $('cards').innerHTML=cd.map(c=>'<div class="c"><b>'+esc(c[1])+'</b><span>'+esc(c[0])+'</span></div>').join('');
 $('bp').textContent=S.paused?'Продолжить':'Пауза';
 $('pos').innerHTML=tbl(['Монета','Сторона','Вход','Цена','Номинал $','uPnL $','Открыта'],S.positions.map(p=>[p.coin,p.side,p.entry,p.px,p.notional,pn(p.upnl),tm(p.opened)]));
 $('sig').innerHTML=tbl(['Время','Монета','Сторона','Трейдеров','Решение'],S.signals.map(g=>[tm(g.ts),g.coin,g.side,g.n,g.decision]));
 $('trd').innerHTML=(S.refreshing?'<p>Идёт обновление списка…</p>':'<p>Обновлён: '+tm(S.traders_ts)+'</p>')+tbl(['Адрес','Имя','Score','Точность','Решающих','Входов/день','Счёт $','PnL мес. $'],
  S.tracked.map(t=>[t.addr.slice(0,10)+'…',t.name,t.score,t.precision,t.decisive,t.opens_per_day,Math.round(t.account),Math.round(t.month_pnl)]));
 $('err').innerHTML=tbl(['Время','Сообщение'],S.errors.map(e=>[tm(e.ts),e.msg]))}
async function cmd(a){const r=await api('/api/cmd',{action:a});alert(r&&r.result||r&&r.error||'');load()}
async function ai(){const b=$('ai');b.style.display='block';b.textContent='Запрос к ИИ…';const r=await api('/api/ai_summary',{});b.textContent=r?(r.provider?'['+r.provider+']\\n':'')+r.text:''}
async function st(){const b=$('st');b.style.display='block';b.textContent='Проверка хранилища, ИИ и сети (до минуты)…';const r=await api('/api/selftest',{});b.textContent=r?r.report:''}
async function saveCfg(){try{const r=await api('/api/config',JSON.parse($('cfg').value));$('cm').textContent=r.error||'Сохранено'}catch(e){$('cm').textContent='Ошибка JSON: '+e}}
(async()=>{const c=await api('/api/config');if(c)$('cfg').value=JSON.stringify(c,null,2);load();setInterval(load,15000)})();
</script></html>"""

ERROR = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ошибка запуска</title><style>body{font-family:system-ui;background:#111;color:#ddd;padding:16px;max-width:900px;margin:auto}
pre{background:#1c1c1c;padding:10px;white-space:pre-wrap;font-size:12px}button{background:#2a3a4a;color:#fff;border:0;padding:8px 12px;border-radius:4px;cursor:pointer}</style>
<h1>Сервис работает, но торговый движок не запустился</h1><pre>__MSG__</pre>
<button onclick="st()">Самотест</button> <form method="post" action="/logout" style="display:inline"><button>Выйти</button></form>
<pre id="st" style="display:none"></pre>
<script>async function st(){const b=document.getElementById('st');b.style.display='block';b.textContent='Проверка…';
const r=await fetch('/api/selftest',{method:'POST',headers:{'X-Requested-With':'fetch'}});b.textContent=r.status==401?'Сессия истекла, войдите заново':(await r.json()).report}</script></html>"""
