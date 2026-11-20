"""User-facing text: translations and formatting of events, /status and /history.

Pure functions of their inputs (``now`` is passed in), so they are easy to test.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from importlib import resources
from zoneinfo import ZoneInfo

from .detector import DeviceState
from .events import (
    AcOutputOff,
    BotStartedOnBattery,
    BotStartedOnGrid,
    Event,
    GridLost,
    GridRestored,
    LowBattery,
    OutageEndedUnobserved,
    StationOffline,
    StationOnline,
)
from .models import GridState, Reading, Station
from .storage import Outage


@cache
def _load(lang: str) -> dict[str, str]:
    text = resources.files(__package__).joinpath("i18n", f"{lang}.json").read_text("utf-8")
    data: dict[str, str] = json.loads(text)
    return data


class Translator:
    def __init__(self, lang: str) -> None:
        self.lang = lang
        self._strings = _load(lang)
        self._fallback = _load("en")

    def __call__(self, key: str, **kwargs: object) -> str:
        template = self._strings.get(key) or self._fallback.get(key) or key
        return template.format(**kwargs)


def _num(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


@dataclass(frozen=True, slots=True)
class StationView:
    """What /status needs to know about one station."""

    station: Station
    state: DeviceState
    reading: Reading | None
    online: bool | None
    last_poll: datetime | None


class Formatter:
    def __init__(self, t: Translator, tz: ZoneInfo, names: Mapping[str, str]) -> None:
        self.t = t
        self.tz = tz
        self.names = dict(names)

    # basics

    def time(self, value: datetime, now: datetime | None = None) -> str:
        local = value.astimezone(self.tz)
        if now is not None and local.date() == now.astimezone(self.tz).date():
            return local.strftime("%H:%M")
        return local.strftime("%d.%m %H:%M")

    def duration(self, value: timedelta) -> str:
        minutes = max(0, int(value.total_seconds() // 60))
        if minutes == 0:
            return self.t("duration_less_minute")
        days, rest = divmod(minutes, 24 * 60)
        hours, mins = divmod(rest, 60)
        parts = []
        if days:
            parts.append(self.t("duration_days", n=days))
        if hours:
            parts.append(self.t("duration_hours", n=hours))
        if mins and not days:
            parts.append(self.t("duration_minutes", n=mins))
        return " ".join(parts)

    def _prefix(self, sn: str) -> str:
        # The station name is only useful when several stations are monitored.
        return f"[{self.names[sn]}] " if len(self.names) > 1 and sn in self.names else ""

    def _details(self, soc: float | None, load: float | None = None) -> list[str]:
        lines = []
        if soc is not None:
            lines.append(self.t("detail_soc", soc=_num(soc)))
        if load is not None:
            lines.append(self.t("detail_load", load=_num(load)))
        return lines

    # events

    def event(self, ev: Event, now: datetime) -> str | None:
        t = self.t
        p = self._prefix(ev.sn)
        match ev:
            case GridLost():
                if ev.earliest is not None:
                    head = t(
                        "grid_lost_window",
                        prefix=p,
                        earliest=self.time(ev.earliest, now),
                        time=self.time(ev.at, now),
                    )
                else:
                    head = t("grid_lost", prefix=p, time=self.time(ev.at, now))
                return "\n".join([head, *self._details(ev.soc, ev.load_w)])
            case GridRestored():
                if ev.earliest is not None:
                    head = t(
                        "grid_restored_window",
                        prefix=p,
                        earliest=self.time(ev.earliest, now),
                        time=self.time(ev.at, now),
                    )
                else:
                    head = t("grid_restored", prefix=p, time=self.time(ev.at, now))
                if ev.duration is not None:
                    key = "outage_duration_approx" if ev.approximate else "outage_duration"
                    head += ", " + t(key, duration=self.duration(ev.duration))
                return "\n".join([head, *self._details(ev.soc)])
            case BotStartedOnBattery():
                key = "bot_started_battery" if ev.since_known else "bot_started_battery_unknown"
                head = t(key, prefix=p, since=self.time(ev.since, now))
                return "\n".join([head, *self._details(ev.soc, ev.load_w)])
            case BotStartedOnGrid():
                return "\n".join([t("bot_started_grid", prefix=p), *self._details(ev.soc)])
            case LowBattery():
                lines = [t("low_battery", prefix=p, soc=_num(ev.soc), threshold=ev.threshold)]
                if ev.runtime_min:
                    lines.append(
                        t(
                            "detail_runtime",
                            runtime=self.duration(timedelta(minutes=ev.runtime_min)),
                        )
                    )
                return "\n".join(lines)
            case AcOutputOff():
                return t("ac_off", prefix=p)
            case StationOffline():
                text = t("station_offline", prefix=p, since=self.time(ev.since, now))
                if ev.last_seen is not None:
                    text += " " + t("station_offline_seen", last_seen=self.time(ev.last_seen, now))
                return text
            case StationOnline():
                return t(
                    "station_online", prefix=p, duration=self.duration(ev.at - ev.offline_since)
                )
            case OutageEndedUnobserved():
                return None

    # /status

    def status(self, views: Sequence[StationView], now: datetime, auth_expired: bool) -> str:
        t = self.t
        blocks = []
        if auth_expired:
            blocks.append(t("status_auth_expired"))
        if not views:
            blocks.append(t("status_no_stations"))
        for v in views:
            blocks.append(self._station_status(v, now))
        return "\n\n".join(blocks)

    def _station_status(self, v: StationView, now: datetime) -> str:
        t, s, r = self.t, v.state, v.reading
        lines = [t("status_title", name=v.station.name)]
        effective = s.effective
        if effective is GridState.GRID and s.power_since:
            lines.append(t("status_grid", since=self.time(s.power_since, now)))
        elif effective is GridState.BATTERY and s.power_since:
            lines.append(
                t(
                    "status_battery",
                    since=self.time(s.power_since, now),
                    duration=self.duration(now - s.power_since),
                )
            )
        elif s.offline_notified and s.unreachable_since:
            lines.append(t("status_unknown_offline", since=self.time(s.unreachable_since, now)))
        elif s.baseline_pending:
            lines.append(t("status_unknown_baseline"))
        else:
            lines.append(t("status_unknown"))

        if r is not None:
            if r.soc is not None:
                lines.append(t("status_soc", soc=_num(r.soc)))
            if r.dsg_minutes and effective is GridState.BATTERY:
                lines.append(
                    t("status_runtime", runtime=self.duration(timedelta(minutes=r.dsg_minutes)))
                )
            lines.append(t("status_ac_load", value=_num(r.ac_load_w)))
            if r.dc_load_w:
                lines.append(t("status_dc_load", value=_num(r.dc_load_w)))
            lines.append(t("status_grid_in", value=_num(r.grid_w)))
            if r.ac_on is False:
                lines.append(t("status_ac_off_warning"))
            if r.ac_eco:
                lines.append(t("status_ac_eco_warning"))
        online = t("unknown") if v.online is None else t("yes" if v.online else "no")
        lines.append(t("status_online", value=online))
        if v.last_poll is not None:
            lines.append(t("status_last_poll", time=self.time(v.last_poll, now)))
        else:
            lines.append(t("status_never_polled"))
        return "\n".join(lines)

    # /history

    def history(self, outages: Sequence[Outage], now: datetime) -> str:
        t = self.t
        if not outages:
            return t("history_empty")
        lines = [t("history_title")]
        for o in outages:
            approx = t("history_approx") if o.approximate else ""
            prefix = self._prefix(o.sn)
            if o.end is None:
                item = t(
                    "history_item_open",
                    start=self.time(o.start),
                    duration=approx + self.duration(now - o.start),
                )
            else:
                item = t(
                    "history_item",
                    start=self.time(o.start),
                    end=self.time(o.end, o.start),
                    duration=approx + self.duration(o.end - o.start),
                )
            lines.append(item.replace("• ", "• " + prefix, 1))
        return "\n".join(lines)
