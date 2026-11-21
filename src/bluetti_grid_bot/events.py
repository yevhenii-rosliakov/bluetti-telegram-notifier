"""Events produced by the detector. The notifier turns them into messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class GridLost:
    sn: str
    at: datetime
    # Set when the station was offline while it happened: the outage began
    # somewhere between ``earliest`` and ``at``.
    earliest: datetime | None
    soc: float | None
    load_w: float | None
    runtime_min: int | None = None


@dataclass(frozen=True, slots=True)
class GridRestored:
    sn: str
    at: datetime
    earliest: datetime | None
    outage_start: datetime | None
    outage_start_earliest: datetime | None
    soc: float | None

    @property
    def duration(self) -> timedelta | None:
        return None if self.outage_start is None else self.at - self.outage_start

    @property
    def approximate(self) -> bool:
        return self.earliest is not None or self.outage_start_earliest is not None


@dataclass(frozen=True, slots=True)
class BotStartedOnBattery:
    sn: str
    since: datetime
    # False: the outage started while the bot was not running; ``since`` is "at least".
    since_known: bool
    soc: float | None
    load_w: float | None


@dataclass(frozen=True, slots=True)
class BotStartedOnGrid:
    """First poll after startup found grid present. Sent only if NOTIFY_STARTUP is on."""

    sn: str
    soc: float | None


@dataclass(frozen=True, slots=True)
class OutageEndedUnobserved:
    """An outage recorded before a restart was over by the time the bot came back.

    Not sent to Telegram (the first poll after startup is silent); only closes history.
    """

    sn: str
    at: datetime


@dataclass(frozen=True, slots=True)
class LowBattery:
    sn: str
    threshold: int
    soc: float
    runtime_min: int | None


@dataclass(frozen=True, slots=True)
class BatteryFull:
    """The battery reached the configured level after grid power came back."""

    sn: str
    soc: float
    # When the grid came back; charging took ``at - charging_since``.
    charging_since: datetime
    at: datetime


@dataclass(frozen=True, slots=True)
class AcOutputOff:
    sn: str
    at: datetime


@dataclass(frozen=True, slots=True)
class StationOffline:
    sn: str
    since: datetime
    last_seen: datetime | None


@dataclass(frozen=True, slots=True)
class StationOnline:
    sn: str
    at: datetime
    offline_since: datetime


Event = (
    GridLost
    | GridRestored
    | BotStartedOnBattery
    | BotStartedOnGrid
    | OutageEndedUnobserved
    | LowBattery
    | BatteryFull
    | AcOutputOff
    | StationOffline
    | StationOnline
)
