import pytest

from bot.analysis.ai import AIAnalystSuite, local_lexicon_sentiment
from bot.backtest.walkforward import run_backtest, walk_forward_validate
from bot.config import Settings
from bot.copytrading.hyperliquid import HyperliquidCopySource, wilson_lower_bound
from bot.db import Database
from bot.exchange.fake import FakeExchange
from bot.market.indicators import bollinger, detect_regime, macd, rsi
from bot.models import Signal
from bot.report import build_report, evaluate_verdict, trade_stats
from bot.signals.aggregator import ConfidenceCalibrator, SignalAggregator
from bot.simulate import sim_settings, simulate
from bot.sources.registry import SourceRegistry
from bot.strategies.baseline import EmaTrendStrategy
from bot.strategies.meta_selector import CUSUMDetector, MetaSelector, deflated_sharpe_ratio


@pytest.mark.asyncio
async def test_stage1_simulation_and_recovery(tmp_path):
    cfg = sim_settings(Settings())
    db_path = str(tmp_path / "sim.db")
    cfg.storage.db_path = db_path
    db = Database(db_path)
    eng, clk = await simulate(cfg, days=2.0, step_sec=600, db=db)
    assert eng.stats["cycles"] > 0
    assert eng.stats["errors"] == 0
    rep = build_report(db, cfg, str(tmp_path / "reports"), now=clk.now)
    assert rep["verdict_category"] == "не доказано"


@pytest.mark.asyncio
async def test_stage2_indicators_walkforward_and_meta():
    fake = FakeExchange(seed=5, days=35)
    candles = await fake.fetch_ohlcv("BTC/USDT", "1h", limit=600)
    assert 0 <= rsi([c.c for c in candles])[-1] <= 100
    assert detect_regime(candles) in ("trend", "flat", "high_vol", "panic")
    bt = run_backtest(EmaTrendStrategy(10, 30, "1h"), "BTC/USDT", candles)
    assert bt.trades >= 0
    wf = walk_forward_validate(lambda p: EmaTrendStrategy(p["f"], p["s"], "1h"),
                               [{"f": 10, "s": 30}, {"f": 20, "s": 50}], "BTC/USDT", candles, 220, 100)
    assert wf["folds"] >= 2

    db = Database(":memory:")
    ms = MetaSelector(db, ["ema_trend", "rsi_mean_rev", "flat"])
    w = ms.update_weights(regime="trend", force=True)
    assert abs(sum(w.values()) - 1.0) < 1e-3
    det = CUSUMDetector(threshold=3.0, drift=0.2)
    for _ in range(5):
        det.update(-1.0, 100.0)
    assert det.degraded
    assert deflated_sharpe_ratio([1.0, 1.2, 0.9, 1.1, 1.3], n_trials=1) > 0.5


def test_stage3_ai_fallback_and_aggregator():
    d, conf, _ = local_lexicon_sentiment(["Bitcoin ETF approved with massive inflows", "Institutional adoption rally"])
    assert d == "long" and conf > 0.5

    suite = AIAnalystSuite(llm=None)
    s_news = suite.analyze_news("BTC/USDT", ["Exchange hack and exploit reported", "SEC lawsuit ban"], ["coindesk_rss"])
    assert s_news.direction == "short"
    s_critic = suite.risk_critic("BTC/USDT", "long", ["Exchange hack and exploit reported", "SEC lawsuit ban"])
    assert s_critic.direction == "flat"

    cal = ConfidenceCalibrator()
    for _ in range(10):
        cal.record(0.7, True)
    assert cal.calibrate(0.7) > 0.6

    agg = SignalAggregator(cal)
    base = Signal(strategy_id="ema_trend", symbol="BTC/USDT", direction="long", confidence=0.6)
    assert agg.aggregate(base, [s_critic]) is None


def test_stage4_source_registry():
    db = Database(":memory:")
    reg = SourceRegistry(db)
    reg.register_defaults()
    assert reg.can_request("coindesk_rss")
    assert not reg.is_duplicate("Bitcoin surges past 70000 after ETF inflows")
    assert reg.is_duplicate("Bitcoin surges past 70000 after ETF inflows!")
    reg.record_outcome("coindesk_rss", "ETH/USDT", 1, 60, 2.5, 1.0)
    w = reg.sample_weights(seed=1)
    assert abs(sum(w.values()) - 1.0) < 0.02
    assert len(reg.ranking()) >= 5


def test_stage5_copytrading_scoring():
    assert wilson_lower_bound(16, 20) > 0.55
    src = HyperliquidCopySource()
    assert src.score_trader_history(18, 20, market_baseline=0.5) > 0.6
    assert src.score_trader_history(18, 20, market_baseline=0.95) < 0.35


def test_stage6_verdict_thresholds():
    cfg = Settings()
    good_rows = [{"pnl": 25.0 if i % 3 else -10.0, "fees": 1.0, "mae_pct": -0.5, "mfe_pct": 1.2} for i in range(120)]
    st = trade_stats(good_rows)
    cat, _ = evaluate_verdict(st, days=50.0, dd_pct=6.0, cfg=cfg)
    assert cat == "статистически подтверждено"
    cat_small, _ = evaluate_verdict(st, days=10.0, dd_pct=6.0, cfg=cfg)
    assert cat_small == "не доказано"
