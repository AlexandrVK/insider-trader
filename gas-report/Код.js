/**
 * Ежедневный ИИ-аудит Crypto Bot с отправкой на Gmail.
 * Каскад LLM: Groq (основной, с автоподбором моделей) -> Gemini (резервный).
 *
 * Свойства скрипта (Настройки проекта -> Свойства скрипта):
 *   BOT_URL         - https://insider-trader.onrender.com (по умолчанию)
 *   BOT_TOKEN       - REPORT_API_TOKEN или ADMIN_PASSWORD
 *   GROQ_API_KEY    - основной ключ Groq (gsk_...)
 *   GEMINI_API_KEY  - резервный ключ Gemini (AIza...)
 *   RECIPIENT_EMAIL - (опционально) email получателя
 *   GROQ_MODEL      - (опционально) фиксированная модель Groq
 *   GEMINI_MODEL    - (опционально) фиксированная модель Gemini
 */

function sendDailyBotReport() {
  const props = PropertiesService.getScriptProperties();
  const baseUrl = (props.getProperty('BOT_URL') || 'https://insider-trader.onrender.com').replace(/\/+$/, '');
  const botToken = (props.getProperty('BOT_TOKEN') || '').trim();
  const groqKey = (props.getProperty('GROQ_API_KEY') || '').trim();
  const geminiKey = (props.getProperty('GEMINI_API_KEY') || '').trim();
  const groqModel = (props.getProperty('GROQ_MODEL') || '').trim();
  const geminiModel = (props.getProperty('GEMINI_MODEL') || '').trim();
  const recipient = (props.getProperty('RECIPIENT_EMAIL') || Session.getEffectiveUser().getEmail() || '').trim();

  if (!botToken) {
    throw new Error('Задайте свойство скрипта BOT_TOKEN (REPORT_API_TOKEN или ADMIN_PASSWORD).');
  }
  if (!groqKey && !geminiKey) {
    throw new Error('Задайте хотя бы один ключ: GROQ_API_KEY или GEMINI_API_KEY.');
  }
  if (!recipient) {
    throw new Error('Задайте свойство скрипта RECIPIENT_EMAIL (ваш Gmail).');
  }

  const tz = Session.getScriptTimeZone();
  const now = new Date();
  const nowStr = Utilities.formatDate(now, tz, 'yyyy-MM-dd HH:mm');
  const fileStamp = Utilities.formatDate(now, tz, 'yyyyMMdd_HHmm');

  // 1. Будим Render на случай холодного старта
  try {
    UrlFetchApp.fetch(baseUrl + '/health', { muteHttpExceptions: true });
  } catch (e) {}

  // 2. Забираем полный JSON-отчёт с Render
  const botResp = UrlFetchApp.fetch(baseUrl + '/api/agent/report', {
    method: 'get',
    headers: {
      'Authorization': 'Bearer ' + botToken,
      'Accept': 'application/json'
    },
    muteHttpExceptions: true
  });

  const botCode = botResp.getResponseCode();
  const botRaw = botResp.getContentText('UTF-8');

  if (botCode !== 200) {
    MailApp.sendEmail(
      recipient,
      '[CryptoBot] ОШИБКА получения отчёта (HTTP ' + botCode + ') — ' + nowStr,
      'Не удалось получить отчёт с ' + baseUrl + '/api/agent/report\n' +
      'HTTP статус: ' + botCode + '\n\nОтвет сервера:\n' + botRaw.slice(0, 2000)
    );
    return;
  }

  const data = safeParseJson_(botRaw);
  const st = data.status || {};
  const md = data.report_markdown || '(пустой отчёт)';

  // 3. Собираем структурированные сводки для точного аудита алгоритмов
  const statusLines = buildStatusLines_(data, st, nowStr);
  const analyticsContext = buildAnalyticsContext_(data, st);

  // 4. Каскад LLM: Groq (основной) -> Gemini (резерв)
  let aiResult = { ok: false, error: '' };
  const allErrors = [];
  let groqFailureReason = '';

  if (groqKey) {
    const groqPrompt = buildPrompt_(statusLines, analyticsContext, md, 10000);
    const groqRes = callGroqWithFallback_(groqKey, groqModel, groqPrompt);
    if (groqRes.ok) {
      aiResult = groqRes;
    } else {
      groqFailureReason = groqRes.error;
      allErrors.push('[Groq]\n' + groqRes.error);
    }
  }

  if (!aiResult.ok && geminiKey) {
    const gemPrompt = buildPrompt_(statusLines, analyticsContext, md, 30000);
    const gemRes = callGeminiWithFallback_(geminiKey, geminiModel, gemPrompt);
    if (gemRes.ok) {
      aiResult = gemRes;
      if (groqKey) {
        aiResult.fallbackNote = 'сработал резерв после сбоя Groq';
        aiResult.groqFailureReason = groqFailureReason;
      }
    } else {
      allErrors.push('[Gemini]\n' + gemRes.error);
    }
  }

  if (!aiResult.ok) {
    aiResult.error = allErrors.join('\n\n') || 'Нет доступных LLM-провайдеров.';
  }

  // 5. Формируем и отправляем письмо в Gmail
  const alertTag = st.halted ? ' [РИСК-СТОП]' : (st.paused ? ' [ПАУЗА]' : '');
  const subject = '[CryptoBot AI]' + alertTag + ' ' + nowStr + ' | Equity: ' + fmtNum_(st.equity) + ' | DD: ' + fmtNum_(st.drawdown_pct) + '%';
  const modelLabel = aiResult.ok
    ? aiResult.model + (aiResult.fallbackNote ? ' (' + aiResult.fallbackNote + ')' : '')
    : 'нет';

  const plainBody = [
    aiResult.ok
      ? '=== ИИ-АУДИТ БОТА (' + modelLabel + ') ===\n\n' + aiResult.text
      : '=== ВНИМАНИЕ: ВСЕ ИИ-ПРОВАЙДЕРЫ НЕДОСТУПНЫ ===\n' + aiResult.error,
    '\n\n=== КРАТКИЙ СТАТУС ===\n' + statusLines,
    '\n\nПолный Markdown-отчёт прикреплён во вложении (report_' + fileStamp + '.md).'
  ].join('');

  const htmlBody = buildHtmlEmail_(aiResult, modelLabel, statusLines, nowStr, baseUrl);
  const mdAttachment = Utilities.newBlob(md, 'text/markdown; charset=utf-8', 'report_' + fileStamp + '.md');

  MailApp.sendEmail({
    to: recipient,
    subject: subject,
    body: plainBody,
    htmlBody: htmlBody,
    attachments: [mdAttachment]
  });
}

// Алиас для обратной совместимости с ранее созданными триггерами
function sendDailyGeminiBotReport() {
  return sendDailyBotReport();
}

function buildStatusLines_(data, st, nowStr) {
  const closedCount = st.n_trades != null
    ? st.n_trades
    : (st.stats && st.stats.closed != null ? st.stats.closed : '-');

  const tasksSummary = st.tasks
    ? Object.keys(st.tasks).map(function (k) {
        const t = st.tasks[k] || {};
        return k + ':' + (t.running ? 'ok' : 'stopped') + (t.restarts ? '(restarts=' + t.restarts + ')' : '');
      }).join(', ')
    : '-';

  const rep = data.report || {};
  return [
    'Время: ' + nowStr,
    'Версия бота: ' + (data.version || st.version || '-'),
    'Период (дней): ' + fmtNum_(rep.days) + ' | Доходность (%): ' + fmtNum_(rep.return_pct),
    'Вердикт статистики: ' + (rep.verdict || '-'),
    'Биржа / Режим рынка: ' + (st.exchange || '-') + ' / ' + (st.regime || '-'),
    'Капитал (USDT): ' + fmtNum_(st.equity) + ' (старт: ' + fmtNum_(st.initial_capital) + ')',
    'Реализованный Net PnL (USDT): ' + fmtNum_(st.realized_pnl),
    'Gross PnL до комиссий (USDT): ' + fmtNum_(st.gross_pnl_before_fees),
    'Комиссии (USDT): ' + fmtNum_(st.fees_total) + ' | Фандинг (USDT): ' + fmtNum_(st.funding_total),
    'Просадка (%): ' + fmtNum_(st.drawdown_pct),
    'Открытых позиций: ' + ((st.open_positions || []).length) + ' | Закрытых сделок: ' + closedCount,
    'Статус: ' + (st.halted ? ('РИСК-СТОП (' + (st.halt_reason || '-') + ')') : (st.paused ? 'ПАУЗА' : (st.phase || 'работает'))),
    'Volatility Targeting: ' + (st.vol_targeting ? JSON.stringify(st.vol_targeting) : '-'),
    'ИИ-каскад бота: ' + (st.ai ? JSON.stringify(st.ai) : '-'),
    'ML Meta-Labeler: ' + (st.ml ? JSON.stringify(st.ml) : '-'),
    'S3-бэкап: ' + (st.s3 ? JSON.stringify(st.s3) : '-'),
    'Фоновые задачи: ' + tasksSummary,
    'Флаги технологий (features): ' + (st.features ? JSON.stringify(st.features) : '-')
  ].join('\n');
}

function buildAnalyticsContext_(data, st) {
  const sections = [];
  const repStats = (data.report && data.report.strategy_stats) || {};
  const strategies = st.strategies || [];
  const openPos = st.open_positions || [];
  const trades = data.recent_trades_50 || [];
  const rejected = st.recent_rejected || [];
  const sources = data.source_ranking || [];

  // 1. Все стратегии: веса, текущий PnL и сводная статистика
  if (strategies.length) {
    const lines = strategies.map(function (s) {
      const m = repStats[s.id] || {};
      const parts = [
        s.id,
        'weight=' + fmtNum_((Number(s.weight) || 0) * 100) + '%',
        'live_pnl=' + fmtNum_(s.pnl)
      ];
      const closedN = m.n != null ? m.n : (m.trades != null ? m.trades : m.n_trades);
      const closedTotal = m.total != null ? m.total : m.pnl;
      if (closedN != null) parts.push('closed=' + closedN);
      if (m.win_rate != null) parts.push('win%=' + fmtNum_(m.win_rate));
      if (closedTotal != null) parts.push('closed_pnl=' + fmtNum_(closedTotal));
      if (m.expectancy != null) parts.push('exp=' + fmtNum_(m.expectancy));
      if (m.sharpe != null) parts.push('sharpe=' + fmtNum_(m.sharpe));
      if (m.sortino != null) parts.push('sortino=' + fmtNum_(m.sortino));
      if (m.profit_factor != null) parts.push('PF=' + fmtNum_(m.profit_factor));
      if (m.mc_percentile != null) parts.push('MC%=' + fmtNum_(m.mc_percentile));
      if (m.ci_lo != null && m.ci_hi != null) parts.push('CI=[' + fmtNum_(m.ci_lo) + ';' + fmtNum_(m.ci_hi) + ']');
      return '- ' + parts.join(' | ');
    });
    sections.push('=== СТРАТЕГИИ И ВЕСА МЕТА-СЕЛЕКТОРА ===\n' + lines.join('\n'));
  }

  // 2. Открытые позиции
  if (openPos.length) {
    const posLines = openPos.map(function (p) {
      return '- ' + p.symbol + ' (' + p.instrument + ') ' + p.side +
        ' | strat=' + p.strategy_id +
        ' | entry=' + p.entry_px + ' mark=' + p.mark_px +
        ' | stop=' + p.stop_px + ' take=' + (p.take_px || '-') +
        ' | uPnL=' + fmtNum_(p.upnl);
    });
    sections.push('=== ОТКРЫТЫЕ ПОЗИЦИИ ===\n' + posLines.join('\n'));
  } else {
    sections.push('=== ОТКРЫТЫЕ ПОЗИЦИИ ===\nнет');
  }

  // 3. Агрегация последних 50 закрытых сделок (по стратегиям, режимам, ИИ, причинам выхода)
  if (trades.length) {
    const byStrat = {};
    const byRegime = {};
    const byExit = {};
    const aiAgg = { withAi: { n: 0, wins: 0, pnl: 0 }, noAi: { n: 0, wins: 0, pnl: 0 } };

    trades.forEach(function (t) {
      const sid = t.strategy_id || 'unknown';
      const reg = t.regime || 'unknown';
      const ex = t.exit_reason || 'unknown';
      const pnl = Number(t.pnl) || 0;
      const fees = Number(t.fees) || 0;
      const mae = Number(t.mae_pct) || 0;
      const mfe = Number(t.mfe_pct) || 0;
      const isWin = pnl > 0 ? 1 : 0;

      if (!byStrat[sid]) byStrat[sid] = { n: 0, wins: 0, pnl: 0, fees: 0, maeSum: 0, mfeSum: 0 };
      byStrat[sid].n++;
      byStrat[sid].wins += isWin;
      byStrat[sid].pnl += pnl;
      byStrat[sid].fees += fees;
      byStrat[sid].maeSum += mae;
      byStrat[sid].mfeSum += mfe;

      if (!byRegime[reg]) byRegime[reg] = { n: 0, wins: 0, pnl: 0 };
      byRegime[reg].n++;
      byRegime[reg].wins += isWin;
      byRegime[reg].pnl += pnl;

      byExit[ex] = (byExit[ex] || 0) + 1;

      const bucket = t.ai_assisted ? aiAgg.withAi : aiAgg.noAi;
      bucket.n++;
      bucket.wins += isWin;
      bucket.pnl += pnl;
    });

    const stratSummary = Object.keys(byStrat).map(function (sid) {
      const b = byStrat[sid];
      return '- ' + sid + ': n=' + b.n +
        ', win=' + Math.round((b.wins / b.n) * 100) + '%' +
        ', pnl=' + fmtNum_(b.pnl) +
        ', fees=' + fmtNum_(b.fees) +
        ', avgMAE=' + fmtNum_(b.maeSum / b.n) + '%' +
        ', avgMFE=' + fmtNum_(b.mfeSum / b.n) + '%';
    }).join('\n');

    const regSummary = Object.keys(byRegime).map(function (reg) {
      const b = byRegime[reg];
      return '- ' + reg + ': n=' + b.n + ', win=' + Math.round((b.wins / b.n) * 100) + '%, pnl=' + fmtNum_(b.pnl);
    }).join('\n');

    const exitSummary = Object.keys(byExit).map(function (ex) {
      return ex + '=' + byExit[ex];
    }).join(', ');

    const aiLine = 'С ИИ: n=' + aiAgg.withAi.n + ', pnl=' + fmtNum_(aiAgg.withAi.pnl) +
      ' | Без ИИ: n=' + aiAgg.noAi.n + ', pnl=' + fmtNum_(aiAgg.noAi.pnl);

    const recentList = trades.slice(0, 15).map(function (t) {
      return '- ' + t.symbol + ' ' + t.side + ' | ' + t.strategy_id +
        ' | reg=' + (t.regime || '-') + ' | ai=' + (t.ai_assisted ? '1' : '0') +
        ' | pnl=' + fmtNum_(t.pnl) + ' (fee=' + fmtNum_(t.fees) + ')' +
        ' | MAE=' + fmtNum_(t.mae_pct) + '% MFE=' + fmtNum_(t.mfe_pct) + '%' +
        ' | exit=' + (t.exit_reason || '-');
    }).join('\n');

    sections.push(
      '=== СВОДКА ПО ПОСЛЕДНИМ ' + trades.length + ' ЗАКРЫТЫМ СДЕЛКАМ ===\n' +
      'По стратегиям:\n' + stratSummary + '\n' +
      'По режимам рынка:\n' + regSummary + '\n' +
      'Атрибуция ИИ: ' + aiLine + '\n' +
      'Причины выхода: ' + exitSummary + '\n' +
      'Последние сделки (до 15):\n' + recentList
    );
  } else {
    sections.push('=== ЗАКРЫТЫЕ СДЕЛКИ (recent_trades_50) ===\nзакрытых сделок пока нет');
  }

  // 4. Отклонённые сигналы (проверка фильтров features)
  if (rejected.length) {
    const rejLines = rejected.slice(0, 20).map(function (r) {
      return '- ' + (r.symbol || '-') + ' ' + (r.side || '-') +
        ' | strat=' + (r.strategy_id || '-') +
        ' | reason=' + (r.reasons || '-');
    });
    sections.push('=== ПОСЛЕДНИЕ ОТКЛОНЁННЫЕ СИГНАЛЫ ===\n' + rejLines.join('\n'));
  }

  // 5. Рейтинг источников (Thompson Sampling + Hyperliquid)
  if (sources.length) {
    const srcLines = sources.map(function (s) {
      return '- ' + s.source_id + ' (' + s.kind + ', on=' + Boolean(s.enabled) + '): ' +
        'hit_rate=' + fmtNum_((Number(s.bayes_hit_rate) || 0) * 100) + '%, samples=' + (s.samples || 0);
    });
    sections.push('=== РЕЙТИНГ ИСТОЧНИКОВ (THOMPSON SAMPLING) ===\n' + srcLines.join('\n'));
  }

  return sections.join('\n\n');
}

function buildPrompt_(statusLines, analyticsContext, md, maxMdChars) {
  return [
    'Ты — старший количественный аналитик алгоритмической крипто-торговли (paper trading 24/7).',
    'Проведи предметный аудит отчёта бота на русском языке, строго опираясь только на переданные цифры (ничего не выдумывай).',
    '',
    '=== ПАСПОРТ АРХИТЕКТУРЫ БОТА (УЧИТЫВАЙ ПРИ ВЫВОДАХ) ===',
    '- Рабочий таймфрейм: 1h по топ-12 ликвидным USDT-парам (4 трендовые стратегии + 2 mean-reversion + cross_sectional_momentum + funding_carry + flat + random_benchmark).',
    '- Нормальный темп торговли на 1h: примерно 3–15 сделок в сутки (оценивай активность по сумме ЗАКРЫТЫХ + ОТКРЫТЫХ сделок).',
    '- 10–40 отклонений фильтрами в сутки (mtf_filter, regime_gating, fee_edge_filter, obi_filter) — это ШТАТНАЯ фильтрация шума на ~700+ проверках свечей, а не "слишком жёсткий фильтр".',
    '- mtf_filter сверяет трендовые входы с 4h EMA20/EMA50 и стоит ДО ai_review: он отсекает входы против старшего тренда и экономит квоту LLM.',
    '- meta_labeling при флаге true сам пассивно ждёт набора min_trades=40 и до этого ничего не блокирует — отключать его ради ожидания НЕ нужно.',
    '- flat — это буфер кэша в Мета-селекторе: вес ~73% при карантине остальных стратегий (до 10 сделок по 3%) — штатное поведение, а не проблема.',
    '',
    'Структура ответа:',
    '## 1. Главный итог',
    '- 2–3 предложения: общее состояние портфеля, работает ли бот штатно, есть ли критические алармы.',
    '',
    '## 2. Ключевые метрики и качество статистики',
    '- Капитал, Net PnL vs Gross PnL, влияние комиссий и фандинга, просадка, Sharpe/Sortino/PF/Monte Carlo, статистический вердикт.',
    '',
    '## 3. Аудит алгоритмов (кто эффективен, а кто гонит в минус)',
    '- Сделай компактную Markdown-таблицу по стратегиям (Стратегия | Вес | Сделки | PnL / uPnL | Статус | Вывод).',
    '- Используй строго 3 статуса с защитой от ложных выводов:',
    '  - "Эффективен" — только если >= 10 закрытых сделок, положительный PnL и лучше random_benchmark;',
    '  - "Гонит в минус" — убыточен на >= 10 сделках, либо хуже random_benchmark, либо явно сливает на комиссиях/стопах (если сделок < 10, но есть заметный плавающий или ранний минус — пиши "Ранний минус / мало данных", НЕ предлагай сразу удалять);',
    '  - "Мало данных / карантин" — < 10 закрытых сделок.',
    '- Отдельно отметь, в каких режимах рынка стратегии зарабатывают или теряют.',
    '',
    '## 4. Аудит подключённых технологий (features) и источников',
    '- Кратко оцени по фактам: ai_review (сравнение сделок с ИИ vs без ИИ и частота ai_veto), maker_first + fee_edge_filter (доля комиссий), trailing_stop (по MAE/MFE и exit_reason), фильтры regime_gating / mtf_filter / obi_filter (с учётом паспорта архитектуры!), ML Meta-Labeler, vol_targeting, и рейтинг источников (включая hyperliquid_copy).',
    '',
    '## 5. Варианты действий',
    '- Раздели строго на 3 подпункта:',
    '  1. **Что переключить в веб-панели сейчас** — ЖЁСТКОЕ ПРАВИЛО: если период работы < 3 дней ИЛИ всего сделок (закрытых + открытых) < 20, НЕ предлагай отключать/переключать технологии (кроме явной аварии: 0 открытых и 0 закрытых сделок за > 24 часа, постоянные ошибки биржи/LLM или риск-стоп). При штатной работе на ранней стадии напиши прямо: "Сейчас ничего не переключать — идёт базовый сбор статистики".',
    '  2. **Что проверить/наблюдать в алгоритмах или параметрах (гипотезы на будущее)** (конкретные гипотезы по слабым стратегиям, стопам, фильтрам, которые стоит проверить после набора выборки).',
    '  3. **Что пока НЕ трогать** (из-за карантина, недостаточной выборки или штатной работы).',
    '',
    '=== СТАТУС ===',
    statusLines,
    '',
    analyticsContext,
    '',
    '=== ИСХОДНЫЙ ОТЧЁТ (MARKDOWN) ===',
    md.slice(0, maxMdChars)
  ].join('\n');
}

function getGroqModels_(apiKey, customModel) {
  const skip = ['whisper', 'guard', 'tts', 'orpheus', 'compound'];
  const preferred = ['llama-3.3-70b', 'gpt-oss-120b', 'qwen', 'gpt-oss-20b', 'llama-3.1-8b'];
  const fallback = ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant'];
  const result = [];
  if (customModel) result.push(customModel);

  try {
    const resp = UrlFetchApp.fetch('https://api.groq.com/openai/v1/models', {
      method: 'get',
      headers: { 'Authorization': 'Bearer ' + apiKey },
      muteHttpExceptions: true
    });
    if (resp.getResponseCode() === 200) {
      const list = (JSON.parse(resp.getContentText('UTF-8')).data || [])
        .filter(function (m) {
          const id = String(m.id || '').toLowerCase();
          return m.active !== false && !skip.some(function (s) { return id.indexOf(s) !== -1; });
        })
        .map(function (m) { return m.id; });

      list.sort(function (a, b) {
        const ia = preferred.findIndex(function (p) { return a.toLowerCase().indexOf(p) !== -1; });
        const ib = preferred.findIndex(function (p) { return b.toLowerCase().indexOf(p) !== -1; });
        return (ia === -1 ? preferred.length : ia) - (ib === -1 ? preferred.length : ib);
      });

      list.slice(0, 4).forEach(function (m) {
        if (result.indexOf(m) === -1) result.push(m);
      });
    }
  } catch (e) {}

  fallback.forEach(function (m) {
    if (result.indexOf(m) === -1) result.push(m);
  });
  return result;
}

function callGroqWithFallback_(apiKey, customModel, prompt) {
  const models = getGroqModels_(apiKey, customModel);
  const errors = [];

  for (let i = 0; i < models.length; i++) {
    const model = models[i];
    try {
      const resp = UrlFetchApp.fetch('https://api.groq.com/openai/v1/chat/completions', {
        method: 'post',
        contentType: 'application/json',
        headers: { 'Authorization': 'Bearer ' + apiKey },
        payload: JSON.stringify({
          model: model,
          temperature: 0.2,
          max_tokens: 2600,
          messages: [
            { role: 'system', content: 'Ты — старший количественный аналитик крипто-бота. Пиши чётко, структурно и честно на русском языке.' },
            { role: 'user', content: prompt }
          ]
        }),
        muteHttpExceptions: true
      });
      const code = resp.getResponseCode();
      const body = resp.getContentText('UTF-8');
      if (code === 200) {
        const json = JSON.parse(body);
        const rawText = (json.choices && json.choices[0] && json.choices[0].message && json.choices[0].message.content) || '';
        const text = rawText.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
        if (text) return { ok: true, model: 'Groq / ' + model, text: text };
        errors.push(model + ': пустой ответ');
      } else {
        errors.push(model + ': HTTP ' + code + ' ' + body.slice(0, 200));
      }
    } catch (e) {
      errors.push(model + ': ' + e.message);
    }
  }
  return { ok: false, error: errors.join('\n') };
}

function callGeminiWithFallback_(apiKey, customModel, prompt) {
  const models = [];
  if (customModel) models.push(customModel);
  ['gemini-flash-latest', 'gemini-2.5-flash', 'gemini-2.5-flash-lite'].forEach(function (m) {
    if (models.indexOf(m) === -1) models.push(m);
  });

  const errors = [];
  for (let i = 0; i < models.length; i++) {
    const model = models[i];
    const url = 'https://generativelanguage.googleapis.com/v1beta/models/' + encodeURIComponent(model) + ':generateContent';
    try {
      const resp = UrlFetchApp.fetch(url, {
        method: 'post',
        contentType: 'application/json',
        headers: { 'x-goog-api-key': apiKey },
        payload: JSON.stringify({
          contents: [{ role: 'user', parts: [{ text: prompt }] }],
          generationConfig: { temperature: 0.2, maxOutputTokens: 3000 }
        }),
        muteHttpExceptions: true
      });
      const code = resp.getResponseCode();
      const body = resp.getContentText('UTF-8');
      if (code === 200) {
        const json = JSON.parse(body);
        const parts = (json.candidates && json.candidates[0] && json.candidates[0].content && json.candidates[0].content.parts) || [];
        const text = parts.map(function (p) { return p.text || ''; }).join('\n').trim();
        if (text) return { ok: true, model: 'Gemini / ' + model, text: text };
        errors.push(model + ': пустой ответ');
      } else {
        errors.push(model + ': HTTP ' + code + ' ' + body.slice(0, 200));
      }
    } catch (e) {
      errors.push(model + ': ' + e.message);
    }
  }
  return { ok: false, error: errors.join('\n') };
}

function safeParseJson_(raw) {
  const sanitized = String(raw || '').replace(
    /"(?:\\.|[^"\\])*"|-?\b(?:Infinity|NaN)\b/g,
    function (match) {
      return match.charAt(0) === '"' ? match : 'null';
    }
  );
  return JSON.parse(sanitized);
}

function fmtNum_(v) {
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  return isNaN(n) ? String(v) : n.toFixed(2);
}

function escHtml_(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function inlineMd_(s) {
  return escHtml_(s)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code style="background:#f1f3f5;padding:1px 4px;border-radius:3px;">$1</code>');
}

function mdToHtml_(text) {
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Markdown-таблица
    if (/^\s*\|.+\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:\-|]+\|\s*$/.test(lines[i + 1])) {
      const parseRow = function (r) {
        return r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); });
      };
      const headers = parseRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.+\|\s*$/.test(lines[i])) {
        rows.push(parseRow(lines[i]));
        i++;
      }
      let tbl = '<div style="overflow-x:auto;margin:10px 0;"><table style="border-collapse:collapse;width:100%;font-size:12px;">';
      tbl += '<tr>' + headers.map(function (h) {
        return '<th style="border:1px solid #d1d5db;background:#f3f4f6;padding:6px 8px;text-align:left;">' + inlineMd_(h) + '</th>';
      }).join('') + '</tr>';
      rows.forEach(function (r) {
        tbl += '<tr>' + r.map(function (c) {
          return '<td style="border:1px solid #e5e7eb;padding:6px 8px;vertical-align:top;">' + inlineMd_(c) + '</td>';
        }).join('') + '</tr>';
      });
      tbl += '</table></div>';
      out.push(tbl);
      continue;
    }

    if (/^###\s+/.test(line)) {
      out.push('<h3 style="margin:14px 0 6px;color:#1a365d;font-size:15px;">' + inlineMd_(line.replace(/^###\s+/, '')) + '</h3>');
    } else if (/^##\s+/.test(line)) {
      out.push('<h2 style="margin:16px 0 8px;color:#1a365d;font-size:16px;border-bottom:1px solid #e5e7eb;padding-bottom:4px;">' + inlineMd_(line.replace(/^##\s+/, '')) + '</h2>');
    } else if (/^#\s+/.test(line)) {
      out.push('<h2 style="margin:16px 0 8px;color:#111;font-size:17px;">' + inlineMd_(line.replace(/^#\s+/, '')) + '</h2>');
    } else if (/^\s*[-*]\s+/.test(line)) {
      out.push('<div style="margin:3px 0 3px 12px;">• ' + inlineMd_(line.replace(/^\s*[-*]\s+/, '')) + '</div>');
    } else if (line.trim() === '') {
      out.push('<div style="height:6px;"></div>');
    } else {
      out.push('<div style="margin:3px 0;">' + inlineMd_(line) + '</div>');
    }
    i++;
  }

  return out.join('\n');
}

function buildHtmlEmail_(aiResult, modelLabel, statusLines, nowStr, baseUrl) {
  let aiBlock = '';
  if (aiResult.ok) {
    const groqNote = aiResult.groqFailureReason
      ? '<div style="margin-top:8px;padding:8px 10px;background:#fff8e1;border:1px solid #ffe082;border-radius:4px;font-size:11px;color:#6d4c00;">' +
        '<b>Причина переключения с Groq на Gemini:</b><br>' + escHtml_(aiResult.groqFailureReason).replace(/\n/g, '<br>') + '</div>'
      : '';
    aiBlock =
      '<div style="line-height:1.55;font-size:14px;color:#222;">' + mdToHtml_(aiResult.text) + '</div>' +
      '<p style="font-size:11px;color:#777;margin-top:12px;">Провайдер / модель: ' + escHtml_(modelLabel) + '</p>' +
      groqNote;
  } else {
    aiBlock =
      '<div style="background:#fff3cd;border:1px solid #ffeeba;padding:12px;border-radius:6px;color:#856404;">' +
      '<b>Все ИИ-провайдеры временно недоступны:</b>' +
      '<pre style="white-space:pre-wrap;font-size:12px;">' + escHtml_(aiResult.error) + '</pre></div>';
  }

  return (
    '<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:820px;margin:0 auto;padding:16px;">' +
    '<h2 style="margin:0 0 4px;color:#111;">Аналитика Crypto Bot</h2>' +
    '<p style="margin:0 0 16px;color:#666;font-size:13px;">' + escHtml_(nowStr) + ' · ' +
    '<a href="' + escHtml_(baseUrl) + '" style="color:#0b57d0;">Открыть веб-панель</a></p>' +
    aiBlock +
    '<hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">' +
    '<h3 style="margin:0 0 8px;font-size:14px;color:#444;">Сводка статуса</h3>' +
    '<pre style="background:#f8f9fa;border:1px solid #e5e7eb;padding:12px;border-radius:6px;font-size:12px;white-space:pre-wrap;color:#222;">' +
    escHtml_(statusLines) + '</pre>' +
    '<p style="font-size:12px;color:#666;">Полный исходный Markdown-отчёт прикреплён к письму во вложении.</p>' +
    '</div>'
  );
}
