"""HTTP endpoints: ``/healthz`` (process alive), ``/readyz`` (polling works), ``/metrics``."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from typing import get_args

from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

from .events import Event

log = logging.getLogger(__name__)

_STATION = ("station", "station_id")


def station_id(sn: str) -> str:
    """Stable id of a station for labels: survives a rename and does not reveal the serial."""
    return hashlib.sha256(sn.encode()).hexdigest()[:8]


def put(gauge: Gauge, value: float | None, *labels: str) -> None:
    """Set the series, or drop it when the value is unknown, so that nothing stale is scraped."""
    if value is None:
        gauge.remove(*labels)
    else:
        gauge.labels(*labels).set(value)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.polls = Counter(
            "bluetti_polls", "Polls of the BLUETTI cloud by result", ["result"], registry=r
        )
        self.last_success = Gauge(
            "bluetti_last_successful_poll_timestamp_seconds",
            "Unix time of the last successful poll",
            registry=r,
        )
        self.grid_state = Gauge(
            "bluetti_grid_state",
            "1 for the station's current state (grid, battery, unknown)",
            [*_STATION, "state"],
            registry=r,
        )
        self.grid_state_since = Gauge(
            "bluetti_grid_state_since_timestamp_seconds",
            "Unix time the current grid state began; absent while the state is unknown",
            _STATION,
            registry=r,
        )
        self.last_data = Gauge(
            "bluetti_station_last_data_timestamp_seconds",
            "Unix time of the last poll with data from the station",
            _STATION,
            registry=r,
        )
        self.online = Gauge("bluetti_station_online", "Cloud online flag", _STATION, registry=r)
        self.soc = Gauge("bluetti_battery_soc_percent", "Battery SOC", _STATION, registry=r)
        self.runtime = Gauge(
            "bluetti_battery_runtime_seconds",
            "Battery runtime left, as the station estimates it; absent off battery",
            _STATION,
            registry=r,
        )
        self.time_to_full = Gauge(
            "bluetti_battery_time_to_full_seconds",
            "Time until the battery is full, as the station estimates it; absent if not charging",
            _STATION,
            registry=r,
        )
        self.power = Gauge(
            "bluetti_power_watts", "Power by channel", [*_STATION, "channel"], registry=r
        )
        self.output_on = Gauge(
            "bluetti_output_on", "1 if the output is on (ac, dc)", [*_STATION, "output"], registry=r
        )
        self.output_eco = Gauge(
            "bluetti_output_eco",
            "1 if ECO mode is on for the output (ac, dc)",
            [*_STATION, "output"],
            registry=r,
        )
        self.login_expired = Gauge(
            "bluetti_login_expired",
            "1 if the BLUETTI login is rejected; site is empty for a single account",
            ["site"],
            registry=r,
        )
        self.events = Counter(
            "bluetti_events", "Detector events by type", [*_STATION, "type"], registry=r
        )
        self.notifications = Counter(
            "bluetti_notifications", "Telegram notifications by result", ["result"], registry=r
        )
        # Counters start at 0: increase() misses the first event of a series that appears at 1.
        for result in ("ok", "error", "auth_error"):
            self.polls.labels(result)
        for result in ("ok", "error"):
            self.notifications.labels(result)

    def add_station(self, name: str, sn: str) -> None:
        for event in get_args(Event):
            self.events.labels(name, station_id(sn), event.__name__)

    def set_logins(self, sites: Iterable[str], expired: set[str]) -> None:
        for site in sites:
            self.login_expired.labels(site).set(site in expired)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class HealthServer:
    def __init__(
        self,
        port: int,
        *,
        alive: Callable[[], bool],
        ready: Callable[[], bool],
        metrics: Callable[[], bytes] | None,
        host: str = "0.0.0.0",  # noqa: S104 - container / pod networking
    ) -> None:
        self._port = port
        self._host = host
        self._alive = alive
        self._ready = ready
        self._metrics = metrics
        self._runner: web.AppRunner | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self._healthz)
        app.router.add_get("/readyz", self._readyz)
        if self._metrics is not None:
            app.router.add_get("/metrics", self._metrics_handler)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        log.info("HTTP server listening on :%d", self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _healthz(self, _: web.Request) -> web.Response:
        ok = self._alive()
        return web.Response(text="ok" if ok else "unhealthy", status=200 if ok else 503)

    async def _readyz(self, _: web.Request) -> web.Response:
        ok = self._ready()
        return web.Response(text="ready" if ok else "not ready", status=200 if ok else 503)

    async def _metrics_handler(self, _: web.Request) -> web.Response:
        assert self._metrics is not None  # noqa: S101 - route only registered with metrics
        body = self._metrics()
        return web.Response(body=body, headers={"Content-Type": CONTENT_TYPE_LATEST})
