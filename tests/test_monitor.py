from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import CommandObject
from aiogram.methods import SendMessage
from aiohttp.test_utils import TestClient, TestServer

from bluetti_grid_bot.auth import AuthExpiredError
from bluetti_grid_bot.cloud import CloudError
from bluetti_grid_bot.health import HealthServer, Metrics
from bluetti_grid_bot.messages import Formatter, Translator
from bluetti_grid_bot.models import GridState, Sample, Station
from bluetti_grid_bot.monitor import AUTH_RETRY_S, Monitor
from bluetti_grid_bot.notifier import TelegramNotifier, parse_history_limit, parse_stats_days
from bluetti_grid_bot.schedule import Schedule, ScheduleClient, ScheduleService, Slot
from bluetti_grid_bot.storage import Storage

from .conftest import SN, T0, make_sample, make_settings


class FakeSource:
    def __init__(self) -> None:
        self.stations = [Station(SN, "PR200V2", "Home")]
        self.queue: list[dict[str, Sample | None] | Exception] = []
        self.start_error: Exception | None = None
        self.errors: dict[str, Exception] = {}

    async def start(self) -> list[Station]:
        if self.start_error is not None:
            raise self.start_error
        return self.stations

    async def fetch(self) -> dict[str, Sample | None]:
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def broadcast(self, text: str) -> None:
        self.sent.append(text)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def make_monitor(
    tmp_path: Path, storage: Storage, **settings: Any
) -> tuple[Monitor, FakeSource, FakeSender, Clock]:
    s = make_settings(tmp_path, **settings)
    source, sender, clock = FakeSource(), FakeSender(), Clock()
    formatter = Formatter(Translator("en"), s.zone, {})
    monitor = Monitor(s, storage, source, sender, formatter, Metrics(), now=clock)
    return monitor, source, sender, clock


async def poll(m: Monitor, src: FakeSource, clock: Clock, item: Any) -> float:
    src.queue.append(item)
    delay = await m.poll_once()
    clock.now += timedelta(minutes=1)
    return delay


async def test_full_outage_cycle_records_history_and_notifies(
    tmp_path: Path, storage: Storage
) -> None:
    m, src, sender, clock = make_monitor(tmp_path, storage)
    assert await m.start() == 0
    grid = {SN: make_sample(grid=0, ac=0)}
    battery = {SN: make_sample(grid=0, ac=80, soc=95)}
    assert await poll(m, src, clock, grid) == 60
    for item in (battery, battery, grid, grid):
        await poll(m, src, clock, item)
    assert [t.splitlines()[0] for t in sender.sent] == [
        "🤖 Bot started: grid power is present ✅",
        "⚡ Power went out at 12:01",
        "✅ Power is back at 12:03, it was out for 2 min",
    ]
    (outage,) = storage.history(10)
    assert (outage.start, outage.end) == (T0 + timedelta(minutes=1), T0 + timedelta(minutes=3))
    assert m.ready()
    assert "Grid: present" in m.status_text()
    assert "Outages: 1\nWithout power: 2 min" in m.status_text()
    assert "12:01 — 12:03" in m.history_text(5)
    state, raw, _ = storage.load_device(SN)
    assert state is not None
    assert state.power is GridState.GRID
    assert raw["GridAllTotalPower"] == "0"


async def test_restart_resumes_open_outage(tmp_path: Path, storage: Storage) -> None:
    m, src, sender, clock = make_monitor(tmp_path, storage)
    await m.start()
    battery = {SN: make_sample(grid=0, ac=80, soc=95)}
    await poll(m, src, clock, battery)
    assert sender.sent[0].startswith("🔋 Bot started: the station is currently on battery")

    m2, src2, sender2, clock2 = make_monitor(tmp_path, storage)
    clock2.now = clock.now + timedelta(minutes=30)
    await m2.start()
    await poll(m2, src2, clock2, battery)
    assert sender2.sent[0].startswith("🔋 Bot started: the station has been on battery since 12:00")
    (outage,) = storage.history(10)
    assert outage.end is None
    assert outage.approximate  # the very first start was not observed


async def test_notify_startup_can_be_disabled(tmp_path: Path, storage: Storage) -> None:
    m, src, sender, clock = make_monitor(tmp_path, storage, notify_startup=False)
    await m.start()
    await poll(m, src, clock, {SN: make_sample()})
    assert sender.sent == []


async def test_failed_polls_lead_to_offline_not_outage(tmp_path: Path, storage: Storage) -> None:
    m, src, sender, clock = make_monitor(tmp_path, storage, offline_grace_seconds=120)
    await m.start()
    await poll(m, src, clock, {SN: make_sample()})
    for _ in range(3):
        await poll(m, src, clock, CloudError("boom"))
    assert len(sender.sent) == 2
    assert sender.sent[1].startswith("📡 No connection to the station since 12:01")
    clock.now += timedelta(minutes=10)
    assert not m.ready()


async def test_auth_expired_notifies_once_and_retries_slowly(
    tmp_path: Path, storage: Storage
) -> None:
    m, src, sender, clock = make_monitor(tmp_path, storage)
    await m.start()
    assert await poll(m, src, clock, AuthExpiredError("invalid_grant")) == AUTH_RETRY_S
    assert await poll(m, src, clock, AuthExpiredError("invalid_grant")) == AUTH_RETRY_S
    assert sender.sent == [
        "🔑 The BLUETTI login has expired. Run `bluetti-grid-bot login` again "
        "and update BLUETTI_REFRESH_TOKEN. The bot keeps retrying."
    ]
    assert "login is invalid" in m.status_text()
    await poll(m, src, clock, {SN: make_sample()})
    assert sender.sent[1] == "🔑 The BLUETTI login works again."
    assert not m.auth_expired


async def test_start_failures_back_off(tmp_path: Path, storage: Storage) -> None:
    m, src, sender, _ = make_monitor(tmp_path, storage)
    src.start_error = CloudError("down")
    assert await m.start() == 30
    assert await m.start() == 60
    src.start_error = AuthExpiredError("expired")
    assert await m.start() == AUTH_RETRY_S
    assert len(sender.sent) == 1
    assert "No stations" in m.status_text()


async def test_invalid_fields_logged_once(
    tmp_path: Path, storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    bad = {SN: make_sample(soc="n/a")}
    await poll(m, src, clock, bad)
    await poll(m, src, clock, bad)
    assert sum("SOC" in r.getMessage() for r in caplog.records) == 1


async def test_run_loop_stops_on_event(tmp_path: Path, storage: Storage) -> None:
    m, src, _, _ = make_monitor(tmp_path, storage)
    good: dict[str, Sample | None] = {SN: make_sample()}
    src.queue = [good, RuntimeError("unexpected"), good, good, good]
    stop = asyncio.Event()
    task = asyncio.create_task(m.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert m.stations
    assert m.alive()


async def test_refresh_updates_readings_without_advancing_detector(
    tmp_path: Path, storage: Storage
) -> None:
    m, src, sender, clock = make_monitor(tmp_path, storage)
    await m.start()
    await poll(m, src, clock, {SN: make_sample(grid=0, ac=0, soc=100)})
    state_before = m.states[SN]

    src.queue.append({SN: make_sample(grid=0, ac=80, soc=97)})
    await m.refresh()
    assert "Battery: 97%" in m.status_text()
    assert "Load on station outlets: 80 W" in m.status_text()
    assert m.states[SN] == state_before  # no debounce progress, no events
    assert len(sender.sent) == 1
    _, raw, _ = storage.load_device(SN)
    assert raw["SOC"] == "97"

    # Rate limited: a second /status right away does not hit the cloud.
    await m.refresh()
    assert src.queue == []


async def test_refresh_failure_keeps_last_values(tmp_path: Path, storage: Storage) -> None:
    now = {"t": 0.0}
    s = make_settings(tmp_path)
    src, sender, clock = FakeSource(), FakeSender(), Clock()
    m = Monitor(
        s,
        storage,
        src,
        sender,
        Formatter(Translator("en"), s.zone, {}),
        Metrics(),
        now=clock,
        monotonic=lambda: now["t"],
    )
    await m.refresh()  # before discovery: nothing to do
    await m.start()
    await poll(m, src, clock, {SN: make_sample(soc=88)})
    src.queue.append(CloudError("down"))
    await m.refresh()
    assert "Battery: 88%" in m.status_text()
    now["t"] += 11
    src.queue.append({SN: make_sample(online=False)})
    await m.refresh()
    assert "Station online: no" in m.status_text()
    assert "Battery: 88%" in m.status_text()


# Telegram notifier


class FakeBot:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, text))


async def test_notifier_retries_and_skips_bad_chats() -> None:
    method = SendMessage(chat_id=1, text="x")
    bot = FakeBot(
        [
            TelegramRetryAfter(method=method, message="flood", retry_after=3),
            TelegramNetworkError(method=method, message="net"),
            TelegramBadRequest(method=method, message="chat not found"),
        ]
    )
    delays: list[float] = []

    async def sleep(d: float) -> None:
        delays.append(d)

    metrics = Metrics()
    notifier = TelegramNotifier(bot, [1, 2], metrics, sleep=sleep)  # type: ignore[arg-type]
    await notifier.broadcast("hello")
    assert bot.sent == [(2, "hello")]  # chat 1 was rejected after two retries
    assert delays == [3.0, 4.0]


async def test_notifier_gives_up_after_attempts() -> None:
    method = SendMessage(chat_id=1, text="x")
    bot = FakeBot([TelegramNetworkError(method=method, message="net") for _ in range(10)])

    async def sleep(_: float) -> None:
        return None

    notifier = TelegramNotifier(bot, [1], Metrics(), sleep=sleep)  # type: ignore[arg-type]
    await notifier.broadcast("hello")
    assert bot.sent == []


@pytest.mark.parametrize(
    ("args", "expected"), [(None, 10), ("5", 5), ("0", 1), ("999", 50), ("abc", 10), ("3 x", 3)]
)
def test_history_limit(args: str | None, expected: int) -> None:
    assert parse_history_limit(CommandObject(command="history", args=args)) == expected


# health endpoints


async def test_health_endpoints() -> None:
    metrics = Metrics()
    metrics.polls.labels("ok").inc()
    state = {"ready": False}
    server = HealthServer(0, alive=lambda: True, ready=lambda: state["ready"], metrics=metrics)
    async with TestClient(TestServer(server.app())) as client:
        assert (await client.get("/healthz")).status == 200
        assert (await client.get("/readyz")).status == 503
        state["ready"] = True
        assert (await client.get("/readyz")).status == 200
        resp = await client.get("/metrics")
        assert resp.status == 200
        assert 'bluetti_polls_total{result="ok"} 1.0' in await resp.text()


async def test_metrics_disabled() -> None:
    server = HealthServer(0, alive=lambda: False, ready=lambda: False, metrics=None)
    async with TestClient(TestServer(server.app())) as client:
        assert (await client.get("/healthz")).status == 503
        assert (await client.get("/metrics")).status == 404


# reports and schedule


def with_schedule(m: Monitor, storage: Storage, schedule: Schedule) -> ScheduleService:
    client = ScheduleClient(cast(aiohttp.ClientSession, None), "yasno", "kyiv", schedule.group)
    service = ScheduleService(client, storage)
    service.current = schedule
    m.schedule = service
    return service


async def test_weekly_summary_sent_once(tmp_path: Path, storage: Storage) -> None:
    # T0 is Friday 12:00 UTC.
    m, src, sender, clock = make_monitor(tmp_path, storage, weekly_summary="fri 12:00")
    await m.start()
    await poll(m, src, clock, {SN: make_sample()})
    await poll(m, src, clock, {SN: make_sample()})
    summaries = [t for t in sender.sent if t.startswith("📅 Weekly summary")]
    assert len(summaries) == 1
    assert "13.11 – 20.11" in summaries[0]
    assert "No outages ✅" in summaries[0]


async def test_weekly_summary_skipped_when_missed(tmp_path: Path, storage: Storage) -> None:
    m, src, sender, clock = make_monitor(tmp_path, storage, weekly_summary="thu 23:00")
    await m.start()
    await poll(m, src, clock, {SN: make_sample()})
    assert not any(t.startswith("📅 Weekly") for t in sender.sent)
    assert storage.get("weekly_summary_sent") is not None


async def test_schedule_warning_publication_and_planned_outage(
    tmp_path: Path, storage: Storage
) -> None:
    m, src, sender, clock = make_monitor(tmp_path, storage, notify_startup=False)
    today = T0.date()
    slot = Slot(T0 + timedelta(minutes=20), T0 + timedelta(minutes=80))
    service = with_schedule(m, storage, Schedule("3.1", (slot,), (today,)))
    await m.start()
    grid = {SN: make_sample(grid=0, ac=0, soc=40)}
    await poll(m, src, clock, grid)
    # Today's schedule seen for the first time: no "published" message, but a warning.
    assert sender.sent == [
        "⏰ Planned outage in 20 min: 12:20–13:20 (group 3.1)\nBattery: 40%\n"
        "The battery is not charged much — keep that in mind."
    ]
    await poll(m, src, clock, grid)
    assert len(sender.sent) == 1  # warned once per slot

    tomorrow = Slot(T0 + timedelta(days=1), T0 + timedelta(days=1, hours=2))
    service.current = Schedule("3.1", (slot, tomorrow), (today, today + timedelta(days=1)))
    await poll(m, src, clock, grid)
    assert sender.sent[-1].startswith("📅 A new outage schedule is out.\n21.11:\n• 12:00–14:00")

    battery = {SN: make_sample(grid=0, ac=80, soc=40)}
    clock.now = slot.start
    await poll(m, src, clock, battery)
    await poll(m, src, clock, battery)
    assert sender.sent[-2].startswith("⚡ Power went out at 12:20")
    assert sender.sent[-2].endswith("📅 On the schedule: 12:20–13:20")
    assert sender.sent[-1].startswith("🔋 Battery is down to 40% (alert at 50%)")
    assert "Planned outage now: 12:20–13:20" in m.status_text()
    assert "Today, 20.11:\n• 12:20–13:20" in m.schedule_text()


async def test_grid_lost_shows_typical_outage(tmp_path: Path, storage: Storage) -> None:
    for i in range(3):
        start = T0 - timedelta(days=i + 1)
        storage.start_outage(SN, start, None)
        storage.end_outage(SN, start + timedelta(hours=2), None)
    m, src, sender, clock = make_monitor(tmp_path, storage, notify_startup=False)
    await m.start()
    await poll(m, src, clock, {SN: make_sample(grid=0, ac=0)})
    battery = {SN: make_sample(grid=0, ac=80, soc=90, DsgFullTime="600")}
    await poll(m, src, clock, battery)
    await poll(m, src, clock, battery)
    assert sender.sent[-1].splitlines()[-2:] == [
        "📈 Outages usually last ≈ 2 h (median, last 30 days)",
        "The battery should last until power is back as usual (≈ 1 h 59 min more) ✅",
    ]
    assert "Outages usually last ≈ 2 h" in m.status_text()
    assert m.stats_text(30).startswith("📊 Last 30 days\n\nOutages: 4")


async def test_schedule_service_fetches_and_persists(tmp_path: Path, storage: Storage) -> None:
    from aioresponses import aioresponses

    from .test_reports import YASNO

    async with aiohttp.ClientSession() as session:
        client = ScheduleClient(session, "yasno", "kyiv", "3.1")
        service = ScheduleService(client, storage)
        with aioresponses() as mock:
            mock.get(client.url, payload=YASNO)
            mock.get(client.url, status=503)
            await service.refresh()
            assert service.current is not None
            await service.refresh()  # failure keeps the last schedule
        assert len(service.current.slots) == 3
    reloaded = ScheduleService(client, storage)
    assert reloaded.current == service.current


def test_parse_stats_days() -> None:
    assert parse_stats_days(None) == 30
    assert parse_stats_days(CommandObject(command="stats", args="7")) == 7
    assert parse_stats_days(CommandObject(command="stats", args="9999")) == 365


async def test_expired_login_of_one_station_does_not_stop_the_other(
    tmp_path: Path, storage: Storage
) -> None:
    m, src, sender, clock = make_monitor(
        tmp_path,
        storage,
        bluetti_stations="home,cottage",
        bluetti_site_vars={"home": {"name": "Home"}, "cottage": {"name": "Cottage"}},
    )
    await m.start()
    src.errors = {"cottage": AuthExpiredError("invalid_grant")}
    assert await poll(m, src, clock, {SN: make_sample()}) == 60
    assert await poll(m, src, clock, {SN: make_sample()}) == 60
    assert sender.sent[1].startswith("🤖 Bot started: grid power is present")
    assert sender.sent[0::2] == [
        "🔑 The BLUETTI login of Cottage has expired. Run `bluetti-grid-bot login --station "
        "cottage` again and update BLUETTI_COTTAGE_REFRESH_TOKEN. The bot keeps retrying."
    ]
    assert "login of Cottage is invalid — run `login --station cottage`" in m.status_text()
    assert m.auth_expired
    # The cottage signs in again and its station is added.
    src.errors = {}
    src.stations = [*src.stations, Station("COTTAGE0001", "AC180", "Cottage")]
    await poll(m, src, clock, {SN: make_sample(), "COTTAGE0001": make_sample(sn="COTTAGE0001")})
    assert sender.sent[2] == "🔑 The BLUETTI login of Cottage works again."
    assert not m.auth_expired
    assert [st.sn for st in m.stations] == [SN, "COTTAGE0001"]
    assert "Cottage" in m.status_text()
