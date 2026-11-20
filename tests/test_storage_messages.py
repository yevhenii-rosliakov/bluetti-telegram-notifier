from __future__ import annotations

import os
import stat
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bluetti_grid_bot.detector import DeviceState
from bluetti_grid_bot.events import (
    AcOutputOff,
    BotStartedOnBattery,
    BotStartedOnGrid,
    GridLost,
    GridRestored,
    LowBattery,
    OutageEndedUnobserved,
    StationOffline,
    StationOnline,
)
from bluetti_grid_bot.messages import Formatter, StationView, Translator
from bluetti_grid_bot.models import GridState, Reading, Station, parse_reading
from bluetti_grid_bot.storage import Storage, TokenSet

from .conftest import SN, at, fixture_json

KYIV = ZoneInfo("Europe/Kyiv")  # UTC+2 in November


# storage


def test_db_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "db.sqlite3"
    Storage(path).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_tokens_and_kv(storage: Storage) -> None:
    assert storage.get_tokens() is None
    storage.save_tokens(TokenSet("a", "r", 1.5, 1.0))
    assert storage.get_tokens() == TokenSet("a", "r", 1.5, 1.0)
    storage.set_gateway("https://gw")
    assert storage.get_gateway() == "https://gw"
    storage.set_gateway(None)
    assert storage.get_gateway() is None


def test_device_state_keeps_last_raw(storage: Storage) -> None:
    state = DeviceState(SN, power=GridState.GRID, power_since=at(0))
    storage.save_device(state, {"SOC": "90"}, at(0))
    storage.save_device(DeviceState(SN, power=GridState.BATTERY), None, None)
    loaded, raw, polled = storage.load_device(SN)
    assert loaded is not None
    assert loaded.power is GridState.BATTERY
    assert raw == {"SOC": "90"}
    assert polled == at(0)
    assert storage.load_device("other") == (None, {}, None)


def test_outage_history(storage: Storage) -> None:
    storage.start_outage(SN, at(0), None)
    storage.start_outage(SN, at(5), None)  # already open: ignored
    assert storage.open_outage(SN) is not None
    storage.end_outage(SN, at(30), None)
    storage.start_outage(SN, at(60), at(50))
    storage.start_outage("OTHER", at(70), None)
    items = storage.history(10, [SN])
    assert [(o.start, o.end, o.approximate) for o in items] == [
        (at(60), None, True),
        (at(0), at(30), False),
    ]
    assert len(storage.history(1)) == 1
    assert storage.history(10)[0].sn == "OTHER"


def test_writes_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    st = Storage(path)
    st.save_tokens(TokenSet("a", "r", None, 0))
    st.close()
    st2 = Storage(path)
    assert st2.get_tokens() is not None
    st2.close()


# translations


def test_locales_have_the_same_keys() -> None:
    from bluetti_grid_bot.messages import _load

    assert set(_load("uk")) == set(_load("en"))


def test_translator_falls_back_to_key() -> None:
    assert Translator("en")("no_such_key") == "no_such_key"


# formatting


def fmt(lang: str = "en", names: dict[str, str] | None = None) -> Formatter:
    return Formatter(Translator(lang), KYIV, names or {SN: "Home"})


def test_duration_formatting() -> None:
    uk, en = fmt("uk"), fmt("en")
    assert uk.duration(timedelta(hours=1, minutes=33)) == "1 год 33 хв"
    assert en.duration(timedelta(hours=1, minutes=33)) == "1 h 33 min"
    assert en.duration(timedelta(seconds=20)) == "less than a minute"
    assert en.duration(timedelta(days=2, hours=3, minutes=4)) == "2 d 3 h"
    assert en.duration(timedelta(minutes=5)) == "5 min"


def test_time_uses_timezone_and_date_when_not_today() -> None:
    f = fmt()
    assert f.time(at(0), at(10)) == "14:00"  # 12:00 UTC -> 14:00 Kyiv
    assert f.time(at(0), at(60 * 24)) == "20.11 14:00"


def test_grid_lost_message_uk() -> None:
    text = fmt("uk").event(GridLost(SN, at(32), None, 87.0, 120.0), at(33))
    assert text == "⚡ Світло зникло о 14:32\nЗаряд: 87%\nНавантаження: 120 Вт"


def test_grid_restored_message_uk() -> None:
    ev = GridRestored(SN, at(125), None, at(32), None, 64.0)
    assert fmt("uk").event(ev, at(126)) == (
        "✅ Світло з'явилося о 16:05, не було 1 год 33 хв\nЗаряд: 64%"
    )


def test_window_messages_when_offline() -> None:
    f = fmt()
    lost = f.event(GridLost(SN, at(70), at(30), None, None), at(70))
    assert lost is not None
    assert "between 14:30 and 15:10" in lost
    restored = f.event(GridRestored(SN, at(120), None, at(70), at(30), None), at(120))
    assert restored is not None
    assert "about 50 min" in restored


def test_prefix_only_with_several_stations() -> None:
    single = fmt().event(AcOutputOff(SN, at(0)), at(0))
    assert single is not None
    assert not single.startswith("⚠️ [")
    multi = fmt(names={SN: "Mom's flat", "B": "Garage"}).event(AcOutputOff(SN, at(0)), at(0))
    assert multi is not None
    assert multi.startswith("⚠️ [Mom's flat] ")


def test_other_event_messages() -> None:
    f = fmt()
    now = at(100)
    assert "since 14:00" in (f.event(BotStartedOnBattery(SN, at(0), True, 50, 10), now) or "")
    assert "at the latest" in (
        f.event(BotStartedOnBattery(SN, at(0), False, None, None), now) or ""
    )
    assert "grid power is present" in (f.event(BotStartedOnGrid(SN, 100), now) or "")
    low = f.event(LowBattery(SN, 15, 14.6, 90), now) or ""
    assert "Low battery: 14.6% (threshold 15%)" in low
    assert "1 h 30 min" in low
    off = f.event(StationOffline(SN, at(10), at(9)), now) or ""
    assert "No connection" in off
    assert "outage" in off  # honest about what it often means
    assert "14:09" in off
    assert "lost for 1 h 30 min" in (f.event(StationOnline(SN, at(100), at(10)), now) or "")
    assert f.event(OutageEndedUnobserved(SN, now), now) is None


def view(state: DeviceState, reading: Reading | None, online: bool | None = True) -> StationView:
    return StationView(Station(SN, "PR200V2", "Home"), state, reading, online, at(90))


def test_status_on_grid_with_fixture() -> None:
    raw = {
        s["fnCode"]: s["fnValue"]
        for s in fixture_json("deviceStates.sample.json")["data"][0]["stateList"]
    }
    reading, _ = parse_reading(raw)
    state = DeviceState(SN, baseline_pending=False, power=GridState.GRID, power_since=at(0))
    text = fmt().status([view(state, reading)], at(91), auth_expired=False)
    assert "Grid: present ✅ since 14:00" in text
    assert "Battery: 100%" in text
    assert "Load on station outlets: 0 W" in text
    assert "Solar" not in text
    assert "AC output" not in text
    assert "DC output" not in text
    assert "Station online: yes" in text
    assert "Last successful poll: 15:30" in text
    assert "Runtime" not in text  # only shown on battery
    assert "ECO" not in text


def test_status_on_battery_with_eco_warning() -> None:
    reading = Reading(grid_w=0, ac_load_w=55, soc=70, ac_on=True, ac_eco=True, dsg_minutes=200)
    state = DeviceState(SN, baseline_pending=False, power=GridState.BATTERY, power_since=at(30))
    text = fmt("uk").status([view(state, reading)], at(90), auth_expired=True)
    assert "Світло: немає ⚡ з 14:30 (1 год)" in text
    assert "До розряду: ≈ 3 год 20 хв" in text
    assert "Навантаження розеток станції: 55 Вт" in text
    assert "AC ECO" in text
    assert "login" in text


def test_status_warns_when_ac_output_off() -> None:
    state = DeviceState(SN, baseline_pending=False, power=GridState.GRID, power_since=at(0))
    text = fmt().status([view(state, Reading(grid_w=0, ac_load_w=0, ac_on=False))], at(1), False)
    assert "AC output is off" in text


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (DeviceState(SN), "not enough data"),
        (
            DeviceState(
                SN,
                baseline_pending=False,
                power=GridState.GRID,
                offline_notified=True,
                unreachable_since=at(10),
            ),
            "no connection to the station since 14:10",
        ),
    ],
)
def test_status_unknown(state: DeviceState, expected: str) -> None:
    text = fmt().status([view(state, None, None)], at(20), auth_expired=False)
    assert expected in text
    assert "Station online: unknown" in text


def test_status_without_stations() -> None:
    assert "No stations" in fmt().status([], at(0), auth_expired=False)


def test_history_formatting() -> None:
    from bluetti_grid_bot.storage import Outage

    outages = [
        Outage(2, SN, at(600), None, None, None),
        Outage(1, SN, at(0), at(-10), at(93), None),
    ]
    text = fmt().history(outages, at(660))
    lines = text.splitlines()
    assert lines[0] == "Recent outages:"
    assert lines[1] == "• 21.11 00:00 — ongoing (1 h)"
    assert lines[2] == "• 20.11 14:00 — 15:33 (≈1 h 33 min)"
    assert fmt().history([], at(0)) == "No outages recorded yet."
