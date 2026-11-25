"""The polling loop: fetch samples, run the detector, persist, notify."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from .auth import AuthError, AuthExpiredError
from .cloud import CloudError, StateSource
from .config import Settings, Site
from .detector import DeviceState, evaluate
from .events import (
    BotStartedOnBattery,
    BotStartedOnGrid,
    Event,
    GridLost,
    GridRestored,
    OutageEndedUnobserved,
)
from .health import Metrics, put, station_id
from .messages import Formatter, StationView
from .models import GridState, Reading, Sample, Station, parse_reading
from .schedule import KYIV, ScheduleService, day_fingerprints
from .storage import Outage, Storage, scoped

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
        schedules: Mapping[str, ScheduleService] | None = None,
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
        # Site id -> its outage schedule; sites in the same queue share one service.
        self.schedules = dict(schedules or {})
        self._now = now
        self._monotonic = monotonic
        self.stations: list[Station] = []
        self.states: dict[str, DeviceState] = {}
        self.readings: dict[str, Reading] = {}
        self.online: dict[str, bool] = {}
        self.last_data: dict[str, datetime] = {}
        # Last time the cloud answered for the station at all, also "offline".
        self.last_answer: dict[str, datetime] = {}
        self.last_success: datetime | None = None
        self.sites = {site.id: site for site in settings.sites}
        # Sites whose login is rejected; "" is the single-account login.
        self.expired_sites: set[str] = set()
        # Every site's login is rejected: poll slowly, skip /status refreshes.
        self._auth_blocked = False
        self._start_failures = 0
        self._heartbeat = monotonic()
        self._started = monotonic()
        self._warned_fields: set[tuple[str, str]] = set()
        # Stations whose last answer was the cloud's placeholder (logged once per stretch).
        self._placeholder: set[str] = set()
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
        except (CloudError, AuthError) as exc:
            await self._update_auth(self.source.errors or {"": exc}, failed=True)
            if isinstance(exc, AuthExpiredError):
                return AUTH_RETRY_S
            self._start_failures += 1
            delay = min(START_RETRY_MAX_S, 15.0 * 2.0**self._start_failures)
            log.warning("Could not discover stations (%s); retrying in %.0f s", exc, delay)
            return delay
        await self._update_auth(self.source.errors, failed=False)
        self._start_failures = 0
        self._add_stations(stations)
        log.info("Monitoring %d station(s) in %s mode", len(stations), self.cfg.mode.value)
        log.debug("Stations: %s", ", ".join(s.sn for s in stations))
        return 0.0

    def _add_stations(self, stations: Sequence[Station]) -> None:
        self.stations = [*self.stations, *stations]
        self.formatter.names = {s.sn: s.name for s in self.stations}
        for st in stations:
            self.metrics.add_station(st.name, st.sn)
            state, raw, polled_at = self.storage.load_device(st.sn)
            # Every process start re-baselines silently (see detector._baseline).
            self.states[st.sn] = replace(state or DeviceState(st.sn), baseline_pending=True)
            if raw:
                self.readings[st.sn] = parse_reading(raw)[0]
            if polled_at:
                self.last_data[st.sn] = polled_at

    async def poll_once(self) -> float:
        now = self._now()
        samples: dict[str, Sample | None] = {}
        try:
            async with self._fetch_lock:
                samples = await self.source.fetch()
        except (CloudError, AuthError) as exc:
            auth = isinstance(exc, AuthExpiredError)
            self.metrics.polls.labels("auth_error" if auth else "error").inc()
            if not auth:
                log.warning("Poll failed: %s", exc)
            await self._update_auth(self.source.errors or {"": exc}, failed=True)
        else:
            self.metrics.polls.labels("ok").inc()
            self.last_success = now
            self.metrics.last_success.set(now.timestamp())
            await self._update_auth(self.source.errors, failed=False)
            # A site that could not start earlier may have started now.
            if new := [st for st in self.source.stations if st.sn not in self.states]:
                self._add_stations(new)
                log.info("Monitoring %d more station(s)", len(new))

        for st in self.stations:
            await self._process(st, samples.get(st.sn), now)
        await self._weekly_summary(now)
        await self._schedule_alerts(now)
        return AUTH_RETRY_S if self._auth_blocked else float(self.settings.poll_interval_seconds)

    async def _process(self, st: Station, sample: Sample | None, now: datetime) -> None:
        if sample is not None:
            self.online[st.sn] = sample.online and not sample.placeholder
            self.last_answer[st.sn] = now
            if sample.placeholder and st.sn not in self._placeholder:
                log.warning("%s: placeholder answer (SOC 0 %%, every power 0); no data", st.sn)
            if sample.placeholder:
                self._placeholder.add(st.sn)
            else:
                self._placeholder.discard(st.sn)
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
        if result.observed is not None:
            log.debug("%s: %s (%s)", st.sn, result.observed.state, result.observed.reason)
        for event in result.events:
            await self._handle(event, now)

    async def _handle(self, event: Event, now: datetime) -> None:
        log.info("Event: %s", type(event).__name__)
        self.metrics.events.labels(
            self.formatter.names.get(event.sn, ""), station_id(event.sn), type(event).__name__
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
                # Restarted during an outage that was already reported: nothing new to say.
                if event.since_known and not self.settings.notify_startup:
                    return
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
            text = "\n".join([text, *self._grid_lost_extra(event)])
        if text:
            await self.sender.broadcast(text)

    def _grid_lost_extra(self, event: GridLost) -> list[str]:
        lines: list[str] = []
        site = next((st.site for st in self.stations if st.sn == event.sn), "")
        service = self.schedules.get(site)
        if service is not None and service.current is not None:
            match = self.formatter.schedule_match(service.current, event.at, SCHEDULE_SLACK)
            if match:
                lines.append(match)
        return lines

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

    def _schedule_groups(self) -> list[tuple[ScheduleService, str, set[str]]]:
        """Each distinct schedule with the sites using it and, when there are several
        schedules, a label naming those sites."""
        services = list({id(sv): sv for sv in self.schedules.values()}.values())
        groups = []
        for service in services:
            sites = {sid for sid, sv in self.schedules.items() if sv is service}
            groups.append((service, self._sites_label(sites) if len(services) > 1 else "", sites))
        return groups

    def _sites_label(self, sites: set[str]) -> str:
        return ", ".join(self._site(sid).label for sid in sorted(sites))

    def _warn_minutes(self, site: str) -> tuple[int, ...]:
        if (s := self.sites.get(site)) is not None:
            return s.schedule_warn_minutes
        return tuple(self.settings.schedule_warn_minutes)

    async def _schedule_alerts(self, now: datetime) -> None:
        groups = self._schedule_groups()
        # Sites of one schedule may warn at different times: one warning per such set.
        warn_groups = []
        for service, _, sites in groups:
            for warn in dict.fromkeys(self._warn_minutes(sid) for sid in sorted(sites)):
                same = {sid for sid in sites if self._warn_minutes(sid) == warn}
                warn_groups.append((service, warn, same))
        for service, label, _ in groups:
            if service.current is not None:
                await self._schedule_published(service, label, now)
        for service, warn, sites in warn_groups:
            if service.current is not None and warn:
                label = self._sites_label(sites) if len(warn_groups) > 1 else ""
                await self._schedule_warning(service, warn, sites, label, now)

    async def _schedule_published(
        self, service: ScheduleService, label: str, now: datetime
    ) -> None:
        schedule = service.current
        assert schedule is not None  # noqa: S101 - checked by the caller
        # A new day published (usually tomorrow's) or a published day changed.
        today = now.astimezone(KYIV).date().isoformat()
        prints = day_fingerprints(schedule)
        seen_key = scoped(_SCHEDULE_SEEN_KEY, service.key)
        stored = self.storage.get(seen_key)
        seen: dict[str, str] = json.loads(stored) if stored else {}
        if stored is not None:
            for day, fingerprint in sorted(prints.items()):
                if day >= today and seen.get(day) != fingerprint:
                    text = self.formatter.schedule_published(
                        schedule, date.fromisoformat(day), day in seen, now
                    )
                    await self.sender.broadcast(self.formatter.labelled(label, text))
        seen.update(prints)
        self.storage.set(seen_key, json.dumps({d: f for d, f in seen.items() if d >= today}))

    async def _schedule_warning(
        self,
        service: ScheduleService,
        warn: tuple[int, ...],
        sites: set[str],
        label: str,
        now: datetime,
    ) -> None:
        schedule = service.current
        assert schedule is not None  # noqa: S101 - checked by the caller
        slot = schedule.upcoming(now)
        if slot is None:
            return
        due = [w for w in warn if slot.start - now <= timedelta(minutes=w)]
        if not due:
            return
        # Warn once per due threshold; starting inside several (10 min before with
        # "60,15") sends one warning, not one per threshold.
        level = min(due)
        key = scoped(_SCHEDULE_WARNED_KEY, f"{service.key}:{','.join(map(str, warn))}")
        if (stored := self.storage.get(key)) is not None:
            at, _, sent = stored.partition("|")
            if at == slot.start.isoformat() and sent.isdigit() and int(sent) <= level:
                return
        self.storage.set(key, f"{slot.start.isoformat()}|{level}")
        views = [v for v in self.views() if v.station.site in sites]
        if views and all(v.state.effective is GridState.BATTERY for v in views):
            return  # already without power
        text = self.formatter.schedule_warning(slot, schedule.group, views, now)
        await self.sender.broadcast(self.formatter.labelled(label, text))

    # metrics

    def render_metrics(self) -> bytes:
        # Station values are set on every scrape, so they also expire when polling fails.
        now = self._now()
        for st in self.stations:
            self._update_metrics(st, now)
        return self.metrics.render()

    def _update_metrics(self, st: Station, now: datetime) -> None:
        m, labels, state = self.metrics, (st.name, station_id(st.sn)), self.states[st.sn]
        for gs in GridState:
            m.grid_state.labels(*labels, gs.value).set(1 if state.effective is gs else 0)
        since = None if state.effective is GridState.UNKNOWN else state.power_since
        put(m.grid_state_since, None if since is None else since.timestamp(), *labels)
        if (last := self.last_data.get(st.sn)) is not None:
            m.last_data.labels(*labels).set(last.timestamp())
        # Like /readyz, short gaps are bridged; after that old values are not exposed.
        online = self.online.get(st.sn) if self._fresh(self.last_answer.get(st.sn), now) else None
        put(m.online, online, *labels)
        r = self.readings.get(st.sn) if self._fresh(last, now) else None
        r = r or Reading()
        put(m.soc, r.soc, *labels)
        runtime = r.runtime_min if state.effective is GridState.BATTERY else None
        put(m.runtime, runtime * 60 if runtime else None, *labels)
        put(m.time_to_full, r.time_to_full_min * 60 if r.time_to_full_min else None, *labels)
        for channel, watts in (
            ("grid", r.grid_w),
            ("ac_load", r.ac_load_w),
            ("dc_load", r.dc_load_w),
            ("pv", r.pv_w),
        ):
            put(m.power, watts, *labels, channel)
        for output, on, eco in (("ac", r.ac_on, r.ac_eco), ("dc", r.dc_on, r.dc_eco)):
            put(m.output_on, on, *labels, output)
            put(m.output_eco, eco, *labels, output)

    # auth notifications

    @property
    def auth_expired(self) -> bool:
        return bool(self.expired_sites)

    async def _update_auth(self, errors: Mapping[str, Exception], *, failed: bool) -> None:
        """Notify about logins that expired, and (unless the whole call ``failed``) about
        logins that work again."""
        for site, exc in errors.items():
            if isinstance(exc, AuthExpiredError):
                await self._auth_failed(exc, site)
        if not failed:
            for site in self.sites:
                if site not in errors:
                    await self._auth_ok(site)
        self._auth_blocked = failed and set(self.sites) <= self.expired_sites
        self.metrics.set_logins(self.sites, self.expired_sites)

    def _site(self, site: str) -> Site:
        return self.sites.get(site) or Site(site)

    async def _auth_failed(self, exc: AuthExpiredError, site: str = "") -> None:
        if site not in self.expired_sites:
            log.error("BLUETTI login %sexpired: %s", f"of station {site} " if site else "", exc)
        self.expired_sites.add(site)
        key = scoped(_AUTH_NOTIFIED_KEY, site)
        if self.storage.get(key) is None:
            self.storage.set(key, self._now().isoformat())
            await self.sender.broadcast(self.formatter.auth_expired(self._site(site)))

    async def _auth_ok(self, site: str = "") -> None:
        self.expired_sites.discard(site)
        key = scoped(_AUTH_NOTIFIED_KEY, site)
        if self.storage.get(key) is not None:
            self.storage.set(key, None)
            log.info("BLUETTI login %sworks again", f"of station {site} " if site else "")
            await self.sender.broadcast(self.formatter.auth_restored(self._site(site)))

    # views for /status, /history and the health endpoints

    def views(self, with_stats: bool = False) -> list[StationView]:
        recent: list[Outage] = []
        if with_stats:
            since = self._now() - timedelta(days=STATS_DAYS)
            recent = self.storage.outages_since(since, [st.sn for st in self.stations])
        views = []
        for st in self.stations:
            own = tuple(o for o in recent if o.sn == st.sn)
            views.append(
                StationView(
                    station=st,
                    state=self.states[st.sn],
                    reading=self.readings.get(st.sn),
                    online=self.online.get(st.sn),
                    last_poll=self.last_data.get(st.sn),
                    recent_outages=own if with_stats else None,
                )
            )
        return views

    async def refresh(self) -> None:
        """Fetch current values for /status.

        Only the displayed readings are updated; the detector is not advanced, so asking
        for /status often does not shorten the debounce of outage detection. On failure the
        last known values are shown (with their poll time).
        """
        if not self.stations or self._auth_blocked:
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
            self.online[st.sn] = sample.online and not sample.placeholder
            self.last_answer[st.sn] = now
            if sample.has_data:
                self.readings[st.sn] = sample.reading
                self.last_data[st.sn] = now
                self.storage.save_device(self.states[st.sn], sample.raw, now)

    def status_text(self) -> str:
        return self.formatter.status(
            self.views(with_stats=True),
            self._now(),
            "" in self.expired_sites,
            STATS_DAYS,
            [(label, sv.current) for sv, label, _ in self._schedule_groups() if sv.current],
            expired_sites=[self._site(s) for s in sorted(self.expired_sites) if s],
        )

    def stats_text(self, days: int) -> str:
        now = self._now()
        sns = [st.sn for st in self.stations]
        outages = self.storage.outages_since(now - timedelta(days=days), sns)
        return self.formatter.stats_report(self.stations, outages, now, days)

    def schedule_text(self) -> str:
        now = self._now()
        texts = [
            self.formatter.labelled(label, self.formatter.schedule(sv.current, now))
            for sv, label, _ in self._schedule_groups()
        ]
        return "\n\n".join(texts) or self.formatter.schedule(None, now)

    def history_text(self, limit: int) -> str:
        outages = self.storage.history(limit, [s.sn for s in self.stations] or None)
        return self.formatter.history(outages, self._now())

    def alive(self) -> bool:
        # A loop step can take a few minutes in the worst case (retries, rebind).
        budget = max(self.settings.poll_interval_seconds, AUTH_RETRY_S, START_RETRY_MAX_S) + 600
        return self._monotonic() - self._heartbeat < budget

    def ready(self) -> bool:
        return self._fresh(self.last_success, self._now())

    def _fresh(self, at: datetime | None, now: datetime) -> bool:
        window = max(3 * self.settings.poll_interval_seconds, 300)
        return at is not None and (now - at).total_seconds() < window
