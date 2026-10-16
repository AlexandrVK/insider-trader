import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Локально переменные можно держать в файле .env рядом с кодом (на Render он не нужен; переменные окружения приоритетнее)
_env = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env):
    with open(_env, encoding="utf-8-sig") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

# Render выставляет переменную RENDER автоматически
IS_RENDER = bool(os.getenv("RENDER"))
PORT = int(os.getenv("PORT", "8000"))
HOST = "0.0.0.0" if IS_RENDER else "127.0.0.1"

# Хранилище: на Render - Backblaze B2 (S3), локально - папка data/
_use = os.getenv("USE_S3", "auto").lower()
USE_S3 = IS_RENDER if _use == "auto" else _use in ("1", "true", "yes")
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "").strip()
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_PREFIX = os.getenv("S3_PREFIX", "insider-trader/")

# Админка
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_HOURS = 12

# ИИ (как в message_checker.py бота): AnyModel -> Groq
ANYMODEL_API_KEY = os.getenv("ANYMODEL_API_KEY", "dummy_key").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# Параметры стратегии. Переопределяются через страницу управления (хранятся в config.json).
DEFAULTS = {
    "poll_sec": 30,               # период опроса позиций трейдеров
    # --- отбор трейдеров ---
    "trader_refresh_hours": 24,
    "scan_candidates": 80,        # сколько кандидатов с лидерборда проверять по истории
    "tracked_max": 15,            # сколько лучших отслеживать
    "min_account_usd": 50000,
    "min_alltime_pnl": 100000,
    "min_month_roi": 0.05,        # доля: 0.05 = 5%
    "lookback_days": 14,
    "hit_horizon_min": 30,        # через сколько минут после входа оцениваем движение цены
    "hit_pct": 0.5,               # движение >= этого % считается "решающим"
    "min_decisive": 15,           # минимум решающих входов для оценки
    "min_score": 0.5,             # нижняя граница Вилсона доли верных решающих входов (0.5 = лучше монетки)
    "max_opens_per_day": 400,     # больше - считаем HFT/маркет-мейкером и исключаем
    "max_turnover": 1500,         # отсев до проверки: месячный оборот / размер счёта больше этого (0 = выкл)
    # --- сигнал ---
    "event_min_usd": 50000,       # набор позиции трейдером за окно сигнала (приросты суммируются), считающийся событием
    "signal_k": 2,                # сколько разных трейдеров нужно
    "signal_window_min": 10,
    # --- виртуальный счёт и риск ---
    "start_equity": 10000.0,
    "risk_pct": 1.0,              # % капитала под риском на сделку (до стоп-лосса)
    "sl_pct": 2.0,
    "tp_pct": 4.0,
    "max_leverage": 3.0,          # суммарный номинал позиций / капитал
    "max_open": 5,
    "max_hold_hours": 24,
    "daily_loss_pct": 3.0,        # дневной лимит убытка: после него новые входы блокируются до 00:00 UTC
    "fee_bps": 4.5,               # комиссия тейкера Hyperliquid
    "slippage_bps": 5.0,
    "coins_whitelist": [],        # пусто = любые монеты
    "ai_web": True,               # веб-проверка сигнала через ИИ (поиск новостей) перед входом; false - выключить
}
