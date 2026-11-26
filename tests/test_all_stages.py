import pytest

from bot.analysis.ai import AIAnalystSuite, LLMProvider, local_lexicon_sentiment
from bot.backtest.walkforward import run_backtest, walk_forward_validate
from bot.config import Settings
from bot.copytrading.hyperliquid import HyperliquidCopySource, _iter_leaderboard_rows, wilson_lower_bound
from bot.db import Database
from bot.exchange.fake import FakeExchange
from bot.execution.base import OrderRequest
from bot.execution.paper import PaperExecutor
from bot.journal import Journal
from bot.market.indicators import bollinger, detect_regime, macd, rsi
from bot.ml.meta_labeler import MetaLabeler, extract_features
from bot.models import MarketContext, OrderBook, Signal
from bot.portfolio import Portfolio
from bot.report import build_report, evaluate_verdict, trade_stats
from bot.risk import RiskEngine
from bot.signals.aggregator import ConfidenceCalibrator, SignalAggregator
from bot.simulate import sim_settings, simulate
from bot.sources.registry import SourceRegistry
from bot.storage_s3 import S3SyncManager
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
    payload = b'{"leaderboardRows":[{"ethAddress":"0x1","accountValue":"100000"},{"ethAddress":"0x2","accountValue":"250000"}]}'
    chunks = [payload[i:i + 17] for i in range(0, len(payload), 17)]
    rows = list(_iter_leaderboard_rows(chunks))
    assert [r["ethAddress"] for r in rows] == ["0x1", "0x2"]


def test_stage6_verdict_thresholds():
    cfg = Settings()
    good_rows = [{"pnl": 25.0 if i % 3 else -10.0, "fees": 1.0, "mae_pct": -0.5, "mfe_pct": 1.2} for i in range(120)]
    st = trade_stats(good_rows)
    cat, _ = evaluate_verdict(st, days=50.0, dd_pct=6.0, cfg=cfg)
    assert cat == "статистически подтверждено"
    cat_small, _ = evaluate_verdict(st, days=10.0, dd_pct=6.0, cfg=cfg)
    assert cat_small == "не доказано"


def test_compact_s3_snapshot_strips_candles(tmp_path):
    import sqlite3
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.set_meta("keep_me", "yes")
    db.executemany(
        "INSERT INTO candles (exchange, symbol, timeframe, ts, o, h, l, c, v) VALUES (?,?,?,?,?,?,?,?,?)",
        [("bybit", "BTC/USDT", "1h", i * 3600000, 100.0, 101.0, 99.0, 100.5, 10.0) for i in range(500)],
    )
    raw = S3SyncManager._compact_snapshot_bytes(db_path, db=db)
    out = tmp_path / "restored.db"
    out.write_bytes(raw)
    con = sqlite3.connect(str(out))
    try:
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 0
        assert con.execute("SELECT value FROM meta WHERE key='keep_me'").fetchone()[0] == "yes"
    finally:
        con.close()
    # В живой БД свечи не удалены
    assert db.one("SELECT COUNT(*) AS n FROM candles")["n"] == 500
    db.close()


@pytest.mark.asyncio
async def test_stages_abc_risk_maker_and_meta_labeler(tmp_path):
    cfg = Settings()
    db = Database(tmp_path / "abc.db")
    pf = Portfolio(db, 10000.0, {"ema_trend": 0.5, "funding_carry": 0.5})
    risk = RiskEngine(cfg, pf, Journal(db))

    # 1. Fee-edge filter блокирует сделку с малым ATR относительно издержек
    sig = Signal(strategy_id="ema_trend", symbol="BTC/USDT", direction="long", confidence=0.6, stop_atr_mult=2.0, take_atr_mult=2.0)
    tiny_atr_ctx = MarketContext(symbol="BTC/USDT", price=100000.0, atr=30.0, spread_bps=5.0, data_age_sec=1.0, depth_usd=500000.0, instrument="spot")
    dec_low = risk.evaluate(sig, tiny_atr_ctx, {"BTC/USDT": 100000.0})
    assert not dec_low.approved and any("low_edge_to_cost" in r for r in dec_low.reasons)

    # 2. OBI filter блокирует лонг в сильную стену продавцов
    wall_ctx = MarketContext(symbol="BTC/USDT", price=100000.0, atr=1500.0, spread_bps=2.0, data_age_sec=1.0, depth_usd=500000.0, instrument="spot", obi=-0.8)
    dec_obi = risk.evaluate(sig, wall_ctx, {"BTC/USDT": 100000.0})
    assert not dec_obi.approved and any("adverse_obi" in r for r in dec_obi.reasons)

    # 3. Sign-aware funding: высокий положительный фандинг разрешён для SHORT (мы получаем фандинг), но запрещён для LONG
    sh_sig = Signal(strategy_id="funding_carry", symbol="BTC/USDT", direction="short", confidence=0.65, stop_atr_mult=2.0, take_atr_mult=3.0)
    fund_ctx = MarketContext(symbol="BTC/USDT", price=100000.0, atr=1500.0, spread_bps=2.0, data_age_sec=1.0, depth_usd=500000.0, instrument="perp", funding_rate=0.002)
    assert risk.evaluate(sh_sig, fund_ctx, {"BTC/USDT": 100000.0}).approved
    lg_sig = Signal(strategy_id="funding_carry", symbol="BTC/USDT", direction="long", confidence=0.65, stop_atr_mult=2.0, take_atr_mult=3.0)
    assert not risk.evaluate(lg_sig, fund_ctx, {"BTC/USDT": 100000.0}).approved

    # 4. Maker-First исполнение использует maker-комиссию
    ob = OrderBook(ts=1.0, bids=[(99.9, 100.0)], asks=[(100.1, 100.0)])
    async def book_fn(_s): return ob
    async def no_sleep(_t): return None
    ex = PaperExecutor(db, book_fn, cfg.execution, sleep=no_sleep)
    res = await ex.submit(OrderRequest(client_id="m1", symbol="BTC/USDT", instrument="perp", side="buy", qty=1.0, ref_price=100.0, strategy_id="ema_trend", bet_id="b1", maker_preferred=True))
    assert res.status == "filled" and res.fills[0].liquidity == "maker" and abs(res.avg_px - 99.9) < 1e-6

    # 5. Meta-Labeler (Этап C) обучается и разделяет сильные/слабые контексты
    ml = MetaLabeler(min_trades=20)
    X, y = [], []
    for i in range(40):
        good = i % 2 == 0
        f = extract_features(
            confidence=0.75 if good else 0.45, direction="long", regime="trend" if good else "flat",
            mtf_trend=1 if good else -1, obi=0.4 if good else -0.4, funding_rate=0.0,
            spread_bps=2.0, atr=1000.0, price=100000.0,
        )
        X.append(f)
        y.append(1 if good else 0)
    assert ml.fit(X, y) and ml.active and ml.train_acc >= 90.0
    p_good, sc_good = ml.predict_scale(X[0])
    p_bad, sc_bad = ml.predict_scale(X[1])
    assert p_good > 0.7 and sc_good > 0.8
    assert p_bad < 0.3 and sc_bad == 0.0
    db.close()


def test_persistence_across_compact_s3_restart(tmp_path):
    db_path = tmp_path / "live.db"
    db1 = Database(db_path)
    # 1) Галочки технологий
    db1.set_meta("features_json", '{"regime_gating": false, "meta_labeling": true}')
    # 2) Калибратор уверенности
    cal1 = ConfidenceCalibrator()
    for _ in range(8):
        cal1.record(0.72, True)
    cal1.save_to_db(db1)
    # 3) Thompson Sampling источников
    reg1 = SourceRegistry(db1)
    reg1.register_defaults()
    for _ in range(5):
        reg1.record_outcome("coindesk_rss", "BTC/USDT", 1, 60, 2.0, 0.5)
    # 4) Телеметрия LLM
    llm1 = LLMProvider()
    llm1.attach_db(db1)
    llm1.last_provider, llm1.last_model, llm1.llm_ok_count = "Groq", "llama-3.3-70b", 7
    llm1._save_stats()

    # Имитируем компактный S3-бэкап + восстановление в новую чистую БД после рестарта Render
    raw = S3SyncManager._compact_snapshot_bytes(db_path, db=db1)
    db1.close()

    restored_path = tmp_path / "restored.db"
    restored_path.write_bytes(raw)
    db2 = Database(restored_path)

    assert '"regime_gating": false' in (db2.get_meta("features_json") or "")
    cal2 = ConfidenceCalibrator()
    cal2.load_from_db(db2)
    assert cal2.calibrate(0.72) > 0.65

    reg2 = SourceRegistry(db2)
    reg2.register_defaults()
    assert reg2.sources["coindesk_rss"].alpha == 7.0  # 2.0 prior + 5 hits

    llm2 = LLMProvider()
    llm2.attach_db(db2)
    assert llm2.last_provider == "Groq" and llm2.last_model == "llama-3.3-70b" and llm2.llm_ok_count == 7
    db2.close()
