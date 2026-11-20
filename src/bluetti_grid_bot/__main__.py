"""Command line: ``bluetti-grid-bot [run|login|check]``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import __version__
from .config import ConfigError, load_settings
from .logs import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bluetti-grid-bot",
        description="Telegram bot that reports grid power outages using a BLUETTI station.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the service (default)")
    sub.add_parser("login", help="sign in to BLUETTI interactively and store the tokens")
    sub.add_parser("check", help="poll once and show what the detector decides")
    args = parser.parse_args(argv)
    command = args.command or "run"

    # Everything written to STATE_DIR holds tokens: keep it private.
    os.umask(0o077)
    try:
        settings = load_settings()
        setup_logging(settings.log_level, settings.log_format)
        from . import app  # noqa: PLC0415 - keep `--help` fast

        if command == "login":
            return app.login(settings)
        if command == "check":
            return asyncio.run(app.check(settings))
        return asyncio.run(app.run_service(settings))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
