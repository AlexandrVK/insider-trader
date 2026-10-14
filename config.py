import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Render выставляет переменную RENDER автоматически
IS_RENDER = bool(os.getenv("RENDER"))
PORT = int(os.getenv("PORT", "8000"))
HOST = "0.0.0.0" if IS_RENDER else "127.0.0.1"

# Хранилище: на Render - Backblaze B2 (S3), локально - папка data/
_use = os.getenv("USE_S3", "auto").lower()
USE_S3 = IS_RENDER if _use == "auto" else _use in ("1", "true", "yes")
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_PREFIX = os.getenv("S3_PREFIX", "insider-trader/")

# Админка
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_HOURS = 12

# ИИ (как в message_checker.py бота): OpenRouter -> AnyModel
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
ANYMODEL_API_KEY = os.getenv("ANYMODEL_API_KEY", "dummy_key")

# Параметры стратегии. Переопределяются через страницу управления (хранятся в config.json).
DEFAULTS = {
    "poll_sec": 30,               # период опроса позиций трейдеров
    # --- отбор трейдеров ---
    "trader_refresh_hours": 24,
    "scan_candidates": 40,        # сколько кандидатов с лидерборда проверять по истории
    "tracked_max": 15,            # сколько лучших отслеживать
    "min_account_usd": 50000,
    "min_alltime_pnl": 100000,
    "min_month_roi": 0.05,        # доля: 0.05 = 5%
    "lookback_days": 14,
    "hit_horizon_min": 30,        # через сколько минут после входа оцениваем движение цены
    "hit_pct": 0.5,               # движение >= этого % считается "решающим"
    "min_decisive": 15,           # минимум решающих входов для оценки
    "min_score": 0.5,             # нижняя граница Вилсона доли верных решающих входов (0.5 = лучше монетки)
    "max_opens_per_day": 200,     # больше - считаем HFT/маркет-мейкером и исключаем
    # --- сигнал ---
    "event_min_usd": 50000,       # минимальный прирост позиции трейдера, считающийся событием
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
