"""CLI: python -m bot [run|report|doctor|selftest|health|reset-halt|version]"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile

from . import __version__


def _cfg(path):
    from .config import load_config
    try:
        return load_config(path)
    except Exception as e:
        print(f"[ОШИБКА] конфигурация {path}: {e}")
        sys.exit(2)


def cmd_run(a) -> int:
    from .app import run_bot
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_bot(_cfg(a.config)))
    return 0


def cmd_report(a) -> int:
    from .db import Database
    from .report import build_report
    cfg = _cfg(a.config)
    db = Database(cfg.storage.db_path)
    r = build_report(db, cfg)
    print(open(r["paths"]["md"], encoding="utf-8").read())
    print("\nФайлы:", *r["paths"].values(), sep="\n  ")
    return 0


def cmd_health(a) -> int:
    from .monitoring import health
    cfg = _cfg(a.config)
    ok, msg = health(cfg.storage.heartbeat_file, cfg.monitoring.health_max_age_sec)
    print(("OK  " if ok else "FAIL ") + msg)
    return 0 if ok else 1


def cmd_reset_halt(a) -> int:
    from .db import Database
    from .journal import Journal
    from .portfolio import Portfolio
    from .risk import RiskEngine
    if not a.yes:
        print("Повторите с --yes, если уверены.")
        return 1
    cfg = _cfg(a.config)
    db = Database(cfg.storage.db_path)
    pf = Portfolio(db, cfg.capital.initial_usdt)
    RiskEngine(cfg, pf, Journal(db)).reset_halt()
    pf.save()
    print("Риск-стоп и паузы сброшены.")
    return 0


def cmd_doctor(a) -> int:
    from .db import Database
    from .exchange.adapter import CcxtAdapter
    from .exchange.hub import probe_exchange
    cfg = _cfg(a.config)
    print(f"Bot v{__version__}, mode={cfg.mode}")
    Database(cfg.storage.db_path)
    print(f"[OK] БД: {cfg.storage.db_path}")

    async def go():
        res = []
        for ex in cfg.exchange.candidates:
            try:
                ad = CcxtAdapter(ex, cfg.exchange.timeout_s)
            except ValueError as e:
                print(f"[--] {ex}: {e}")
                continue
            r = await probe_exchange(ad, cfg.exchange.probe_symbol, cfg.exchange.timeout_s * 1.5)
            await ad.close()
            res.append(r)
            print(f"[{'OK' if r.ok else 'НЕТ'}] {r.id:8} {r.latency_ms:5.0f} мс {r.reason}")
        return res

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    res = asyncio.run(go())
    ok = [r for r in res if r.ok]
    return 0 if ok else 1


def cmd_selftest(a) -> int:
    from .config import Settings
    from .db import Database
    from .engine import TradingEngine
    from .report import build_report
    from .simulate import sim_settings, simulate
    cfg = sim_settings(Settings(storage={"db_path": "x"}))
    path = os.path.join(tempfile.mkdtemp(), "selftest.db")
    cfg.storage.db_path = path
    db = Database(path)
    eng, clk = asyncio.run(simulate(cfg, days=a.days, step_sec=300, db=db))
    eng2 = TradingEngine(cfg, Database(path), eng.hub, clk)
    assert len(eng2.pf.positions) == len(eng.pf.positions)
    rep = build_report(db, cfg, tempfile.mkdtemp(), now=clk.now)
    print(f"Симуляция {a.days} дн.: циклов={eng.stats['cycles']}, открыто={eng.stats['opened']}, закрыто={eng.stats['closed']}, equity={eng.equity():.2f}")
    print("Вердикт:", rep["verdict"])
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m bot", description="Crypto bot (paper trading)")
    p.add_argument("command", nargs="?", default="run", choices=["run", "report", "doctor", "selftest", "health", "reset-halt", "version"])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--days", type=float, default=3.0)
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    if a.command == "version":
        print(__version__)
        return 0
    return {"run": cmd_run, "report": cmd_report, "doctor": cmd_doctor, "selftest": cmd_selftest,
            "health": cmd_health, "reset-halt": cmd_reset_halt}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
