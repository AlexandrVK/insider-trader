"""Структурные JSON-логи с ротацией и маскировкой секретов."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import time
from pathlib import Path

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE)", re.I)


def secret_values() -> list[str]:
    return [v for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v) >= 6]


class MaskFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets = secrets if secrets is not None else secret_values()

    def mask(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.mask(record.getMessage())
        record.args = ()
        for k, v in list(record.__dict__.items()):
            if k not in _STD and isinstance(v, str):
                record.__dict__[k] = self.mask(v)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
             "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        for k, v in record.__dict__.items():
            if k not in _STD:
                d[k] = v
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


def setup_logging(log_dir: str | Path = "logs", level: str = "INFO", console: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level.upper())
    mask = MaskFilter()
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(Path(log_dir) / "bot.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    fh.addFilter(mask)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        ch.addFilter(mask)
        root.addHandler(ch)
    for noisy in ("ccxt", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
