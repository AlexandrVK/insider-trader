# Autonomous Crypto Paper-Trading Bot v2.0.9

Исследовательская автономная система **виртуальной (paper)** торговли криптовалютами 24/7.
**Реальных ордеров, торговых API-ключей и обещаний прибыли в проекте нет.** Класс `LiveExecutor` намеренно не реализован — оставлен только интерфейс `Executor`.

---

## Как работает алгоритм бота

Бот работает на `asyncio` как набор изолированных фоновых задач под управлением `Supervisor` (падение одной задачи не останавливает остальные):

1. **Автоподбор биржи и отказоустойчивость (`bot/exchange/hub.py`):**
   При старте бот проверяет доступность публичных API (`bybit`, `okx`, `binance`, `kucoin`, `gate`, `kraken`) без ключей и регистрации, выбирает основную и резервные биржи и автоматически переключается при сбое (`failover`).
2. **Автоотбор универсума активов (`bot/market/universe.py`):**
   Раз в час отбирает топ-12 ликвидных `USDT`-пар (исключая стейблкоины, плечевые токены, пары с широким спредом > 8 bps, малой глубиной стакана < $50k, историей < 90 дней и признаками накрутки объёма) и группирует активы в **кластеры корреляции** (≥ 0.8).
3. **Рыночные данные без lookahead (`bot/market/data.py`):**
   Свечи кэшируются в SQLite, но стратегии получают **только закрытые свечи**. Каждая закрытая свеча обрабатывается ровно один раз.
4. **Определение рыночного режима (`bot/market/indicators.py`):**
   - `panic` — падение ≥ 6% от максимума за 24 свечи + всплеск волатильности > 1.5x (отключает контртрендовые стратегии RSI и Bollinger);
   - `high_vol` — волатильность > 1.6x от базовой;
   - `trend` — `ADX(14) >= 22`;
   - `flat` — боковик (`ADX < 22`).
5. **Пул стратегий и Мета-селектор (`bot/strategies/pool.py`, `bot/strategies/meta_selector.py`):**
   В отдельных виртуальных субпортфелях работают 10 стратегий: `ema_trend`, `macd_trend`, `donchian_breakout`, `rsi_mean_rev`, `bb_mean_rev`, `momentum`, кросс-секционный моментум к BTC (`cross_sectional_momentum`), деривативная стратегия фандинга (`funding_carry`), `flat` (держать кэш) и контрольный бенчмарк `random_benchmark`.
   `MetaSelector` перераспределяет веса по режимам рынка с защитой от самообмана: **карантин** для новых стратегий (3% веса до 10 сделок), поправка на множественное тестирование (**Deflated Sharpe Ratio**), детектор деградации **CUSUM** (с персистентным 7-дневным возвратом в БД) и гарантированная доля исследования (**exploration ≥ 10%**).
6. **Технологии повышения доходности без ущерба безопасности (Этапы A–C, включаются/выключаются галочками прямо в веб-панели):**
   - **Этап A:** режимный фильтр стратегий (`regime_gating`), мультитаймфреймовый фильтр `4h EMA` (`mtf_filter`), перевод в безубыток + 50% Partial TP + Chandelier Trailing Stop (`trailing_stop`), фильтр «потенциал тейка ≥ 3× издержек» (`fee_edge_filter`), фильтр дисбаланса стакана OBI (`obi_filter`), пассивное исполнение `maker_first` (Post-Only с maker-комиссиями `2 bps` перп / `4 bps` спот) и замкнутое самообучение (`closed_loop_learning` — обновление `ConfidenceCalibrator` и Thompson Sampling в `SourceRegistry` после каждой закрытой сделки).
   - **Этап B:** деривативная альфа и учёт знака фандинга (`funding_alpha` + `funding_carry`), кросс-секционный моментум с BTC-нейтрализацией (`cross_sectional_momentum`) и портфельное таргетирование волатильности (`vol_targeting`, масштаб `0.35x–1.25x` по реализованной волатильности BTC).
   - **Этап C:** лёгкий ML-фильтр **Meta-Labeling** (`bot/ml/meta_labeler.py`, L2-логистическая регрессия на чистом `numpy` без тяжёлых зависимостей) — автоматически собирает контекстные фичи сделок, обучается после накопления ≥ 40 закрытых сделок и масштабирует размер позиции (`0x..1x`) в рамках лимитов `RiskEngine`.
7. **Внешние источники, ИИ-аналитики и копитрейдинг (`bot/data/`, `bot/analysis/ai.py`, `bot/copytrading/hyperliquid.py`):**
   - Раз в 15 минут обновляются бесплатные RSS-новости (с дедупликацией в `SourceRegistry`), Fear & Greed, DeFiLlama TVL, mempool.space и FRED.
   - Каждый торговый сигнал (кроме `random_benchmark`) проходит через 4 ИИ-аналитиков (News/Event, Macro, On-chain, Risk Critic) и `SignalAggregator`. Каскад LLM: **`GROQ_API_KEY`** (с автоподбором чат-моделей) → **`ANYMODEL_API_KEY`** → **`OPENROUTER_API_KEY`** (`:free` модели) → **локальный лексиконный fallback** без LLM.
   - Копитрейдинг Hyperliquid раз в сутки потоково обновляет пул трейдеров (с многооконным скорингом устойчивости week/month/allTime) и раз в минуту опрашивает их позиции; согласованный вход ≥ 2 трейдеров поступает в общий конвейер.
   - *Важно:* LLM знает прошлое, поэтому бэктест ИИ-части ненадёжен; доказательством служит только forward-тест в реальном времени.
8. **Жёсткий риск-движок и реалистичное исполнение (`bot/risk.py`, `bot/execution/paper.py`):**
   `RiskEngine` нельзя обойти: он проверяет лимиты убытка (3% день / 7% неделя / 15% полный стоп), кулдаун после 3 убытков подряд, свежесть данных, спред, гэп, расхождение цены с резервной биржей, знак фандинга, OBI, отношение потенциала к издержкам, лимиты кластеров и рассчитывает размер позиции от ATR (риск ≤ 1% капитала) с учётом `vol_targeting` и `meta_labeling`. `PaperExecutor` моделирует задержку (`300 мс`), прохождение по глубине стакана, `maker_first` / taker-режимы, проскальзывание, комиссии и 8-часовой фандинг.
9. **Журнал, отчёты, API для агента и вердикт (`bot/journal.py`, `bot/report.py`, `bot/web.py`):**
   Все исполненные и отклонённые сигналы сохраняются в SQLite (WAL). Команда `python -m bot.report` (и страница `/report`) строит отчёт в Markdown/HTML/CSV с Sharpe, Sortino, Calmar, Monte Carlo перцентилем, bootstrap-интервалом и автоматическим вердиктом (*«не доказано» / «обнадёживающе» / «статистически подтверждено»*).
   Для автоматического запроса отчёта с удалённого Render-сервиса (`https://insider-trader.onrender.com`) добавлены защищённые эндпоинты `GET /api/agent/report` (JSON) и `GET /api/agent/report.md` (Markdown) с авторизацией по `Authorization: Bearer <REPORT_API_TOKEN или ADMIN_PASSWORD>` и локальная CLI-команда `python -m bot remote-report`.

---

## Веб-панель и авторизация

- **Локально (Windows / Linux):** панель открывается на `http://127.0.0.1:8080` **без запроса пароля** (даже если `ADMIN_PASSWORD` задан в `.env`).
- **На Render (Web Service):**
  - `GET /health`, `HEAD /health`, `GET /favicon.ico` — всегда доступны без пароля (для Render Health Check и UptimeRobot).
  - Панель `/` и API защищены страницей `/login` с **одним полем ввода пароля** (без логина) и подписанной cookie-сессией (требуется `ADMIN_PASSWORD` длиной не менее 12 символов).
- **Бэкап БД на Render:** при старте база `data/bot.db` автоматически восстанавливается из Backblaze B2 (`S3_*`), бэкапится раз в час в компактный `bot.db.gz` (без публичного кэша свечей `candles` и с пропуском выгрузки, если данные не менялись) и при остановке (`SIGTERM`).

---

## Быстрый старт

### Локально (Windows / Linux, Python 3.11+)
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Linux: source .venv/bin/activate
pip install --no-compile -r requirements.txt

python -m bot doctor      # проверка доступности бирж из вашей сети
python -m bot selftest    # офлайн-проверка всего конвейера
pytest -v                 # тесты всех 6 этапов
python -m bot run         # запуск 24/7 (или Start.bat на Windows)
python -m bot.report      # генерация отчёта (MD, HTML, CSV)
python -m bot remote-report  # запрос отчёта с удалённого Render через /api/agent/report
```

### Деплой на Render (Docker Web Service)
1. Распакуйте содержимое архива в корень репозитория GitHub и сделайте `git push`.
2. В Render Environment задайте `ADMIN_PASSWORD` (≥ 12 символов), `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_PREFIX=crypto-bot/`, а также опционально `GROQ_API_KEY`, `ANYMODEL_API_KEY`, `OPENROUTER_API_KEY`, `FRED_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Настройте UptimeRobot на `https://<ваш-сервис>.onrender.com/health`.

---

## История версий (Changelog)
- **v2.0.9:** таймеры ребалансировки `MetaSelector` (`meta_last_rebalance_ts` + `meta_weights_json`), 4-часовых сканеров (`cross_sectional_momentum`, `funding_carry`) и 15-минутных снимков портфеля (`snapshots`) теперь сохраняются и восстанавливаются из SQLite/S3 между перезапусками Render (частые редеплои больше не вызывают лишние ребалансировки и внеочередные входы).
- **v2.0.8:** обеспечена полная сохранность самообучения в SQLite и S3-бэкапе между рестартами Render: `SourceRegistry` восстанавливает накопленные `alpha/beta` Thompson Sampling из `source_evals`, `ConfidenceCalibrator` сохраняет и загружает корзины калибровки (`meta.calibrator_bins`), `LLMProvider` сохраняет и восстанавливает телеметрию ИИ (`meta.llm_stats`).
- **v2.0.7:** исправлена ошибка экранирования кавычек в `<script>` веб-панели (`bot/web.py`), из-за которой не отрисовывались карточки, галочки технологий и таблицы (`data-feat` вместо вложенных кавычек).
- **v2.0.6:** реализованы все технологии Этапов A, B и C (`regime_gating`, `mtf_filter` 4h, `trailing_stop` с безубытком/50% Partial TP/Chandelier, `fee_edge_filter`, `obi_filter`, `maker_first`, замкнутое самообучение `ConfidenceCalibrator` + `SourceRegistry` + фикс 7-дневного возврата CUSUM, `funding_alpha` + `funding_carry`, `cross_sectional_momentum`, `vol_targeting`, ML `MetaLabeler` на чистом `numpy`, улучшенный многооконный отбор трейдеров Hyperliquid); в веб-панель добавлены интерактивные галочки включения/выключения каждой технологии (сохраняются в БД/S3), защищённый API отчётов (`GET /api/agent/report` и `/api/agent/report.md` по `Bearer`-токену) и CLI-команда `python -m bot remote-report`.
- **v2.0.5:** оптимизация исходящего трафика на Render: из S3-бэкапа `bot.db.gz` исключён публичный кэш свечей `candles` (сжатие `VACUUM` + `gzip -9`), добавлен пропуск повторной выгрузки неизменённой БД (SHA-256), интервал авто-бэкапа увеличен с 15 до 60 минут, добавлена автоподрезка кэша свечей в локальной БД (до 600 последних на пару/таймфрейм).
- **v2.0.4:** крупная оптимизация потребления RAM под лимит Render 512 MB: последовательный опрос бирж в `autoselect` с немедленным освобождением памяти и ранней остановкой на Render (`stop_early`), очистка тяжёлых сырых `info`-словарей рынков `ccxt`, потоковый JSON-парсер лидерборда Hyperliquid (~40 МБ без пика RAM), ограничение кэша SQLite и LLM, `MALLOC_ARENA_MAX=2` в `Dockerfile`, удаление неиспользуемого `matplotlib`; также исправлена распаковка 3-колоночного стакана для OKX и Kraken.
- **v2.0.3:** исправлен `.gitignore` (`/data/`, `/logs/`, `/reports/` вместо `data/`), чтобы пакет `bot/data/` (`news.py`, `onchain.py`) корректно попадал в Git и деплой на Render; добавлен `.gitattributes` (`*.bat text eol=crlf`) для сохранения CRLF в `Start.bat`.
- **v2.0.2:** исправлен `AttributeError` (`eng.llm` при `eng is None`) в `bot/app.py` при открытии веб-панели во время стартового подбора биржи.
- **v2.0.1:** отключён запрос пароля при локальном запуске; на Render вместо Basic Auth добавлена страница `/login` с одним полем пароля (без логина) и cookie-сессией; в `analytics.json` и на веб-панель добавлена телеметрия ИИ (`last_provider`, `last_model`, счётчик ответов); обновлён `README.md` с полным описанием алгоритма.
- **v2.0.0:** полная сборка этапов 1–6, веб-панель, каскад `Groq -> AnyModel -> OpenRouter`, S3-бэкап SQLite.
