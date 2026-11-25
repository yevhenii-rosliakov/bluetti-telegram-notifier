from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from prometheus_client.parser import text_string_to_metric_families

from bluetti_grid_bot.auth import AuthExpiredError
from bluetti_grid_bot.cloud import CloudError
from bluetti_grid_bot.health import Metrics, station_id
from bluetti_grid_bot.models import Station
from bluetti_grid_bot.monitor import Monitor
from bluetti_grid_bot.storage import Storage

from .conftest import SN, T0, make_sample
from .test_monitor import make_monitor, poll

DASHBOARD = (
    Path(__file__).resolve().parent.parent
    / "charts/bluetti-grid-bot/dashboards/bluetti-grid-bot.json"
)
STATION = {"station": "Home", "station_id": station_id(SN)}
PLACEHOLDER = {SN: make_sample(ac=0, soc=0, ac_on="0")}
# Series that must only exist while the station delivers data.
READINGS = {
    "bluetti_battery_soc_percent",
    "bluetti_battery_runtime_seconds",
    "bluetti_battery_time_to_full_seconds",
    "bluetti_power_watts",
    "bluetti_output_on",
    "bluetti_output_eco",
}


def scrape(m: Monitor) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    text = m.render_metrics().decode()
    return {
        (s.name, frozenset(s.labels.items())): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


def value(m: Monitor, name: str, **labels: str) -> float | None:
    return scrape(m).get((name, frozenset({**STATION, **labels}.items())))


def names(m: Monitor) -> set[str]:
    return {name for name, _ in scrape(m)}


def full_sample(**kwargs: Any) -> dict[str, Any]:
    extra = {
        "PVAllTotalPower": "30",
        "SetCtrlDc": "0",
        "SetACECO": "0",
        "SetDCECO": "1",
        "DsgFullTime": "90",
        "ChgFullTime": "0",
    }
    return {SN: make_sample(**{**extra, **kwargs})}


async def test_station_values(tmp_path: Path, storage: Storage) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    await poll(m, src, clock, full_sample(ac=80, soc=73))
    assert value(m, "bluetti_battery_soc_percent") == 73
    for channel, watts in (("grid", 0), ("ac_load", 80), ("dc_load", 0), ("pv", 30)):
        assert value(m, "bluetti_power_watts", channel=channel) == watts
    assert value(m, "bluetti_output_on", output="ac") == 1
    assert value(m, "bluetti_output_on", output="dc") == 0
    assert value(m, "bluetti_output_eco", output="ac") == 0
    assert value(m, "bluetti_output_eco", output="dc") == 1
    assert value(m, "bluetti_grid_state", state="battery") == 1
    assert value(m, "bluetti_grid_state", state="grid") == 0
    assert value(m, "bluetti_grid_state_since_timestamp_seconds") == T0.timestamp()
    assert value(m, "bluetti_station_last_data_timestamp_seconds") == T0.timestamp()
    assert value(m, "bluetti_station_online") == 1
    assert value(m, "bluetti_battery_runtime_seconds") == 90 * 60
    assert "bluetti_battery_time_to_full_seconds" not in names(m)  # 0 = not charging
    assert SN not in m.render_metrics().decode()


async def test_estimates_follow_the_state(tmp_path: Path, storage: Storage) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    await poll(m, src, clock, full_sample(grid=500, ChgFullTime="45"))
    assert value(m, "bluetti_battery_time_to_full_seconds") == 45 * 60
    assert "bluetti_battery_runtime_seconds" not in names(m)  # on grid


async def test_missing_field_drops_its_series(tmp_path: Path, storage: Storage) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    await poll(m, src, clock, full_sample(soc=80))
    await poll(m, src, clock, full_sample(soc=None))
    assert value(m, "bluetti_battery_soc_percent") is None
    assert value(m, "bluetti_power_watts", channel="pv") == 30


@pytest.mark.parametrize(
    "answer",
    [{SN: None}, {SN: make_sample(online=False)}, PLACEHOLDER, CloudError("down")],
    ids=["no answer", "offline", "placeholder", "cloud error"],
)
async def test_values_expire_without_data(
    tmp_path: Path, storage: Storage, answer: dict[str, Any] | Exception
) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    await poll(m, src, clock, full_sample(soc=73))
    await poll(m, src, clock, answer)  # a short gap is bridged
    assert value(m, "bluetti_battery_soc_percent") == 73
    for _ in range(10):
        await poll(m, src, clock, answer)
    assert not READINGS & names(m)
    assert value(m, "bluetti_station_last_data_timestamp_seconds") == T0.timestamp()
    assert value(m, "bluetti_grid_state", state="unknown") == 1
    assert value(m, "bluetti_grid_state_since_timestamp_seconds") is None
    # "Offline" is an answer; without one the online flag is not known either.
    unanswered = isinstance(answer, Exception) or answer[SN] is None
    assert value(m, "bluetti_station_online") == (None if unanswered else 0)
    await poll(m, src, clock, full_sample(soc=60))
    assert value(m, "bluetti_battery_soc_percent") == 60


async def test_values_expire_when_polling_stops(tmp_path: Path, storage: Storage) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    await poll(m, src, clock, full_sample())
    clock.now += timedelta(minutes=10)
    assert not READINGS & names(m)


async def test_old_stored_values_are_not_exposed_after_restart(
    tmp_path: Path, storage: Storage
) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    await poll(m, src, clock, full_sample(soc=73))

    m2, src2, _, clock2 = make_monitor(tmp_path, storage)
    clock2.now = clock.now + timedelta(hours=5)
    await m2.start()
    await poll(m2, src2, clock2, {SN: make_sample(online=False)})
    assert not READINGS & names(m2)
    assert value(m2, "bluetti_station_last_data_timestamp_seconds") == T0.timestamp()


async def test_stations_with_the_same_name_stay_apart(tmp_path: Path, storage: Storage) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    other = "OTHERXXXXXXXXXXXXXXXX"
    src.stations.append(Station(other, "PR200V2", "Home"))
    await m.start()
    await poll(m, src, clock, {SN: make_sample(soc=80), other: make_sample(soc=40, sn=other)})
    assert value(m, "bluetti_battery_soc_percent") == 80
    assert value(m, "bluetti_battery_soc_percent", station_id=station_id(other)) == 40


async def test_event_counters_start_at_zero(tmp_path: Path, storage: Storage) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    assert value(m, "bluetti_events_total", type="GridLost") == 0
    await poll(m, src, clock, full_sample())
    for _ in range(2):
        await poll(m, src, clock, full_sample(ac=80))
    assert value(m, "bluetti_events_total", type="GridLost") == 1


async def test_login_expired(tmp_path: Path, storage: Storage) -> None:
    m, src, _, clock = make_monitor(tmp_path, storage)
    await m.start()
    key = ("bluetti_login_expired", frozenset({("site", "")}))
    assert scrape(m)[key] == 0
    await poll(m, src, clock, AuthExpiredError("rejected"))
    assert scrape(m)[key] == 1
    await poll(m, src, clock, full_sample())
    assert scrape(m)[key] == 0


# dashboard


def expressions(node: Any) -> list[str]:
    if isinstance(node, dict):
        own = [v for k, v in node.items() if k in ("expr", "query") and isinstance(v, str)]
        return own + [e for v in node.values() for e in expressions(v)]
    if isinstance(node, list):
        return [e for v in node for e in expressions(v)]
    return []


def test_dashboard_uses_existing_metrics() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    used = set(re.findall(r"\bbluetti_[a-z_]+", " ".join(expressions(dashboard))))
    known = {
        f"{metric.name}_total" if metric.type == "counter" else metric.name
        for metric in Metrics().registry.collect()
    }
    assert used
    assert used <= known


def test_dashboard_is_portable() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    assert dashboard["uid"] == "bluetti-grid-bot"
    assert dashboard["id"] is None
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    for panel in dashboard["panels"]:
        if panel["type"] != "row":
            assert panel["datasource"] == {"type": "prometheus", "uid": "${datasource}"}
