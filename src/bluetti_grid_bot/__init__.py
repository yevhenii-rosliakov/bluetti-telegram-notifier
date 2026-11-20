"""Telegram bot that reports grid power outages using a BLUETTI power station."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bluetti-grid-bot")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"
