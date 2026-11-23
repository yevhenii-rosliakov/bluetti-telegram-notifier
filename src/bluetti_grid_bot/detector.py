"""Outage / alert state machine.

Pure: ``evaluate(previous state, sample, config, now) -> (new state, events)``. No I/O,
no clock, no logging, so every transition can be unit-tested.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

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
from .models import DetectionMode, GridState, Reading, Sample

# The cloud sometimes answers with a broken snapshot (SOC 0 %, AC switch off, every power
# 0, a different working mode) and the real values again on the next poll. An answer that
# looks like that is skipped until it repeats this many polls in a row.
SUSPECT_CONFIRM_POLLS = 3


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    mode: DetectionMode = DetectionMode.ATS
    load_threshold_w: float = 5.0
    grid_threshold_w: float = 5.0
    soc_drop_pct: float = 2.0
    debounce_polls: int = 2
    restore_debounce_polls: int = 2
    offline_grace: timedelta = timedelta(seconds=600)
    low_soc_thresholds: tuple[int, ...] = (50, 25)
    alert_ac_off: bool = True
    # Alert when the load on battery stays at or above this many watts; 0 disables.
    high_load_w: float = 1000.0
    # Report when the battery reaches this SOC after grid power came back; 0 disables.
    full_charge_soc: int = 100


@dataclass(slots=True)
class DeviceState:
    """Everything the detector remembers about one station. Persisted between restarts."""

    sn: str
    # Set on every process start; the next poll with data becomes the silent baseline.
    baseline_pending: bool = True
    # Last confirmed grid state (survives offline periods; UNKNOWN only before a baseline).
    power: GridState = GridState.UNKNOWN
    power_since: datetime | None = None
    # When the start of the current state is only known to be within a window.
    power_since_earliest: datetime | None = None
    # Debounce of a pending transition.
    candidate: GridState | None = None
    candidate_count: int = 0
    candidate_since: datetime | None = None
    candidate_earliest: datetime | None = None
    # Connectivity.
    last_seen_at: datetime | None = None
    unreachable_since: datetime | None = None
    offline_notified: bool = False
    # Last time seen before an offline period that was reported; a state change found
    # right after it happened somewhere inside that gap.
    gap_start: datetime | None = None
    # Alerts.
    low_soc_fired: tuple[int, ...] = ()
    ac_off_count: int = 0
    ac_off_alerted: bool = False
    high_load_count: int = 0
    high_load_alerted: bool = False
    # Grid came back with the battery below ``full_charge_soc``: report when it is full.
    charging_since: datetime | None = None
    # passthrough mode: SOC at the last poll that showed grid input.
    soc_at_grid: float | None = None
    # Implausible answers seen in a row (see ``implausible``).
    suspect_count: int = 0

    @property
    def effective(self) -> GridState:
        """What the bot can honestly claim right now."""
        if self.baseline_pending or self.offline_notified:
            return GridState.UNKNOWN
        return self.power

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, GridState):
                value = value.value
            elif isinstance(value, tuple):
                value = list(value)
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceState:
        known = {f.name: f for f in dataclasses.fields(cls)}
        kwargs: dict[str, Any] = {}
        for name, value in data.items():
            if name not in known or value is None:
                continue
            if name in ("power", "candidate"):
                value = GridState(value)
            elif name in (
                "power_since",
                "power_since_earliest",
                "candidate_since",
                "candidate_earliest",
                "last_seen_at",
                "unreachable_since",
                "gap_start",
                "charging_since",
            ):
                value = datetime.fromisoformat(value)
            elif name == "low_soc_fired":
                value = tuple(int(v) for v in value)
            kwargs[name] = value
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class Classification:
    state: GridState | None  # None = this sample says nothing either way
    reason: str


@dataclass(frozen=True, slots=True)
class Result:
    state: DeviceState
    events: list[Event] = field(default_factory=list)
    observed: Classification | None = None
    # Why this poll was skipped as implausible; nothing else changed then.
    suspect: str | None = None


def _fmt(value: float) -> str:
    return f"{value:g}"


def implausible(reading: Reading) -> str | None:
    """Why this reading looks like a broken cloud answer, or ``None`` if it looks real.

    A station that really is at 0 % with nothing coming in has switched itself off and stops
    answering, so "online, 0 %, every power 0" is the cloud's placeholder, not a reading.
    """
    powers = (reading.grid_w, reading.ac_load_w, reading.dc_load_w, reading.pv_w)
    if reading.soc == 0 and not any(powers) and not reading.dsg_minutes:
        return "SOC 0 % with every power reading 0"
    return None


def classify(reading: Reading, cfg: DetectorConfig, soc_at_grid: float | None) -> Classification:
    """Decide from one reading whether the station is on grid or on battery."""
    grid_thr, load_thr = cfg.grid_threshold_w, cfg.load_threshold_w
    grid = reading.grid_w

    if cfg.mode is DetectionMode.GRID_INPUT:
        if grid is None:
            return Classification(None, "GridAllTotalPower unknown")
        if grid >= grid_thr:
            return Classification(GridState.GRID, f"grid input {_fmt(grid)} W >= {_fmt(grid_thr)}")
        return Classification(GridState.BATTERY, f"grid input {_fmt(grid)} W < {_fmt(grid_thr)}")

    if grid is not None and grid >= grid_thr:
        return Classification(GridState.GRID, f"grid input {_fmt(grid)} W >= {_fmt(grid_thr)}")

    if cfg.mode is DetectionMode.ATS:
        load = reading.ac_load_w
        if load is None:
            return Classification(None, "ACLoadAllTotalPower unknown")
        if load < load_thr:
            return Classification(
                GridState.GRID, f"AC load {_fmt(load)} W < {_fmt(load_thr)}: house runs from grid"
            )
        if grid is None:
            return Classification(None, "GridAllTotalPower unknown")
        return Classification(
            GridState.BATTERY,
            f"AC load {_fmt(load)} W >= {_fmt(load_thr)} and grid input {_fmt(grid)} W "
            f"< {_fmt(grid_thr)}",
        )

    # passthrough
    if grid is None:
        return Classification(None, "GridAllTotalPower unknown")
    if reading.ac_load_w is not None or reading.dc_load_w is not None:
        load = (reading.ac_load_w or 0.0) + (reading.dc_load_w or 0.0)
        if load >= load_thr:
            return Classification(
                GridState.BATTERY,
                f"grid input {_fmt(grid)} W < {_fmt(grid_thr)} and load {_fmt(load)} W "
                f">= {_fmt(load_thr)}",
            )
    if soc_at_grid is not None and reading.soc is not None:
        drop = soc_at_grid - reading.soc
        if drop >= cfg.soc_drop_pct:
            return Classification(
                GridState.BATTERY,
                f"SOC dropped {_fmt(drop)} % since grid was last seen "
                f"(>= {_fmt(cfg.soc_drop_pct)})",
            )
    return Classification(None, "no grid input and no load: cannot tell")


def _reset_candidate(s: DeviceState) -> None:
    s.candidate = None
    s.candidate_count = 0
    s.candidate_since = None
    s.candidate_earliest = None


def _load(reading: Reading) -> float | None:
    if reading.ac_load_w is None and reading.dc_load_w is None:
        return None
    return (reading.ac_load_w or 0.0) + (reading.dc_load_w or 0.0)


def _baseline(
    s: DeviceState,
    observed: GridState,
    reading: Reading,
    cfg: DetectorConfig,
    now: datetime,
    prev_seen: datetime | None,
) -> list[Event]:
    events: list[Event] = []
    persisted = s.power
    if observed is GridState.BATTERY:
        if persisted is GridState.BATTERY and s.power_since is not None:
            # Same outage as before the restart: keep its start and alerts already sent.
            events.append(
                BotStartedOnBattery(s.sn, s.power_since, True, reading.soc, _load(reading))
            )
        else:
            s.power_since = now
            s.power_since_earliest = prev_seen if persisted is GridState.GRID else None
            s.low_soc_fired = ()
            s.high_load_alerted = False
            events.append(BotStartedOnBattery(s.sn, now, False, reading.soc, _load(reading)))
    else:
        if persisted is GridState.BATTERY:
            events.append(OutageEndedUnobserved(s.sn, now))
            _arm_full_charge(s, reading, cfg, now)
        if persisted is not GridState.GRID or s.power_since is None:
            s.power_since = now
            s.power_since_earliest = None
        s.low_soc_fired = ()
        s.high_load_alerted = False
        events.append(BotStartedOnGrid(s.sn, reading.soc))
    s.power = observed
    s.baseline_pending = False
    s.gap_start = None
    _reset_candidate(s)
    return events


def _debounce(
    s: DeviceState, observed: GridState, reading: Reading, cfg: DetectorConfig, now: datetime
) -> list[Event]:
    if observed is s.power:
        _reset_candidate(s)
        s.gap_start = None
        return []
    if s.candidate is observed:
        s.candidate_count += 1
    else:
        s.candidate = observed
        s.candidate_count = 1
        s.candidate_since = now
        s.candidate_earliest = s.gap_start
    s.gap_start = None

    needed = cfg.debounce_polls if observed is GridState.BATTERY else cfg.restore_debounce_polls
    if s.candidate_count < needed:
        return []

    at = s.candidate_since or now
    earliest = s.candidate_earliest
    events: list[Event] = []
    if observed is GridState.BATTERY:
        events.append(
            GridLost(s.sn, at, earliest, reading.soc, _load(reading), reading.dsg_minutes)
        )
    elif s.power is GridState.BATTERY:
        events.append(
            GridRestored(s.sn, at, earliest, s.power_since, s.power_since_earliest, reading.soc)
        )
        _arm_full_charge(s, reading, cfg, at)
    s.power = observed
    s.power_since = at
    s.power_since_earliest = earliest
    s.low_soc_fired = ()
    s.high_load_alerted = False
    _reset_candidate(s)
    return events


def _arm_full_charge(s: DeviceState, reading: Reading, cfg: DetectorConfig, at: datetime) -> None:
    if cfg.full_charge_soc and (reading.soc is None or reading.soc < cfg.full_charge_soc):
        s.charging_since = at


def _full_charge(
    s: DeviceState, reading: Reading, cfg: DetectorConfig, now: datetime
) -> list[Event]:
    if s.power is not GridState.GRID:
        s.charging_since = None
        return []
    if s.charging_since is None or reading.soc is None or reading.soc < cfg.full_charge_soc:
        return []
    since, s.charging_since = s.charging_since, None
    return [BatteryFull(s.sn, reading.soc, since, now)]


def _low_soc(s: DeviceState, reading: Reading, cfg: DetectorConfig) -> list[Event]:
    if s.power is not GridState.BATTERY or reading.soc is None:
        return []
    soc = reading.soc
    crossed = [t for t in cfg.low_soc_thresholds if soc <= t and t not in s.low_soc_fired]
    if not crossed:
        return []
    s.low_soc_fired = tuple(sorted({*s.low_soc_fired, *crossed}, reverse=True))
    # Several thresholds crossed at once (e.g. bot started at 12 %): one message, the lowest.
    return [LowBattery(s.sn, min(crossed), soc, reading.dsg_minutes)]


def _high_load(s: DeviceState, reading: Reading, cfg: DetectorConfig) -> list[Event]:
    load = _load(reading)
    if not cfg.high_load_w or s.power is not GridState.BATTERY or load is None:
        s.high_load_count = 0
        return []
    if load < cfg.high_load_w:
        s.high_load_count = 0
        return []
    s.high_load_count += 1
    # Once per outage: a heater's thermostat switching it on and off must not spam.
    if s.high_load_alerted or s.high_load_count < cfg.debounce_polls:
        return []
    s.high_load_alerted = True
    return [HighLoad(s.sn, load, reading.soc, reading.dsg_minutes)]


def _ac_off(s: DeviceState, reading: Reading, cfg: DetectorConfig, now: datetime) -> list[Event]:
    if reading.ac_on is None:
        return []
    if reading.ac_on:
        s.ac_off_count = 0
        s.ac_off_alerted = False
        return []
    s.ac_off_count += 1
    if cfg.alert_ac_off and not s.ac_off_alerted and s.ac_off_count >= cfg.debounce_polls:
        s.ac_off_alerted = True
        return [AcOutputOff(s.sn, now)]
    return []


def evaluate(
    prev: DeviceState, sample: Sample | None, cfg: DetectorConfig, now: datetime
) -> Result:
    """Advance the state machine by one poll.

    ``sample`` is ``None`` when the poll failed. A sample with ``online == False`` or an
    empty ``stateList`` counts as "no data" too: the state becomes UNKNOWN, never
    "grid lost" or "grid restored". An implausible sample (see ``implausible``) is skipped
    until it repeats ``SUSPECT_CONFIRM_POLLS`` times in a row.
    """
    s = dataclasses.replace(prev)
    events: list[Event] = []

    if sample is None or not sample.has_data:
        _reset_candidate(s)
        if s.unreachable_since is None:
            s.unreachable_since = now
        if not s.offline_notified and now - s.unreachable_since >= cfg.offline_grace:
            s.offline_notified = True
            events.append(StationOffline(s.sn, s.unreachable_since, s.last_seen_at))
        return Result(s, events)

    reading = sample.reading
    if (reason := implausible(reading)) is None:
        s.suspect_count = 0
    else:
        s.suspect_count += 1
        if s.suspect_count < SUSPECT_CONFIRM_POLLS:
            return Result(s, suspect=reason)

    prev_seen = s.last_seen_at
    if s.unreachable_since is not None:
        if s.offline_notified:
            events.append(StationOnline(s.sn, now, s.unreachable_since))
            s.gap_start = prev_seen
        s.unreachable_since = None
        s.offline_notified = False
    s.last_seen_at = now

    observed = classify(reading, cfg, s.soc_at_grid)
    if observed.state is GridState.GRID and reading.soc is not None:
        s.soc_at_grid = reading.soc

    if observed.state is not None:
        if s.baseline_pending:
            events += _baseline(s, observed.state, reading, cfg, now, prev_seen)
        else:
            events += _debounce(s, observed.state, reading, cfg, now)

    if not s.baseline_pending:
        events += _low_soc(s, reading, cfg)
        events += _high_load(s, reading, cfg)
        events += _full_charge(s, reading, cfg, now)
    events += _ac_off(s, reading, cfg, now)
    return Result(s, events, observed)
