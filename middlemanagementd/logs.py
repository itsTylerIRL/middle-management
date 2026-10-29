"""Structured logging: `event(log, "msg", key=value)` renders as key=value text or JSON lines."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from .config import LoggingConfig, expand


def event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        base = f"{ts} {record.levelname:<7} {record.name}: {record.getMessage()}"
        fields = getattr(record, "fields", None)
        if fields:
            base += " " + " ".join(f"{k}={v!r}" for k, v in fields.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(cfg: LoggingConfig, verbose: bool = False) -> None:
    formatter: logging.Formatter = JsonFormatter() if cfg.format == "json" else TextFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg.file:
        path = expand(cfg.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path))
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    root.setLevel("DEBUG" if verbose else cfg.level.upper())
    for noisy in ("aiohttp.access", "faster_whisper", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
