"""Heartbeat-файл и проверка здоровья."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Callable


def write_heartbeat(path: str | Path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({**payload, "ts": time.time()}, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, p)


def health(path: str | Path, max_age: float) -> tuple[bool, str]:
    p = Path(path)
    if not p.exists():
        return False, "heartbeat-файл не найден: бот не запущен"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"heartbeat не читается: {e}"
    age = time.time() - d.get("ts", 0)
    if age > max_age:
        return False, f"heartbeat устарел на {age:.0f} с (порог {max_age:.0f})"
    if d.get("halted"):
        return True, f"работает, НО риск-стоп: {d.get('halt_reason')}"
    return True, f"ок: возраст {age:.0f} с, equity {d.get('equity')}, позиций {d.get('positions')}, биржа {d.get('exchange')}"


async def heartbeat_loop(stop: asyncio.Event, path: str, interval: float, payload_fn: Callable[[], dict]) -> None:
    while not stop.is_set():
        try:
            write_heartbeat(path, payload_fn())
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
