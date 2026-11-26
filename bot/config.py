"""Конфигурация: config.yaml + переменные окружения (.env). Режим только paper."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class CapitalCfg(BaseModel):
    initial_usdt: float = Field(10000.0, gt=0)


class ExchangeCfg(BaseModel):
    candidates: list[str] = ["bybit", "okx", "binance", "kucoin", "gate", "kraken"]
    primary: str = "auto"
    timeout_s: float = 15.0
    probe_symbol: str = "BTC/USDT"
    use_websocket: bool = False
    min_backups: int = 1


class UniverseCfg(BaseModel):
    quote: str = "USDT"
    size: int = Field(12, ge=1)
    min_quote_volume_usd: float = 20e6
    max_spread_bps: float = 8.0
    min_depth_usd: float = 50_000.0
    min_history_days: int = 90
    refresh_minutes: int = 60
    exclude_symbols: list[str] = []
    wash_volume_ratio: float = 0.35


class InstrumentsCfg(BaseModel):
    spot: bool = True
    perp: bool = True
    max_leverage: float = Field(2.0, ge=1.0, le=5.0)


class ExecutionCfg(BaseModel):
    taker_fee_bps: float = 10.0
    maker_fee_bps: float = 10.0
    perp_taker_fee_bps: float = 5.0
    latency_ms: int = 300
    max_slippage_bps: float = 30.0
    max_participation: float = 0.25
    min_fill_ratio: float = 0.5
    min_notional_usd: float = 10.0


class RiskCfg(BaseModel):
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 20.0
    max_positions: int = 5
    max_gross_exposure_pct: float = 100.0
    max_cluster_positions: int = 2
    max_cluster_exposure_pct: float = 35.0
    corr_threshold: float = 0.8
    daily_loss_limit_pct: float = 3.0
    weekly_loss_limit_pct: float = 7.0
    max_drawdown_pct: float = 15.0
    cooldown_losses: int = 3
    cooldown_minutes: int = 240
    max_data_age_sec: float = 180.0
    max_spread_bps: float = 15.0
    max_gap_pct: float = 8.0
    max_divergence_bps: float = 80.0
    max_funding_rate_abs: float = 0.001
    default_stop_atr_mult: float = 2.0
    blackout_minutes: int = 30
    blackout_windows: list[list[float]] = []


class StrategiesCfg(BaseModel):
    enabled: list[str] = [
        "ema_trend", "macd_trend", "donchian_breakout",
        "rsi_mean_rev", "bb_mean_rev", "momentum", "flat", "random_benchmark",
    ]
    allocations: dict[str, float] = {}
    ema_fast: int = 20
    ema_slow: int = 50
    timeframe: str = "1h"
    random_prob: float = 0.03
    random_seed: int = 42
    exploration_floor: float = 0.10
    rebalance_days: float = 3.0


class AiCfg(BaseModel):
    enabled: bool = True
    cache_ttl_sec: int = 900


class CopyTradingCfg(BaseModel):
    enabled: bool = True
    provider: str = "hyperliquid"
    refresh_hours: int = 24
    tracked_max: int = 10
    copy_delay_min: int = 3
    min_decisive: int = 15
    min_score: float = 0.5


class VerdictCfg(BaseModel):
    min_weeks: float = 6.0
    min_trades: int = 100
    min_sharpe: float = 1.0
    max_drawdown_pct: float = 12.0
    min_profit_factor: float = 1.3


class StorageCfg(BaseModel):
    db_path: str = "data/bot.db"
    log_dir: str = "logs"
    report_dir: str = "reports"
    heartbeat_file: str = "data/heartbeat.json"


class MonitoringCfg(BaseModel):
    heartbeat_sec: int = 30
    snapshot_minutes: int = 15
    loop_interval_sec: float = 30.0
    health_max_age_sec: int = 180
    log_level: str = "INFO"


class Settings(BaseModel):
    mode: str = "paper"
    capital: CapitalCfg = CapitalCfg()
    exchange: ExchangeCfg = ExchangeCfg()
    universe: UniverseCfg = UniverseCfg()
    timeframes: list[str] = ["15m", "1h", "4h", "1d"]
    instruments: InstrumentsCfg = InstrumentsCfg()
    execution: ExecutionCfg = ExecutionCfg()
    risk: RiskCfg = RiskCfg()
    strategies: StrategiesCfg = StrategiesCfg()
    ai: AiCfg = AiCfg()
    copytrading: CopyTradingCfg = CopyTradingCfg()
    verdict: VerdictCfg = VerdictCfg()
    storage: StorageCfg = StorageCfg()
    monitoring: MonitoringCfg = MonitoringCfg()

    @field_validator("mode")
    @classmethod
    def _paper_only(cls, v: str) -> str:
        if v != "paper":
            raise ValueError("поддерживается только mode: paper (реальная торговля в проекте отключена)")
        return v

    @model_validator(mode="after")
    def _checks(self):
        if self.strategies.timeframe not in self.timeframes:
            raise ValueError("timeframe стратегий должен быть в списке timeframes")
        if self.risk.max_drawdown_pct <= self.risk.weekly_loss_limit_pct:
            raise ValueError("max_drawdown_pct должен быть больше weekly_loss_limit_pct")
        return self


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def load_config(path: str | Path = "config.yaml", env_file: str | Path = ".env") -> Settings:
    load_dotenv(env_file)
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    return Settings(**(data or {}))
