"""Planned outage schedules (графіки погодинних відключень) for one queue/group.

Two sources, both unofficial and undocumented, so parsing is defensive:

- ``yasno``: the Yasno app API (Kyiv, Dnipro). First-party, minute resolution.
- ``outage-data-ua``: JSON published by the Baskerville42/outage-data-ua project, which
  scrapes the DTEK sites (Kyiv, Kyiv region, Dnipro, Odesa). Half-hour resolution.

The latest schedule is kept in the state database, so a restart (or the source being
down during an outage) does not lose it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .storage import Storage

log = logging.getLogger(__name__)

# Schedules are published in Kyiv time whatever TZ the bot uses for messages.
KYIV = ZoneInfo("Europe/Kyiv")

YASNO_URL = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/"
    "regions/{region}/dsos/{dso}/planned-outages"
)
# SCHEDULE_REGION -> (region id, DSO id). The two Dnipro operators have different schedules.
YASNO_REGIONS: dict[str, tuple[int, int]] = {
    "kyiv": (25, 902),
    "dnipro-dnem": (3, 301),
    "dnipro-cek": (3, 303),
}
OUTAGE_DATA_URL = (
    "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{region}.json"
)
OUTAGE_DATA_REGIONS = ("kyiv", "kyiv-region", "dnipro", "odesa")
PROVIDERS: dict[str, tuple[str, ...]] = {
    "yasno": tuple(YASNO_REGIONS),
    "outage-data-ua": OUTAGE_DATA_REGIONS,
}

REFRESH_INTERVAL_S = 600.0
FETCH_TIMEOUT_S = 30.0
_STORAGE_KEY = "schedule"
_HEADERS = {"User-Agent": "Mozilla/5.0 (bluetti-grid-bot)", "Accept": "application/json"}


class ScheduleError(Exception):
    """The schedule could not be fetched or understood."""


@dataclass(frozen=True, slots=True)
class Slot:
    start: datetime
    end: datetime
    # The source only says an outage is possible ("maybe").
    possible: bool = False


@dataclass(frozen=True, slots=True)
class Schedule:
    group: str
    slots: tuple[Slot, ...]
    # Days the schedule is published for (a day without slots = no outages planned).
    days: tuple[date, ...]
    # Text from the provider, e.g. "emergency outages, the schedules do not apply".
    notice: str | None = None

    def covers(self, moment: datetime) -> bool:
        return moment.astimezone(KYIV).date() in self.days

    def for_day(self, day: date) -> list[Slot]:
        return [s for s in self.slots if s.start.astimezone(KYIV).date() == day]

    def current(self, now: datetime, slack: timedelta = timedelta()) -> Slot | None:
        """The definite slot ``now`` is in (``slack`` widens it on both sides)."""
        for s in self.slots:
            if not s.possible and s.start - slack <= now < s.end + slack:
                return s
        return None

    def upcoming(self, now: datetime) -> Slot | None:
        """The next definite slot that has not started yet."""
        return next((s for s in self.slots if not s.possible and s.start > now), None)

    def to_json(self) -> str:
        return json.dumps(
            {
                "group": self.group,
                "slots": [[s.start.isoformat(), s.end.isoformat(), s.possible] for s in self.slots],
                "days": [d.isoformat() for d in self.days],
                "notice": self.notice,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Schedule:
        data = json.loads(text)
        return cls(
            group=data["group"],
            slots=tuple(
                Slot(datetime.fromisoformat(a), datetime.fromisoformat(b), bool(p))
                for a, b, p in data["slots"]
            ),
            days=tuple(date.fromisoformat(d) for d in data["days"]),
            notice=data.get("notice"),
        )


def _at(day: date, minutes: int) -> datetime:
    # Wall-clock arithmetic in Kyiv time; minute 1440 is the next midnight.
    return (datetime.combine(day, time(), KYIV) + timedelta(minutes=minutes)).astimezone(UTC)


def _merge(slots: list[Slot]) -> tuple[Slot, ...]:
    """Sort and join touching slots of the same kind (e.g. 23:00-24:00 + 00:00-02:00)."""
    out: list[Slot] = []
    for s in sorted(slots, key=lambda s: s.start):
        if out and out[-1].possible == s.possible and s.start <= out[-1].end:
            out[-1] = Slot(out[-1].start, max(out[-1].end, s.end), s.possible)
        else:
            out.append(s)
    return tuple(out)


def parse_yasno(data: Any, group: str) -> Schedule:
    if not isinstance(data, dict):
        raise ScheduleError("unexpected Yasno response")
    queue = data.get(group)
    if not isinstance(queue, dict):
        known = ", ".join(sorted(data, key=_group_key)[:12])
        raise ScheduleError(f"group {group!r} not in the Yasno schedule (known: {known}, ...)")
    slots: list[Slot] = []
    days: list[date] = []
    notices: list[str] = []
    for key in ("today", "tomorrow"):
        day = queue.get(key)
        if not isinstance(day, dict) or not day.get("date"):
            continue
        status = day.get("status")
        on = datetime.fromisoformat(day["date"]).astimezone(KYIV).date()
        if status == "WaitingForSchedule":
            continue
        if status != "ScheduleApplies":
            # Emergency outages and the like: the hourly schedule does not apply.
            notices.append(f"{on:%d.%m}: {status}")
            continue
        days.append(on)
        for s in day.get("slots") or []:
            if s.get("type") == "Definite":
                slots.append(Slot(_at(on, int(s["start"])), _at(on, int(s["end"]))))
    return Schedule(group, _merge(slots), tuple(days), "; ".join(notices) or None)


# outage-data-ua hour states: (start minute, end minute, possible) inside the hour.
_HOUR_STATES: dict[str, tuple[int, int, bool]] = {
    "no": (0, 60, False),
    "first": (0, 30, False),
    "second": (30, 60, False),
    "maybe": (0, 60, True),
    "mfirst": (0, 30, True),
    "msecond": (30, 60, True),
}


def parse_outage_data(data: Any, group: str) -> Schedule:
    if not isinstance(data, dict) or not isinstance(data.get("fact"), dict):
        raise ScheduleError("unexpected outage-data-ua response")
    status = data.get("lastUpdateStatus") or {}
    if status.get("ok") is False:
        log.warning("outage-data-ua: the last scrape failed (%s); data may be old", status)
    key = f"GPV{group}"
    slots: list[Slot] = []
    days: list[date] = []
    for ts, groups in sorted((data["fact"].get("data") or {}).items()):
        hours = groups.get(key) if isinstance(groups, dict) else None
        if not isinstance(hours, dict):
            continue
        on = datetime.fromtimestamp(int(ts), KYIV).date()
        days.append(on)
        for hour in range(1, 25):
            if (state := _HOUR_STATES.get(str(hours.get(str(hour))))) is not None:
                start, end, possible = state
                base = (hour - 1) * 60
                slots.append(Slot(_at(on, base + start), _at(on, base + end), possible))
    if not days:
        raise ScheduleError(f"group {group!r} not in the outage-data-ua schedule")
    notice = data.get("attention")
    return Schedule(group, _merge(slots), tuple(days), notice if isinstance(notice, str) else None)


def _group_key(group: str) -> tuple[int, ...]:
    with contextlib.suppress(ValueError):
        return tuple(int(p) for p in group.split("."))
    return (999,)


class ScheduleClient:
    def __init__(self, session: aiohttp.ClientSession, provider: str, region: str, group: str):
        self._session = session
        self.provider = provider
        self.group = group
        if provider == "yasno":
            region_id, dso_id = YASNO_REGIONS[region]
            self.url = YASNO_URL.format(region=region_id, dso=dso_id)
            self._parse: Callable[[Any, str], Schedule] = parse_yasno
        else:
            self.url = OUTAGE_DATA_URL.format(region=region)
            self._parse = parse_outage_data

    async def fetch(self) -> Schedule:
        try:
            async with self._session.get(
                self.url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
            ) as resp:
                if resp.status != 200:
                    raise ScheduleError(f"{self.provider}: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ScheduleError(f"{self.provider}: {exc}") from exc
        try:
            return self._parse(data, self.group)
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleError(f"{self.provider}: unexpected data ({exc!r})") from exc


class ScheduleService:
    """Keeps the latest schedule: refreshes it periodically and stores it."""

    def __init__(self, client: ScheduleClient, storage: Storage) -> None:
        self._client = client
        self._storage = storage
        self.group = client.group
        self.current: Schedule | None = None
        if (text := storage.get(_STORAGE_KEY)) is not None:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                stored = Schedule.from_json(text)
                if stored.group == self.group:
                    self.current = stored

    async def refresh(self) -> None:
        try:
            schedule = await self._client.fetch()
        except ScheduleError as exc:
            log.warning("Could not update the outage schedule: %s", exc)
            return
        self.current = schedule
        self._storage.set(_STORAGE_KEY, schedule.to_json())

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.refresh()
            except Exception:
                log.exception("Unexpected error while updating the outage schedule")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=REFRESH_INTERVAL_S)


def day_fingerprints(schedule: Schedule) -> Mapping[str, str]:
    """A short text per published day; a change means the day's schedule changed."""
    return {
        d.isoformat(): ",".join(
            f"{s.start:%H%M}-{s.end:%H%M}{'?' if s.possible else ''}" for s in schedule.for_day(d)
        )
        for d in schedule.days
    }
