"""SQLite (WAL). Схема совместима с PostgreSQL: TEXT-идентификаторы, REAL/INTEGER, параметры через ?."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 2

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE TABLE IF NOT EXISTS candles (
        exchange TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, ts INTEGER NOT NULL,
        o REAL, h REAL, l REAL, c REAL, v REAL,
        PRIMARY KEY (exchange, symbol, timeframe, ts))""",
    """CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE, ts REAL, symbol TEXT, instrument TEXT, side TEXT,
        type TEXT, qty REAL, status TEXT, strategy_id TEXT, bet_id TEXT, reason TEXT, req_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS fills (
        id TEXT PRIMARY KEY, order_id TEXT, ts REAL, symbol TEXT, side TEXT, price REAL, qty REAL, fee REAL,
        slippage_bps REAL, ref_price REAL, liquidity TEXT)""",
    """CREATE TABLE IF NOT EXISTS positions (
        id TEXT PRIMARY KEY, bet_id TEXT, strategy_id TEXT, symbol TEXT, exec_symbol TEXT, cluster TEXT, instrument TEXT,
        side INTEGER, qty REAL, entry_px REAL, entry_ts REAL, stop_px REAL, take_px REAL, margin REAL, leverage REAL,
        fees_paid REAL, funding_paid REAL, mae_px REAL, mfe_px REAL, horizon_ts REAL, status TEXT,
        last_funding_ts REAL, close_ts REAL, close_px REAL, exit_reason TEXT, realized_pnl REAL)""",
    """CREATE TABLE IF NOT EXISTS portfolio_state (
        id INTEGER PRIMARY KEY, cash REAL, initial_capital REAL, peak_equity REAL, halted INTEGER, halt_reason TEXT,
        paused_until REAL, pause_reason TEXT, day_start_ts REAL, day_start_equity REAL,
        week_start_ts REAL, week_start_equity REAL, updated_ts REAL)""",
    """CREATE TABLE IF NOT EXISTS bets (
        id TEXT PRIMARY KEY, ts REAL, symbol TEXT, instrument TEXT, side TEXT, strategy_id TEXT, strategy_version TEXT,
        benchmark INTEGER, ai_assisted INTEGER DEFAULT 0, signals_json TEXT, source_ids_json TEXT, regime TEXT,
        entry_px REAL, stop_px REAL, take_px REAL, horizon_min INTEGER, qty REAL, notional REAL, fees REAL,
        slippage_bps REAL, rationale TEXT, status TEXT, exit_px REAL, exit_ts REAL, exit_reason TEXT,
        pnl REAL, pnl_pct REAL, mae_pct REAL, mfe_pct REAL, funding REAL)""",
    """CREATE TABLE IF NOT EXISTS rejected_signals (
        id TEXT PRIMARY KEY, ts REAL, strategy_id TEXT, symbol TEXT, side TEXT, reasons_json TEXT,
        signal_json TEXT, context_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS snapshots (
        ts REAL PRIMARY KEY, equity REAL, cash REAL, exposure REAL, drawdown REAL, strategy_pnl_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS risk_events (id TEXT PRIMARY KEY, ts REAL, kind TEXT, detail_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS source_evals (
        id TEXT PRIMARY KEY, source_id TEXT, ts REAL, symbol TEXT, direction INTEGER,
        horizon_min INTEGER, forward_ret_pct REAL, btc_ret_pct REAL, adj_ret_pct REAL, hit INTEGER)""",
    """CREATE INDEX IF NOT EXISTS ix_bets_ts ON bets (ts)""",
    """CREATE INDEX IF NOT EXISTS ix_pos_status ON positions (status)""",
    """CREATE INDEX IF NOT EXISTS ix_rej_ts ON rejected_signals (ts)""",
]


class Database:
    def __init__(self, path: str | Path = "data/bot.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            for stmt in _SCHEMA:
                self._conn.execute(stmt)
            self._conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    @contextmanager
    def tx(self):
        with self._lock:
            depth = self._depth
            name = f"sp{depth}"
            self._conn.execute("BEGIN" if depth == 0 else f"SAVEPOINT {name}")
            self._depth = depth + 1
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK" if depth == 0 else f"ROLLBACK TO {name}")
                if depth:
                    self._conn.execute(f"RELEASE {name}")
                raise
            else:
                self._conn.execute("COMMIT" if depth == 0 else f"RELEASE {name}")
            finally:
                self._depth = depth

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).rowcount

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def upsert(self, table: str, row: dict, key: str = "id") -> None:
        cols = list(row)
        upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c != key)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) ON CONFLICT({key}) DO UPDATE SET {upd}"
        self.execute(sql, [row[c] for c in cols])

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def log_risk_event(self, kind: str, detail: dict, ts: Optional[float] = None) -> None:
        self.execute("INSERT INTO risk_events (id, ts, kind, detail_json) VALUES (?,?,?,?)",
                     (uuid.uuid4().hex, ts or time.time(), kind, json.dumps(detail, ensure_ascii=False, default=str)))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
