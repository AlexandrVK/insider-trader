"""Надзор за фоновыми задачами: падение одной не роняет остальные; перезапуск с нарастающей паузой."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger("bot.supervisor")


class Supervisor:
    def __init__(self, stop: asyncio.Event):
        self.stop = stop
        self.status: dict[str, dict] = {}

    async def supervise(self, name: str, factory: Callable[[], Awaitable], backoff_start: float = 1.0, backoff_max: float = 60.0) -> None:
        st = self.status.setdefault(name, {"restarts": 0, "last_error": "", "running": False})
        delay = backoff_start
        while not self.stop.is_set():
            t0 = time.monotonic()
            st["running"] = True
            try:
                await factory()
                if self.stop.is_set():
                    break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st["restarts"] += 1
                st["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log.exception("задача упала, перезапуск", extra={"task": name, "in_s": delay})
            finally:
                st["running"] = False
            if time.monotonic() - t0 > 60:
                delay = backoff_start
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, backoff_max)
