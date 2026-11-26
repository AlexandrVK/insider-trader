"""Запуск бота: S3-восстановление, веб-панель ($PORT + /health), автовыбор биржи, движок, heartbeat, бэкапы."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Callable, Optional

from . import __version__
from .config import Settings
from .db import Database
from .engine import TradingEngine
from .exchange.adapter import CcxtAdapter, ExchangeAdapter
from .exchange.hub import ExchangeHub, NoExchangeAvailable, autoselect, probe_summary
from .logging_setup import setup_logging
from .market.feeds import PriceStream
from .monitoring import heartbeat_loop
from .report import build_report
from .storage_s3 import S3SyncManager, is_render, s3_backup_loop
from .supervisor import Supervisor
from .web import run_web_server

log = logging.getLogger("bot.app")


async def build_hub(cfg: Settings, db: Database, stop: asyncio.Event,
                    factory: Optional[Callable[[str], ExchangeAdapter]] = None) -> Optional[ExchangeHub]:
    factory = factory or (lambda i: CcxtAdapter(i, cfg.exchange.timeout_s))
    delay = 5.0
    while not stop.is_set():
        try:
            hub, results = await autoselect(factory, cfg.exchange.candidates, cfg.exchange.probe_symbol, cfg.exchange.timeout_s * 1.5,
                                            cfg.exchange.min_backups, on_event=lambda k, d: db.log_risk_event(k, d),
                                            stop_early=is_render())
            if cfg.exchange.primary != "auto" and not hub.set_primary(cfg.exchange.primary):
                log.warning("заданная primary-биржа не прошла пробу, используется лучшая")
            db.set_meta("exchange_probe", probe_summary(results))
            db.set_meta("exchange_primary", hub.exchange_id)
            return hub
        except NoExchangeAvailable as e:
            log.error("ни одна биржа недоступна, повтор", extra={"error": str(e)[:300], "in_s": delay})
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 300)
    return None


def _install_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def run_bot(cfg: Settings, *, hub_factory=None, clock=time.time, stop: Optional[asyncio.Event] = None,
                  install_signals: bool = True, sleep=asyncio.sleep) -> Optional[TradingEngine]:
    setup_logging(cfg.storage.log_dir, cfg.monitoring.log_level)
    stop = stop or asyncio.Event()
    if install_signals:
        _install_signals(asyncio.get_running_loop(), stop)

    s3_sync = S3SyncManager(cfg.storage.db_path, remote_name="bot.db")
    s3_sync.restore_on_startup()
    db = Database(cfg.storage.db_path)

    state_holder: dict = {"engine": None, "hub": None, "phase": "selecting_exchange"}
    sup = Supervisor(stop)

    def payload() -> dict:
        eng: Optional[TradingEngine] = state_holder["engine"]
        hub: Optional[ExchangeHub] = state_holder["hub"]
        if eng is None:
            return {
                "version": __version__,
                "phase": state_holder["phase"],
                "initial_capital": cfg.capital.initial_usdt,
                "equity": cfg.capital.initial_usdt,
                "ai": {
                    "enabled": cfg.ai.enabled,
                    "last_provider": "",
                    "last_model": "",
                    "llm_ok": 0,
                    "llm_fail": 0,
                },
                "s3": s3_sync.status(),
            }
        eq = eng.equity()
        now = time.time()
        open_pos = []
        for p in eng.pf.positions.values():
            mk = eng.prices.get(p.exec_symbol, eng.prices.get(p.symbol, p.entry_px))
            open_pos.append({
                "symbol": p.symbol,
                "instrument": p.instrument,
                "side": "LONG" if p.side > 0 else "SHORT",
                "strategy_id": p.strategy_id,
                "entry_px": round(p.entry_px, 6),
                "mark_px": round(mk, 6),
                "stop_px": round(p.stop_px, 6),
                "take_px": round(p.take_px, 6) if p.take_px else 0,
                "upnl": round(p.unrealized(mk), 2),
                "entry_ts": p.entry_ts,
            })
        strat_pnl = eng.pf.strategy_pnl(eng.prices)
        strategies_view = [
            {"id": s.id, "weight": eng.pf.weight(s.id), "pnl": strat_pnl.get(s.id, 0.0)}
            for s in eng.strategies
        ]
        recent_trades = db.query(
            "SELECT exit_ts, symbol, side, strategy_id, entry_px, exit_px, pnl, fees, exit_reason "
            "FROM bets WHERE status='closed' ORDER BY exit_ts DESC LIMIT 20"
        )
        recent_rej = [
            {"ts": r["ts"], "symbol": r["symbol"], "side": r["side"], "strategy_id": r["strategy_id"],
             "reasons": ", ".join(json.loads(r["reasons_json"] or "[]"))}
            for r in db.query("SELECT ts, symbol, side, strategy_id, reasons_json FROM rejected_signals ORDER BY ts DESC LIMIT 15")
        ]
        recent_ev = [
            {"ts": e["ts"], "kind": e["kind"], "detail": (e["detail_json"] or "")[:160]}
            for e in db.query("SELECT ts, kind, detail_json FROM risk_events ORDER BY ts DESC LIMIT 15")
        ]
        agg = db.one(
            "SELECT COALESCE(SUM(pnl), 0) AS net_pnl, COALESCE(SUM(fees), 0) AS fees, "
            "COALESCE(SUM(funding), 0) AS funding, COUNT(*) AS n FROM bets WHERE status='closed'"
        ) or {"net_pnl": 0.0, "fees": 0.0, "funding": 0.0, "n": 0}
        realized_pnl = float(agg["net_pnl"] or 0.0)
        fees_total = float(agg["fees"] or 0.0) + sum(p.fees_paid for p in eng.pf.positions.values())
        funding_total = float(agg["funding"] or 0.0) + sum(p.funding_paid for p in eng.pf.positions.values())
        gross_pnl = realized_pnl + float(agg["fees"] or 0.0) + float(agg["funding"] or 0.0)
        return {
            "version": __version__,
            "phase": "running",
            "initial_capital": eng.pf.initial_capital,
            "equity": round(eq, 2),
            "realized_pnl": round(realized_pnl, 2),
            "gross_pnl_before_fees": round(gross_pnl, 2),
            "fees_total": round(fees_total, 2),
            "funding_total": round(funding_total, 2),
            "n_trades": int(agg["n"] or 0),
            "drawdown_pct": round(eng.pf.drawdown(eq) * 100.0, 2),
            "positions": len(open_pos),
            "open_positions": open_pos,
            "strategies": strategies_view,
            "recent_trades": recent_trades,
            "recent_rejected": recent_rej,
            "recent_events": recent_ev,
            "exchange": hub.exchange_id if hub else "",
            "halted": eng.pf.halted,
            "halt_reason": eng.pf.halt_reason,
            "paused": eng.pf.paused_until > now,
            "stats": eng.stats,
            "ai": {
                "enabled": bool(eng.llm),
                "last_provider": eng.llm.last_provider if eng.llm else "",
                "last_model": eng.llm.last_model if eng.llm else "",
                "llm_ok": eng.llm.llm_ok_count if eng.llm else 0,
                "llm_fail": eng.llm.llm_fail_count if eng.llm else 0,
            },
            "s3": s3_sync.status(),
            "tasks": sup.status,
            "last_cycle_ts": eng.last_cycle_ts,
        }

    def render_report() -> str:
        res = build_report(db, cfg)
        return Path(res["paths"]["md"]).read_text(encoding="utf-8")

    def handle_cmd(action: str) -> str:
        eng: Optional[TradingEngine] = state_holder["engine"]
        if action == "backup":
            ok = s3_sync.backup_now(db, force=True)
            return "бэкап выполнен" if ok else "S3 отключён или ошибка бэкапа"
        if eng is None:
            return "движок ещё запускается"
        if action == "pause":
            eng.pf.paused_until = time.time() + 365 * 86400
            eng.pf.pause_reason = "ручная пауза через веб-панель"
            eng.pf.save()
            return "поставлен на паузу"
        if action == "resume":
            eng.pf.paused_until = 0.0
            eng.pf.pause_reason = ""
            eng.pf.save()
            return "работа продолжена"
        if action == "reset_halt":
            eng.risk.reset_halt(eng.prices)
            eng.pf.save()
            return "риск-стоп и пауза сброшены"
        raise ValueError(f"неизвестная команда: {action}")

    web_enabled = is_render() or os.getenv("WEB_ENABLED", "true").lower() in ("1", "true", "yes")
    tasks: list[asyncio.Task] = []
    if web_enabled:
        tasks.append(asyncio.create_task(sup.supervise("web", lambda: run_web_server(stop, payload, render_report, handle_cmd))))

    hub = await build_hub(cfg, db, stop, hub_factory)
    if hub is None:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()
        return None

    engine = TradingEngine(cfg, db, hub, clock, sleep=sleep)
    state_holder.update({"engine": engine, "hub": hub, "phase": "running"})

    async def ws_manager():
        if not cfg.exchange.use_websocket:
            return
        while not stop.is_set() and not engine.universe:
            await asyncio.sleep(1)
        if stop.is_set():
            return
        stream = PriceStream(hub.exchange_id, list(engine.universe.symbols))
        engine.md.stream = stream
        await stream.run(stop)

    tasks.extend([
        asyncio.create_task(sup.supervise("engine", lambda: engine.run(stop))),
        asyncio.create_task(sup.supervise("heartbeat", lambda: heartbeat_loop(stop, cfg.storage.heartbeat_file,
                                                                              cfg.monitoring.heartbeat_sec, payload))),
        asyncio.create_task(sup.supervise("s3_backup", lambda: s3_backup_loop(stop, s3_sync, db, interval_sec=3600.0))),
        asyncio.create_task(sup.supervise("websocket", ws_manager)),
    ])

    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    engine.snapshot(force=True)
    engine.pf.save()
    s3_sync.backup_now(db)
    await hub.close()
    db.close()
    return engine
