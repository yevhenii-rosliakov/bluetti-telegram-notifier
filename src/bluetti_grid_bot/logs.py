"""Logging setup: text or JSON lines, with known secrets masked in every record."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_SECRETS: set[str] = set()
_MASK = "**********"


def register_secret(value: str | None) -> None:
    """Mask this value wherever it would appear in a log line."""
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, _MASK)
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _SECRETS:
            record.msg = redact(record.getMessage())
            record.args = None
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_RedactingFilter())
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # aiogram logs every update at INFO; keep the service log about power, not chat traffic.
    logging.getLogger("aiogram.event").setLevel(max(logging.WARNING, root.level))
