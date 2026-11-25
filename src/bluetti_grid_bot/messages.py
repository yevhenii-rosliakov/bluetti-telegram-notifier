"""User-facing text: translations and formatting of events, commands and reports.

Pure functions of their inputs (``now`` is passed in), so they are easy to test.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import cache
from importlib import resources
from zoneinfo import ZoneInfo

from . import stats
from .config import Site
from .detector import DeviceState
from .events import (
    AcOutputOff,
    BatteryFull,
    BotStartedOnBattery,
    BotStartedOnGrid,
    Event,
    GridLost,
    GridRestored,
    HighLoad,
    LowBattery,
    OutageEndedUnobserved,
    StationOffline,
    StationOnline,
)
from .models import GridState, Reading, Station
from .schedule import KYIV, Schedule, Slot
from .storage import Outage

BAR_WIDTH = 10
# Before a planned outage, mention that the battery is not charged enough below this.
LOW_SOC_BEFORE_PLANNED = 50
# /stats lists at most this many days one by one.
STATS_DAYS_LISTED = 14


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


def _bar(fraction: float) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


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
    # Outages overlapping the statistics window; None = do not show statistics.
    recent_outages: Sequence[Outage] | None = None


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

    def slot(self, slot: Slot) -> str:
        start, end = slot.start.astimezone(self.tz), slot.end.astimezone(self.tz)
        until = "24:00" if end.time() == time() and end.date() > start.date() else f"{end:%H:%M}"
        return f"{start:%H:%M}–{until}"

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
                lines = [head, *self._details(ev.soc, ev.load_w)]
                if ev.runtime_min:
                    runtime = self.duration(timedelta(minutes=ev.runtime_min))
                    lines.append(t("detail_runtime", runtime=runtime))
                return "\n".join(lines)
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
            case HighLoad():
                lines = [t("high_load", prefix=p, load=_num(ev.load_w)), *self._details(ev.soc)]
                if ev.runtime_min:
                    runtime = self.duration(timedelta(minutes=ev.runtime_min))
                    lines.append(t("detail_runtime", runtime=runtime))
                return "\n".join(lines)
            case BatteryFull():
                return t(
                    "battery_full",
                    prefix=p,
                    soc=_num(ev.soc),
                    duration=self.duration(ev.at - ev.charging_since),
                )
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

    # login

    def auth_expired(self, site: Site) -> str:
        if not site.id:
            return self.t("auth_expired")
        return self.t(
            "auth_expired_station", name=site.label, site=site.id, var=site.var("REFRESH_TOKEN")
        )

    def auth_restored(self, site: Site) -> str:
        if not site.id:
            return self.t("auth_restored")
        return self.t("auth_restored_station", name=site.label)

    # /status

    def status(
        self,
        views: Sequence[StationView],
        now: datetime,
        auth_expired: bool,
        stats_days: int = 7,
        schedules: Sequence[tuple[str, Schedule]] = (),
        expired_sites: Sequence[Site] = (),
    ) -> str:
        """``auth_expired``: the single-account login; ``expired_sites``: BLUETTI_STATIONS
        entries whose login is invalid; ``schedules``: (label, schedule) pairs, see
        ``labelled``."""
        t = self.t
        blocks = []
        if auth_expired:
            blocks.append(t("status_auth_expired"))
        for site in expired_sites:
            blocks.append(t("status_auth_expired_station", name=site.label, site=site.id))
        for label, schedule in schedules:
            blocks.append(self.labelled(label, self._schedule_status(schedule, now)))
        if not views:
            blocks.append(t("status_no_stations"))
        for v in views:
            blocks.append(self._station_status(v, now))
            if v.recent_outages is not None:
                blocks.append(self._stats(v.recent_outages, now, stats_days))
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
            if r.runtime_min and effective is GridState.BATTERY:
                lines.append(
                    t("status_runtime", runtime=self.duration(timedelta(minutes=r.runtime_min)))
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

    def _stats(self, outages: Sequence[Outage], now: datetime, days: int) -> str:
        summary = stats.summarize(outages, now, days)
        return "\n".join([self.t("stats_title", days=days), *self._summary_lines(summary)])

    def _summary_lines(self, s: stats.Summary) -> list[str]:
        t = self.t
        if s.count == 0:
            return [t("stats_none")]
        approx = t("history_approx") if s.approximate else ""
        return [
            t("stats_count", count=s.count),
            t(
                "stats_total",
                duration=approx + self.duration(s.total),
                share=f"{s.share_with_power:.0f}",
            ),
            t("stats_longest", duration=approx + self.duration(s.longest)),
        ]

    # /stats

    def stats_report(
        self,
        stations: Sequence[Station],
        outages: Sequence[Outage],
        now: datetime,
        days: int,
    ) -> str:
        if not stations:
            return self.t("status_no_stations")
        blocks = [self.t("stats_title", days=days)]
        for st in stations:
            own = [o for o in outages if o.sn == st.sn]
            blocks.append(self._station_report(st, own, now, days, len(stations) > 1))
        return "\n\n".join(blocks)

    def _station_report(
        self, st: Station, outages: Sequence[Outage], now: datetime, days: int, titled: bool
    ) -> str:
        t = self.t
        lines = [t("status_title", name=st.name)] if titled else []
        lines += self._summary_lines(stats.summarize(outages, now, days))
        if typical := stats.typical_duration(outages):
            lines.append(t("stats_typical", duration=self.duration(typical)))
        if not outages:
            return "\n".join(lines)

        listed = min(days, STATS_DAYS_LISTED)
        lines += ["", t("stats_by_day", days=listed)]
        for day, total in stats.by_day(outages, now, listed, self.tz):
            lines.append(f"{day:%d.%m} {_bar(total / timedelta(days=1))} {self._day_total(total)}")

        lines += ["", t("stats_by_hour")]
        hours = stats.by_hour(outages, now, days, self.tz)
        for start in range(0, 24, 2):
            share = (hours[start] + hours[start + 1]) / 2
            lines.append(f"{start:02d}–{start + 2:02d} {_bar(share)} {share * 100:.0f}%")
        return "\n".join(lines)

    def _day_total(self, total: timedelta) -> str:
        return "—" if total < timedelta(minutes=1) else self.duration(total)

    # weekly summary

    def weekly(self, stations: Sequence[Station], outages: Sequence[Outage], now: datetime) -> str:
        t = self.t
        start = now - timedelta(days=7)
        lines = [
            t(
                "weekly_title",
                start=f"{start.astimezone(self.tz):%d.%m}",
                end=f"{now.astimezone(self.tz):%d.%m}",
            )
        ]
        for st in stations:
            own = [o for o in outages if o.sn == st.sn]
            week = stats.summarize(own, now, 7)
            before = stats.summarize(own, start, 7)
            if len(stations) > 1:
                lines += ["", t("status_title", name=st.name)]
            lines += self._summary_lines(week)
            lines.append(
                t("weekly_previous", count=before.count, duration=self.duration(before.total))
            )
        return "\n".join(lines)

    # schedule

    def labelled(self, label: str, text: str) -> str:
        """Prefix a schedule text with the stations it is for (when they differ)."""
        return f"{self.t('status_title', name=label)}\n{text}" if label else text

    def _schedule_status(self, schedule: Schedule, now: datetime) -> str:
        t = self.t
        if (slot := schedule.current(now)) is not None:
            text = t("schedule_now", slot=self.slot(slot), group=schedule.group)
        elif (slot := schedule.upcoming(now)) is not None:
            text = t("schedule_next", time=self._slot_time(slot, now), group=schedule.group)
        elif schedule.covers(now):
            text = t("schedule_none", group=schedule.group)
        else:
            text = t("schedule_unknown", group=schedule.group)
        if schedule.notice:
            text += "\n" + t("schedule_notice", text=schedule.notice)
        return text

    def _slot_time(self, slot: Slot, now: datetime) -> str:
        day = slot.start.astimezone(self.tz).date()
        prefix = "" if day == now.astimezone(self.tz).date() else f"{day:%d.%m} "
        return prefix + self.slot(slot)

    def schedule(self, schedule: Schedule | None, now: datetime) -> str:
        t = self.t
        if schedule is None:
            return t("schedule_unavailable")
        lines = [t("schedule_title", group=schedule.group)]
        today = now.astimezone(KYIV).date()
        for day in schedule.days:
            if day >= today:
                lines += ["", self._schedule_day(schedule, day, today)]
        if not any(d >= today for d in schedule.days):
            lines += ["", t("schedule_not_published")]
        if schedule.notice:
            lines += ["", t("schedule_notice", text=schedule.notice)]
        return "\n".join(lines)

    def _schedule_day(self, schedule: Schedule, day: date, today: date) -> str:
        t = self.t
        key = "schedule_today" if day == today else "schedule_tomorrow"
        lines = [t(key, date=f"{day:%d.%m}")]
        slots = schedule.for_day(day)
        for s in slots:
            lines.append(
                t("schedule_slot_possible" if s.possible else "schedule_slot", slot=self.slot(s))
            )
        if not slots:
            lines.append(t("schedule_day_clear"))
        else:
            off = sum((s.end - s.start for s in slots if not s.possible), timedelta())
            lines.append(t("schedule_day_total", duration=self.duration(off)))
        return "\n".join(lines)

    def schedule_published(
        self, schedule: Schedule, day: date, changed: bool, now: datetime
    ) -> str:
        today = now.astimezone(KYIV).date()
        head = self.t("schedule_changed" if changed else "schedule_published")
        return "\n".join([head, self._schedule_day(schedule, day, today)])

    def schedule_warning(
        self, slot: Slot, group: str, views: Sequence[StationView], now: datetime
    ) -> str:
        t = self.t
        lines = [
            t(
                "schedule_warning",
                slot=self.slot(slot),
                minutes=max(1, round((slot.start - now).total_seconds() / 60)),
                group=group,
            )
        ]
        for v in views:
            r = v.reading
            if r is None or r.soc is None:
                continue
            p = f"{v.station.name}: " if len(views) > 1 else ""
            lines.append(p + t("detail_soc", soc=_num(r.soc)))
            if r.soc < LOW_SOC_BEFORE_PLANNED:
                lines.append(t("schedule_warning_low"))
        lines.append(t("schedule_warning_reminder"))
        return "\n".join(lines)

    def schedule_match(self, schedule: Schedule, at: datetime, slack: timedelta) -> str | None:
        """A line for "power went out": was it on the schedule?"""
        if not schedule.covers(at):
            return None
        if (slot := schedule.current(at, slack)) is not None:
            return self.t("schedule_planned", slot=self.slot(slot))
        return self.t("schedule_unplanned")

    # /history
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
