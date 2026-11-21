"""The polling loop: fetch samples, run the detector, persist, notify."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from .auth import AuthError, AuthExpiredError
from .cloud import CloudError, StateSource
from .config import Settings
from .detector import DeviceState, evaluate
from .events import (
    BotStartedOnBattery,
    BotStartedOnGrid,
    Event,
    GridLost,
    GridRestored,
    OutageEndedUnobserved,
)
from .health import Metrics
from .messages import Formatter, StationView
from .models import GridState, Reading, Sample, Station, parse_reading
from .schedule import KYIV, ScheduleService, day_fingerprints
from .stats import typical_duration
from .storage import Outage, Storage

log = logging.getLogger(__name__)

# While the refresh token is rejected, retry this often (a `login` run from another
# process is picked up on the next attempt).
AUTH_RETRY_S = 300.0
START_RETRY_MAX_S = 300.0
# /status fetches fresh data, but not more often than this (several users, button mashing).
REFRESH_MIN_INTERVAL_S = 10.0
REFRESH_TIMEOUT_S = 25.0
# /status shows outage statistics for this many last days.
STATS_DAYS = 7
# "Outages usually last ..." is the median over this many last days.
TYPICAL_DAYS = 30
# A weekly summary is skipped if the bot was not running at that time for longer than this.
WEEKLY_LATE_MAX = timedelta(hours=12)
# An outage starting this close to a planned slot counts as planned.
SCHEDULE_SLACK = timedelta(minutes=15)
_AUTH_NOTIFIED_KEY = "auth_expired_notified"
_WEEKLY_KEY = "weekly_summary_sent"
_SCHEDULE_SEEN_KEY = "schedule_seen"
_SCHEDULE_WARNED_KEY = "schedule_warned"


class Sender(Protocol):
    async def broadcast(self, text: str) -> None: ...


class Monitor:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        source: StateSource,
        sender: Sender,
        formatter: Formatter,
        metrics: Metrics,
        *,
        schedule: ScheduleService | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.cfg = settings.detector_config()
        self.storage = storage
        self.source = source
        self.sender = sender
        self.formatter = formatter
        self.metrics = metrics
        self.schedule = schedule
        self._now = now
        self._monotonic = monotonic
        self.stations: list[Station] = []
        self.states: dict[str, DeviceState] = {}
        self.readings: dict[str, Reading] = {}
        self.online: dict[str, bool] = {}
        self.last_data: dict[str, datetime] = {}
        self.last_success: datetime | None = None
        self.auth_expired = False
        self._start_failures = 0
        self._heartbeat = monotonic()
        self._started = monotonic()
        self._warned_fields: set[tuple[str, str]] = set()
        # One cloud request at a time: the polling loop and /status share the source.
        self._fetch_lock = asyncio.Lock()
        self._last_refresh: float | None = None

    # lifecycle

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._heartbeat = self._monotonic()
            try:
                delay = await self.poll_once() if self.stations else await self.start()
            except Exception:
                log.exception("Unexpected error in the polling loop")
                delay = float(self.settings.poll_interval_seconds)
            if delay > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)

    async def start(self) -> float:
        """Discover and bind stations. Returns the delay before the next loop step."""
        try:
            stations = await self.source.start()
        except AuthExpiredError as exc:
            await self._auth_failed(exc)
            return AUTH_RETRY_S
        except (CloudError, AuthError) as exc:
            self._start_failures += 1
            delay = min(START_RETRY_MAX_S, 15.0 * 2.0**self._start_failures)
            log.warning("Could not discover stations (%s); retrying in %.0f s", exc, delay)
            return delay
        await self._auth_ok()
        self._start_failures = 0
        self.stations = stations
        self.formatter.names = {s.sn: s.name for s in stations}
        for st in stations:
            state, raw, polled_at = self.storage.load_device(st.sn)
            # Every process start re-baselines silently (see detector._baseline).
            self.states[st.sn] = replace(state or DeviceState(st.sn), baseline_pending=True)
            if raw:
                self.readings[st.sn] = parse_reading(raw)[0]
            if polled_at:
                self.last_data[st.sn] = polled_at
        log.info("Monitoring %d station(s) in %s mode", len(stations), self.cfg.mode.value)
        log.debug("Stations: %s", ", ".join(s.sn for s in stations))
        return 0.0

    async def poll_once(self) -> float:
        now = self._now()
        samples: dict[str, Sample | None] = {}
        try:
            async with self._fetch_lock:
                samples = await self.source.fetch()
        except AuthExpiredError as exc:
            self.metrics.polls.labels("auth_error").inc()
            await self._auth_failed(exc)
        except (CloudError, AuthError) as exc:
            self.metrics.polls.labels("error").inc()
            log.warning("Poll failed: %s", exc)
        else:
            self.metrics.polls.labels("ok").inc()
            self.last_success = now
            self.metrics.last_success.set(now.timestamp())
            await self._auth_ok()

        for st in self.stations:
            await self._process(st, samples.get(st.sn), now)
        await self._weekly_summary(now)
        await self._schedule_alerts(now)
        return AUTH_RETRY_S if self.auth_expired else float(self.settings.poll_interval_seconds)

    async def _process(self, st: Station, sample: Sample | None, now: datetime) -> None:
        if sample is not None:
            self.online[st.sn] = sample.online
            for field in sample.invalid_fields:
                if (st.sn, field) not in self._warned_fields:
                    self._warned_fields.add((st.sn, field))
                    log.warning(
                        "Field %s has an unexpected value %r; treating it as unknown",
                        field,
                        sample.raw.get(field),
                    )
        result = evaluate(self.states[st.sn], sample, self.cfg, now)
        self.states[st.sn] = result.state
        has_data = sample is not None and sample.has_data
        if has_data:
            assert sample is not None  # noqa: S101 - narrowed by has_data
            self.readings[st.sn] = sample.reading
            self.last_data[st.sn] = now
        self.storage.save_device(
            result.state,
            sample.raw if has_data and sample is not None else None,
            now if has_data else None,
        )
        self._update_metrics(st, result.state)
        if result.observed is not None:
            log.debug("%s: %s (%s)", st.sn, result.observed.state, result.observed.reason)
        for event in result.events:
            await self._handle(event, now)

    async def _handle(self, event: Event, now: datetime) -> None:
        log.info("Event: %s", type(event).__name__)
        self.metrics.events.labels(
            self.formatter.names.get(event.sn, ""), type(event).__name__
        ).inc()
        match event:
            case GridLost():
                self.storage.start_outage(event.sn, event.at, event.earliest)
            case BotStartedOnBattery():
                state = self.states[event.sn]
                # An unknown start is flagged approximate (earliest == start at worst).
                earliest = (
                    None if event.since_known else (state.power_since_earliest or event.since)
                )
                self.storage.start_outage(event.sn, event.since, earliest)
            case GridRestored():
                self.storage.end_outage(event.sn, event.at, event.earliest)
            case OutageEndedUnobserved():
                self.storage.end_outage(event.sn, event.at, event.at)
            case BotStartedOnGrid() if not self.settings.notify_startup:
                return
            case _:
                pass
        text = self.formatter.event(event, now)
        if text and isinstance(event, GridLost):
            text = "\n".join([text, *self._grid_lost_extra(event, now)])
        if text:
            await self.sender.broadcast(text)

    def _grid_lost_extra(self, event: GridLost, now: datetime) -> list[str]:
        lines = self.formatter.runtime_hint(
            event.runtime_min, now - event.at, self._typical(event.sn, now)
        )
        if self.schedule is not None and self.schedule.current is not None:
            match = self.formatter.schedule_match(self.schedule.current, event.at, SCHEDULE_SLACK)
            if match:
                lines.append(match)
        return lines

    def _typical(self, sn: str, now: datetime) -> timedelta | None:
        return typical_duration(
            self.storage.outages_since(now - timedelta(days=TYPICAL_DAYS), [sn])
        )

    # periodic messages

    async def _weekly_summary(self, now: datetime) -> None:
        spec = self.settings.weekly_summary_at
        if spec is None or not self.stations:
            return
        weekday, at = spec
        tz = self.settings.zone
        local = now.astimezone(tz)
        day = local.date() - timedelta(days=(local.weekday() - weekday) % 7)
        due = datetime.combine(day, at, tz)
        if due > local:
            due -= timedelta(days=7)
        sent = self.storage.get(_WEEKLY_KEY)
        if sent is not None and datetime.fromisoformat(sent) >= due:
            return
        self.storage.set(_WEEKLY_KEY, due.isoformat())
        if now - due > WEEKLY_LATE_MAX:
            return
        sns = [st.sn for st in self.stations]
        outages = self.storage.outages_since(now - timedelta(days=2 * STATS_DAYS), sns)
        await self.sender.broadcast(self.formatter.weekly(self.stations, outages, now))

    async def _schedule_alerts(self, now: datetime) -> None:
        if self.schedule is None or (schedule := self.schedule.current) is None:
            return
        # A new day published (usually tomorrow's) or a published day changed.
        today = now.astimezone(KYIV).date().isoformat()
        prints = day_fingerprints(schedule)
        stored = self.storage.get(_SCHEDULE_SEEN_KEY)
        seen: dict[str, str] = json.loads(stored) if stored else {}
        if stored is not None:
            for day, fingerprint in sorted(prints.items()):
                if day >= today and seen.get(day) != fingerprint:
                    text = self.formatter.schedule_published(
                        schedule, date.fromisoformat(day), day in seen, now
                    )
                    await self.sender.broadcast(text)
        seen.update(prints)
        self.storage.set(
            _SCHEDULE_SEEN_KEY, json.dumps({d: f for d, f in seen.items() if d >= today})
        )

        warn = timedelta(minutes=self.settings.schedule_warn_minutes)
        slot = schedule.upcoming(now)
        if not warn or slot is None or slot.start - now > warn:
            return
        if self.storage.get(_SCHEDULE_WARNED_KEY) == slot.start.isoformat():
            return
        self.storage.set(_SCHEDULE_WARNED_KEY, slot.start.isoformat())
        views = self.views()
        if views and all(v.state.effective is GridState.BATTERY for v in views):
            return  # already without power
        await self.sender.broadcast(
            self.formatter.schedule_warning(slot, schedule.group, views, now)
        )

    def _update_metrics(self, st: Station, state: DeviceState) -> None:
        label = st.name
        for gs in GridState:
            self.metrics.grid_state.labels(label, gs.value).set(1 if state.effective is gs else 0)
        if (online := self.online.get(st.sn)) is not None:
            self.metrics.online.labels(label).set(1 if online else 0)
        if (r := self.readings.get(st.sn)) is not None:
            if r.soc is not None:
                self.metrics.soc.labels(label).set(r.soc)
            for channel, value in (
                ("grid", r.grid_w),
                ("ac_load", r.ac_load_w),
                ("dc_load", r.dc_load_w),
                ("pv", r.pv_w),
            ):
                if value is not None:
                    self.metrics.power.labels(label, channel).set(value)

    # auth notifications

    async def _auth_failed(self, exc: AuthExpiredError) -> None:
        if not self.auth_expired:
            log.error("BLUETTI login expired: %s", exc)
        self.auth_expired = True
        if self.storage.get(_AUTH_NOTIFIED_KEY) is None:
            self.storage.set(_AUTH_NOTIFIED_KEY, self._now().isoformat())
            await self.sender.broadcast(self.formatter.t("auth_expired"))

    async def _auth_ok(self) -> None:
        self.auth_expired = False
        if self.storage.get(_AUTH_NOTIFIED_KEY) is not None:
            self.storage.set(_AUTH_NOTIFIED_KEY, None)
            log.info("BLUETTI login works again")
            await self.sender.broadcast(self.formatter.t("auth_restored"))

    # views for /status, /history and the health endpoints

    def views(self, with_stats: bool = False) -> list[StationView]:
        recent: list[Outage] = []
        if with_stats:
            since = self._now() - timedelta(days=TYPICAL_DAYS)
            recent = self.storage.outages_since(since, [st.sn for st in self.stations])
        views = []
        for st in self.stations:
            # The formatter cuts these to the STATS_DAYS window.
            own = tuple(o for o in recent if o.sn == st.sn)
            views.append(
                StationView(
                    station=st,
                    state=self.states[st.sn],
                    reading=self.readings.get(st.sn),
                    online=self.online.get(st.sn),
                    last_poll=self.last_data.get(st.sn),
                    recent_outages=own if with_stats else None,
                    typical_outage=typical_duration(own),
                )
            )
        return views

    async def refresh(self) -> None:
        """Fetch current values for /status.

        Only the displayed readings are updated; the detector is not advanced, so asking
        for /status often does not shorten the debounce of outage detection. On failure the
        last known values are shown (with their poll time).
        """
        if not self.stations or self.auth_expired:
            return
        mono = self._monotonic()
        if self._last_refresh is not None and mono - self._last_refresh < REFRESH_MIN_INTERVAL_S:
            return
        self._last_refresh = mono
        try:
            async with self._fetch_lock:
                samples = await asyncio.wait_for(self.source.fetch(), REFRESH_TIMEOUT_S)
        except (CloudError, AuthError, TimeoutError) as exc:
            log.warning("Refresh for /status failed: %s", exc)
            return
        now = self._now()
        for st in self.stations:
            sample = samples.get(st.sn)
            if sample is None:
                continue
            self.online[st.sn] = sample.online
            if sample.has_data:
                self.readings[st.sn] = sample.reading
                self.last_data[st.sn] = now
                self.storage.save_device(self.states[st.sn], sample.raw, now)
            self._update_metrics(st, self.states[st.sn])

    def status_text(self) -> str:
        return self.formatter.status(
            self.views(with_stats=True),
            self._now(),
            self.auth_expired,
            STATS_DAYS,
            self.schedule.current if self.schedule is not None else None,
        )

    def stats_text(self, days: int) -> str:
        now = self._now()
        sns = [st.sn for st in self.stations]
        outages = self.storage.outages_since(now - timedelta(days=days), sns)
        return self.formatter.stats_report(self.stations, outages, now, days)

    def schedule_text(self) -> str:
        current = self.schedule.current if self.schedule is not None else None
        return self.formatter.schedule(current, self._now())

    def history_text(self, limit: int) -> str:
        outages = self.storage.history(limit, [s.sn for s in self.stations] or None)
        return self.formatter.history(outages, self._now())

    def alive(self) -> bool:
        # A loop step can take a few minutes in the worst case (retries, rebind).
        budget = max(self.settings.poll_interval_seconds, AUTH_RETRY_S, START_RETRY_MAX_S) + 600
        return self._monotonic() - self._heartbeat < budget

    def ready(self) -> bool:
        window = max(3 * self.settings.poll_interval_seconds, 300)
        if self.last_success is None:
            return False
        return (self._now() - self.last_success).total_seconds() < window
