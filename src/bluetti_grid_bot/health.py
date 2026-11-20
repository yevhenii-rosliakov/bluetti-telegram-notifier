"""HTTP endpoints: ``/healthz`` (process alive), ``/readyz`` (polling works), ``/metrics``."""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

log = logging.getLogger(__name__)


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
            ["station", "state"],
            registry=r,
        )
        self.online = Gauge("bluetti_station_online", "Cloud online flag", ["station"], registry=r)
        self.soc = Gauge("bluetti_battery_soc_percent", "Battery SOC", ["station"], registry=r)
        self.power = Gauge(
            "bluetti_power_watts", "Power by channel", ["station", "channel"], registry=r
        )
        self.events = Counter(
            "bluetti_events", "Detector events by type", ["station", "type"], registry=r
        )
        self.notifications = Counter(
            "bluetti_notifications", "Telegram notifications by result", ["result"], registry=r
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


class HealthServer:
    def __init__(
        self,
        port: int,
        *,
        alive: Callable[[], bool],
        ready: Callable[[], bool],
        metrics: Metrics | None,
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
        body = self._metrics.render()
        return web.Response(body=body, headers={"Content-Type": CONTENT_TYPE_LATEST})
