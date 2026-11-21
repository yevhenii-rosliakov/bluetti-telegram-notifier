"""Telegram: sending notifications and answering commands (aiogram 3)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message

from .health import Metrics
from .messages import Translator
from .monitor import Monitor

log = logging.getLogger(__name__)

SEND_ATTEMPTS = 4
HISTORY_DEFAULT = 10
HISTORY_MAX = 50
STATS_DEFAULT_DAYS = 30
STATS_MAX_DAYS = 365


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        chat_ids: Iterable[int],
        metrics: Metrics,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._chat_ids = list(chat_ids)
        self._metrics = metrics
        self._sleep = sleep

    async def broadcast(self, text: str) -> None:
        for chat_id in self._chat_ids:
            await self._send(chat_id, text)

    async def _send(self, chat_id: int, text: str) -> None:
        for attempt in range(SEND_ATTEMPTS):
            try:
                await self._bot.send_message(chat_id, text)
            except TelegramRetryAfter as exc:
                await self._sleep(float(exc.retry_after))
            except (TelegramNetworkError, TelegramServerError) as exc:
                log.warning("Telegram send failed (%s), attempt %d", exc, attempt + 1)
                await self._sleep(2.0 * 2**attempt)
            except TelegramAPIError as exc:
                # Bad chat ID, bot blocked or removed from the group: retrying won't help.
                log.error("Telegram rejected a message for a configured chat: %s", exc)
                self._metrics.notifications.labels("error").inc()
                return
            else:
                self._metrics.notifications.labels("ok").inc()
                return
        log.error("Giving up sending a Telegram message after %d attempts", SEND_ATTEMPTS)
        self._metrics.notifications.labels("error").inc()


def _int_arg(command: CommandObject | None, default: int, maximum: int) -> int:
    arg = (command.args or "").strip() if command else ""
    try:
        n = int(arg.split()[0]) if arg else default
    except ValueError:
        n = default
    return max(1, min(maximum, n))


def parse_history_limit(command: CommandObject | None) -> int:
    return _int_arg(command, HISTORY_DEFAULT, HISTORY_MAX)


def parse_stats_days(command: CommandObject | None) -> int:
    return _int_arg(command, STATS_DEFAULT_DAYS, STATS_MAX_DAYS)


def build_router(monitor: Monitor, allowed_chat_ids: frozenset[int]) -> Router:
    router = Router(name="commands")
    # Everyone else is ignored silently.
    router.message.filter(F.chat.id.in_(allowed_chat_ids))
    t = monitor.formatter.t

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if message.bot is not None:
            with contextlib.suppress(TelegramAPIError):
                await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await monitor.refresh()
        await message.answer(monitor.status_text())

    @router.message(Command("history"))
    async def history(message: Message, command: CommandObject) -> None:
        await message.answer(monitor.history_text(parse_history_limit(command)))

    @router.message(Command("stats"))
    async def stats(message: Message, command: CommandObject) -> None:
        await message.answer(monitor.stats_text(parse_stats_days(command)))

    @router.message(Command("schedule"))
    async def schedule(message: Message) -> None:
        await message.answer(monitor.schedule_text())

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(t("help"))

    return router


def build_dispatcher(monitor: Monitor, allowed_chat_ids: frozenset[int]) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(monitor, allowed_chat_ids))
    return dp


async def set_commands(bot: Bot, t: Translator) -> None:
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="status", description=t("cmd_status")),
                BotCommand(command="stats", description=t("cmd_stats")),
                BotCommand(command="schedule", description=t("cmd_schedule")),
                BotCommand(command="history", description=t("cmd_history")),
                BotCommand(command="help", description=t("cmd_help")),
            ]
        )
    except TelegramAPIError as exc:
        log.warning("Could not set the bot command list: %s", exc)
