"""Structured logging with JSON file, rotating text, and Rich console."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "migradora"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        for key in (
            "job_id",
            "filename",
            "phase",
            "bytes",
            "speed_bps",
            "eta_sec",
            "error",
            "extra",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(
    service: str = "migradora",
    log_dir: str = "/data/logs",
    level: str = "INFO",
    enable_rich: bool = True,
) -> logging.Logger:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("migradora")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    json_handler = RotatingFileHandler(
        log_path / "migradora.jsonl",
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
    )
    json_handler.setFormatter(JsonFormatter())
    root.addHandler(json_handler)

    text_handler = RotatingFileHandler(
        log_path / "migradora.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
    )
    text_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(text_handler)

    if enable_rich:
        console = Console(stderr=True)
        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            markup=False,
        )
        rich_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(rich_handler)
    else:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        root.addHandler(stream)

    # Attach service name to all records via filter
    class ServiceFilter(logging.Filter):
        def __init__(self, svc: str) -> None:
            super().__init__()
            self.svc = svc

        def filter(self, record: logging.LogRecord) -> bool:
            record.service = self.svc
            return True

    root.addFilter(ServiceFilter(service))
    return root


def log_event(
    logger: logging.Logger,
    message: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    extra = {k: v for k, v in fields.items() if k in {
        "job_id", "filename", "phase", "bytes", "speed_bps", "eta_sec", "error", "extra",
    }}
    logger.log(level, message, extra=extra)
