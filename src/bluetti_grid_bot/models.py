"""Plain data types shared by the cloud client, the detector and the notifier."""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass, field


class GridState(enum.StrEnum):
    UNKNOWN = "unknown"
    GRID = "grid"
    BATTERY = "battery"


class DetectionMode(enum.StrEnum):
    ATS = "ats"
    PASSTHROUGH = "passthrough"
    GRID_INPUT = "grid_input"


# fnCode -> Reading attribute, grouped by how the value is parsed.
_FLOAT_FIELDS = {
    "GridAllTotalPower": "grid_w",
    "ACLoadAllTotalPower": "ac_load_w",
    "DCLoadAllTotalPower": "dc_load_w",
    "PVAllTotalPower": "pv_w",
    "SOC": "soc",
}
_BOOL_FIELDS = {
    "SetCtrlAc": "ac_on",
    "SetCtrlDc": "dc_on",
    "SetACECO": "ac_eco",
    "SetDCECO": "dc_eco",
}
_INT_FIELDS = {
    "DsgFullTime": "dsg_minutes",
    "ChgFullTime": "chg_minutes",
}


@dataclass(frozen=True, slots=True)
class Reading:
    """The fields of a ``deviceStates`` answer the bot understands. ``None`` = unknown."""

    grid_w: float | None = None
    ac_load_w: float | None = None
    dc_load_w: float | None = None
    pv_w: float | None = None
    soc: float | None = None
    ac_on: bool | None = None
    dc_on: bool | None = None
    ac_eco: bool | None = None
    dc_eco: bool | None = None
    dsg_minutes: int | None = None
    chg_minutes: int | None = None
    work_mode: str | None = None


def _to_float(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_reading(raw: Mapping[str, str]) -> tuple[Reading, tuple[str, ...]]:
    """Parse the raw ``fnCode -> fnValue`` map.

    Returns the reading and the fnCodes that were present but could not be parsed.
    Missing fields are simply unknown; nothing here raises.
    """
    values: dict[str, object] = {}
    invalid: list[str] = []
    for code, attr in _FLOAT_FIELDS.items():
        if (value := raw.get(code, "").strip()) != "":
            if (number := _to_float(value)) is None:
                invalid.append(code)
            else:
                values[attr] = number
    for code, attr in _BOOL_FIELDS.items():
        if (value := raw.get(code, "").strip()) != "":
            if value in ("0", "1"):
                values[attr] = value == "1"
            else:
                invalid.append(code)
    for code, attr in _INT_FIELDS.items():
        if (value := raw.get(code, "").strip()) != "":
            if (number := _to_float(value)) is None:
                invalid.append(code)
            else:
                values[attr] = int(number)
    if work_mode := raw.get("SetCtrlWorkMode", "").strip():
        values["work_mode"] = work_mode
    return Reading(**values), tuple(invalid)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Sample:
    """One station's answer from a single poll."""

    sn: str
    online: bool
    raw: Mapping[str, str] = field(default_factory=dict)
    reading: Reading = field(default_factory=Reading)
    invalid_fields: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, sn: str, online: bool, raw: Mapping[str, str]) -> Sample:
        reading, invalid = parse_reading(raw)
        return cls(sn=sn, online=online, raw=dict(raw), reading=reading, invalid_fields=invalid)

    @property
    def has_data(self) -> bool:
        return self.online and bool(self.raw)


@dataclass(frozen=True, slots=True)
class Station:
    sn: str
    model: str
    name: str
