"""Structured JSONL logging for Suno wrapper.

Provides a dual-output logger:
  - Console: human-readable colored output (matches existing print() style)
  - File: JSONL with timestamp, level, message, and extras

Usage:
    from suno_wrapper.log import get_logger
    log = get_logger("pokemon_covers")
    log.info("Generating cover", extra={"track": "Route 1", "genre": "jazz-lofi"})
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON (JSONL)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "name": record.name,
            "msg": record.getMessage(),
        }
        # Merge any extra fields (passed via extra={...})
        for key in ("tag", "track", "genre", "elapsed_s", "method",
                     "token_uses", "max_uses", "clip_id", "error",
                     "event", "check", "source", "mode"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        return json.dumps(entry, separators=(",", ":"), ensure_ascii=True)


class ConsoleFormatter(logging.Formatter):
    """Human-readable console output matching existing print() style."""

    LEVEL_PREFIX = {
        logging.DEBUG: "  ",
        logging.INFO: "  ",
        logging.WARNING: "  WARNING: ",
        logging.ERROR: "  ERROR: ",
        logging.CRITICAL: "  CRITICAL: ",
    }

    def format(self, record: logging.LogRecord) -> str:
        prefix = self.LEVEL_PREFIX.get(record.levelno, "  ")
        tag = getattr(record, "tag", None)
        if tag:
            return f"{prefix}[{tag}] {record.getMessage()}"
        return f"{prefix}{record.getMessage()}"


def get_logger(
    name: str,
    log_file: Path | None = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """Create a dual-output logger (console + JSONL file).

    Args:
        name: Logger name (e.g. "pokemon_covers", "music_factory").
        log_file: Path for JSONL output. Defaults to auto-generated/{name}.jsonl.
        console_level: Minimum level for console output.
        file_level: Minimum level for file output.

    Returns:
        Configured logging.Logger instance.
    """
    logger = logging.getLogger(f"suno.{name}")

    # Avoid duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    logger.setLevel(min(console_level, file_level))
    logger.propagate = False

    # Console handler — human-readable
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(console)

    # File handler — JSONL with rotation
    if log_file is None:
        log_file = Path("auto-generated") / f"{name}.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(JSONFormatter())
    logger.addHandler(file_handler)

    return logger
