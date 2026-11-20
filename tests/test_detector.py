from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta

import pytest

from bluetti_grid_bot.detector import DetectorConfig, DeviceState, classify, evaluate
from bluetti_grid_bot.events import (
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
from bluetti_grid_bot.models import DetectionMode, GridState, Reading, Sample

from .conftest import SN, at, make_sample

ATS = DetectorConfig(mode=DetectionMode.ATS)
PASS = DetectorConfig(mode=DetectionMode.PASSTHROUGH)
GRID_IN = DetectorConfig(mode=DetectionMode.GRID_INPUT)

GRID_IDLE = make_sample(grid=0, ac=0)  # ATS: battery full, nothing drawn, grid present
CHARGING = make_sample(grid=300, ac=0, soc=80)
ON_BATTERY = make_sample(grid=0, ac=120, soc=90)


def run(
    cfg: DetectorConfig,
    steps: Sequence[Sample | None],
    state: DeviceState | None = None,
    start: float = 0,
) -> tuple[DeviceState, list[tuple[float, Event]]]:
    s = state or DeviceState(SN)
    events: list[tuple[float, Event]] = []
    for i, sample in enumerate(steps):
        minute = start + i
        result = evaluate(s, sample, cfg, at(minute))
        s = result.state
        events += [(minute, e) for e in result.events]
    return s, events


def kinds(events: list[tuple[float, Event]]) -> list[str]:
    return [type(e).__name__ for _, e in events]


def baseline(cfg: DetectorConfig, sample: Sample) -> DeviceState:
    s, _ = run(cfg, [sample])
    return s


# classification


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        (Reading(grid_w=0, ac_load_w=0), GridState.GRID),  # full battery, idle: grid present
        (Reading(grid_w=300, ac_load_w=0), GridState.GRID),  # charging
        (Reading(grid_w=300, ac_load_w=200), GridState.GRID),  # grid wins
        (Reading(grid_w=0, ac_load_w=10), GridState.BATTERY),  # 10 W router on battery
        (Reading(grid_w=4.9, ac_load_w=5), GridState.BATTERY),
        (Reading(grid_w=0, ac_load_w=4.9), GridState.GRID),
        (Reading(grid_w=None, ac_load_w=50), None),
        (Reading(grid_w=None, ac_load_w=0), GridState.GRID),
        (Reading(grid_w=0, ac_load_w=None), None),
        (Reading(grid_w=10, ac_load_w=None), GridState.GRID),
    ],
)
def test_classify_ats(reading: Reading, expected: GridState | None) -> None:
    assert classify(reading, ATS, None).state is expected


@pytest.mark.parametrize(
    ("reading", "soc_at_grid", "expected"),
    [
        (Reading(grid_w=100, ac_load_w=50), None, GridState.GRID),
        (Reading(grid_w=0, ac_load_w=50), None, GridState.BATTERY),
        (Reading(grid_w=0, ac_load_w=3, dc_load_w=3), None, GridState.BATTERY),  # AC + DC
        (Reading(grid_w=0, dc_load_w=12), None, GridState.BATTERY),
        (Reading(grid_w=0, ac_load_w=0, soc=100), None, None),  # idle: cannot tell
        (Reading(grid_w=0, ac_load_w=0, soc=97), 100.0, GridState.BATTERY),  # SOC drop
        (Reading(grid_w=0, ac_load_w=0, soc=99), 100.0, None),  # drop below SOC_DROP_PCT
        (Reading(grid_w=None, ac_load_w=50), None, None),
    ],
)
def test_classify_passthrough(
    reading: Reading, soc_at_grid: float | None, expected: GridState | None
) -> None:
    assert classify(reading, PASS, soc_at_grid).state is expected


@pytest.mark.parametrize(
    ("grid", "expected"),
    [(0.0, GridState.BATTERY), (4.0, GridState.BATTERY), (5.0, GridState.GRID), (None, None)],
)
def test_classify_grid_input(grid: float | None, expected: GridState | None) -> None:
    assert classify(Reading(grid_w=grid, ac_load_w=100), GRID_IN, None).state is expected


def test_classify_has_reason() -> None:
    c = classify(Reading(grid_w=0, ac_load_w=10), ATS, None)
    assert "AC load 10 W" in c.reason


# first poll / baseline


def test_first_poll_on_grid_is_baseline_without_outage_alerts() -> None:
    s, events = run(ATS, [GRID_IDLE])
    assert kinds(events) == ["BotStartedOnGrid"]
    assert s.power is GridState.GRID
    assert s.power_since == at(0)
    assert not s.baseline_pending
    assert s.effective is GridState.GRID


def test_first_poll_on_battery_reports_bot_started() -> None:
    s, events = run(ATS, [ON_BATTERY])
    assert kinds(events) == ["BotStartedOnBattery"]
    ev = events[0][1]
    assert isinstance(ev, BotStartedOnBattery)
    assert ev.since == at(0)
    assert not ev.since_known
    assert ev.soc == 90
    assert ev.load_w == 120
    assert s.power is GridState.BATTERY


def test_baseline_waits_for_a_determinate_sample() -> None:
    s, events = run(PASS, [make_sample(grid=0, ac=0)])  # cannot tell in passthrough
    assert events == []
    assert s.baseline_pending
    assert s.effective is GridState.UNKNOWN
    s, events = run(PASS, [make_sample(grid=100, ac=0)], s)
    assert kinds(events) == ["BotStartedOnGrid"]


def test_restart_during_same_outage_keeps_start_time_and_fired_alerts() -> None:
    s, _ = run(ATS, [GRID_IDLE, ON_BATTERY, ON_BATTERY, make_sample(grid=0, ac=100, soc=25)])
    assert s.power is GridState.BATTERY
    assert s.low_soc_fired == (30,)
    restarted = replace(s, baseline_pending=True)
    s2, events = run(ATS, [make_sample(grid=0, ac=100, soc=24)], restarted, start=10)
    assert kinds(events) == ["BotStartedOnBattery"]
    ev = events[0][1]
    assert isinstance(ev, BotStartedOnBattery)
    assert ev.since_known
    assert ev.since == at(1)
    assert s2.low_soc_fired == (30,)


def test_restart_after_outage_ended_while_down_closes_it_silently() -> None:
    s, _ = run(ATS, [GRID_IDLE, ON_BATTERY, ON_BATTERY])
    restarted = replace(s, baseline_pending=True)
    s2, events = run(ATS, [GRID_IDLE], restarted, start=60)
    assert kinds(events) == ["OutageEndedUnobserved", "BotStartedOnGrid"]
    assert isinstance(events[0][1], OutageEndedUnobserved)
    assert s2.power is GridState.GRID
    assert s2.power_since == at(60)


def test_restart_when_outage_began_while_down() -> None:
    s, _ = run(ATS, [GRID_IDLE])
    restarted = replace(s, baseline_pending=True)
    s2, events = run(ATS, [ON_BATTERY], restarted, start=30)
    ev = events[0][1]
    assert isinstance(ev, BotStartedOnBattery)
    assert not ev.since_known
    assert s2.power_since == at(30)
    assert s2.power_since_earliest == at(0)  # grid was last seen then


def test_restart_on_grid_keeps_grid_since() -> None:
    s, _ = run(ATS, [GRID_IDLE])
    s2, _ = run(ATS, [GRID_IDLE], replace(s, baseline_pending=True), start=30)
    assert s2.power_since == at(0)


# transitions with debounce, all modes


@pytest.mark.parametrize(
    ("cfg", "grid_sample", "battery_sample"),
    [
        (ATS, GRID_IDLE, ON_BATTERY),
        (ATS, CHARGING, ON_BATTERY),
        (PASS, make_sample(grid=200, ac=80), make_sample(grid=0, ac=80, soc=95)),
        (GRID_IN, make_sample(grid=40, ac=0), make_sample(grid=0, ac=0, soc=95)),
    ],
)
def test_outage_and_restore_in_every_mode(
    cfg: DetectorConfig, grid_sample: Sample, battery_sample: Sample
) -> None:
    s, events = run(
        cfg,
        [grid_sample, battery_sample, battery_sample, battery_sample, grid_sample, grid_sample],
    )
    assert kinds(events) == ["BotStartedOnGrid", "GridLost", "GridRestored"]
    lost = events[1][1]
    assert isinstance(lost, GridLost)
    assert events[1][0] == 2  # confirmed on the 2nd consecutive poll ...
    assert lost.at == at(1)  # ... but timed at the first one
    assert lost.earliest is None
    restored = events[2][1]
    assert isinstance(restored, GridRestored)
    assert restored.at == at(4)
    assert restored.outage_start == at(1)
    assert restored.duration == timedelta(minutes=3)
    assert not restored.approximate
    assert s.power is GridState.GRID


def test_single_blip_is_debounced() -> None:
    _, events = run(ATS, [GRID_IDLE, ON_BATTERY, GRID_IDLE, ON_BATTERY, GRID_IDLE])
    assert kinds(events) == ["BotStartedOnGrid"]


def test_debounce_counts_are_configurable() -> None:
    cfg = replace(ATS, debounce_polls=3, restore_debounce_polls=1)
    _, events = run(cfg, [GRID_IDLE, ON_BATTERY, ON_BATTERY, GRID_IDLE])
    assert kinds(events) == ["BotStartedOnGrid"]
    _, events = run(cfg, [GRID_IDLE, ON_BATTERY, ON_BATTERY, ON_BATTERY, GRID_IDLE])
    assert kinds(events) == ["BotStartedOnGrid", "GridLost", "GridRestored"]
    assert events[2][0] == 4  # restore with a single poll


def test_debounce_one_reports_immediately() -> None:
    cfg = replace(ATS, debounce_polls=1, restore_debounce_polls=1)
    _, events = run(cfg, [GRID_IDLE, ON_BATTERY, GRID_IDLE])
    assert kinds(events) == ["BotStartedOnGrid", "GridLost", "GridRestored"]


def test_indeterminate_sample_neither_counts_nor_resets() -> None:
    cfg = PASS
    grid = make_sample(grid=200, ac=80)
    battery = make_sample(grid=0, ac=80, soc=100)
    idle = make_sample(grid=0, ac=0, soc=100)
    _, events = run(cfg, [grid, battery, idle, battery])
    assert kinds(events) == ["BotStartedOnGrid", "GridLost"]
    assert events[1][1].at == at(1)  # type: ignore[union-attr]


def test_passthrough_soc_drop_detects_outage_without_load_reading() -> None:
    grid = make_sample(grid=50, ac=0, soc=100)
    _, events = run(
        PASS,
        [
            grid,
            make_sample(grid=0, ac=0, soc=99),
            make_sample(grid=0, ac=0, soc=98),
            make_sample(grid=0, ac=0, soc=97),
        ],
    )
    assert kinds(events) == ["BotStartedOnGrid", "GridLost"]
    assert events[1][1].at == at(2)  # type: ignore[union-attr]


def test_ats_small_load_like_a_router_is_detected() -> None:
    router = make_sample(grid=0, ac=10, soc=100)
    _, events = run(ATS, [GRID_IDLE, router, router])
    assert kinds(events) == ["BotStartedOnGrid", "GridLost"]


# offline / unknown


def test_offline_reported_after_grace_and_state_unknown() -> None:
    cfg = replace(ATS, offline_grace=timedelta(minutes=10))
    steps: list[Sample | None] = [GRID_IDLE] + [None] * 11
    s, events = run(cfg, steps)
    assert kinds(events) == ["BotStartedOnGrid", "StationOffline"]
    off = events[1][1]
    assert isinstance(off, StationOffline)
    assert events[1][0] == 11  # 10 min after the first failure at minute 1
    assert off.since == at(1)
    assert off.last_seen == at(0)
    assert s.effective is GridState.UNKNOWN
    assert s.power is GridState.GRID  # last confirmed state is remembered


def test_online_zero_and_empty_state_list_count_as_no_data() -> None:
    cfg = replace(ATS, offline_grace=timedelta(0))
    _, events = run(cfg, [GRID_IDLE, make_sample(online=False)])
    assert kinds(events) == ["BotStartedOnGrid", "StationOffline"]
    _, events = run(cfg, [GRID_IDLE, Sample.from_raw(SN, True, {})])
    assert kinds(events) == ["BotStartedOnGrid", "StationOffline"]


def test_short_glitch_below_grace_is_silent() -> None:
    _, events = run(ATS, [GRID_IDLE, None, None, GRID_IDLE, GRID_IDLE])
    assert kinds(events) == ["BotStartedOnGrid"]


def test_offline_is_never_reported_as_outage_or_restore() -> None:
    cfg = replace(ATS, offline_grace=timedelta(minutes=2))
    _, events = run(cfg, [ON_BATTERY, None, None, None, None, ON_BATTERY, ON_BATTERY])
    assert kinds(events) == ["BotStartedOnBattery", "StationOffline", "StationOnline"]


def test_back_online_with_changed_state_reports_time_window() -> None:
    cfg = replace(ATS, offline_grace=timedelta(minutes=5))
    steps: list[Sample | None] = [GRID_IDLE] + [None] * 39 + [ON_BATTERY, ON_BATTERY]
    s, events = run(cfg, steps)
    assert kinds(events) == ["BotStartedOnGrid", "StationOffline", "StationOnline", "GridLost"]
    back = events[2][1]
    assert isinstance(back, StationOnline)
    assert back.offline_since == at(1)
    lost = events[3][1]
    assert isinstance(lost, GridLost)
    assert lost.earliest == at(0)  # last seen with grid
    assert lost.at == at(40)  # first poll after reconnecting
    assert s.power_since_earliest == at(0)

    # And the restore after an approximate start is approximate too.
    s, events = run(cfg, [GRID_IDLE, GRID_IDLE], s, start=50)
    restored = events[0][1]
    assert isinstance(restored, GridRestored)
    assert restored.approximate
    assert restored.earliest is None


def test_back_online_unchanged_clears_gap() -> None:
    cfg = replace(ATS, offline_grace=timedelta(minutes=1))
    s, events = run(cfg, [GRID_IDLE, None, None, None, GRID_IDLE])
    assert s.gap_start is None
    s, events = run(cfg, [ON_BATTERY, ON_BATTERY], s, start=10)
    lost = events[0][1]
    assert isinstance(lost, GridLost)
    assert lost.earliest is None


def test_offline_interrupts_debounce() -> None:
    _, events = run(ATS, [GRID_IDLE, ON_BATTERY, None, ON_BATTERY, GRID_IDLE])
    assert kinds(events) == ["BotStartedOnGrid"]


def test_offline_before_any_baseline() -> None:
    cfg = replace(ATS, offline_grace=timedelta(minutes=1))
    s, events = run(cfg, [None, None, GRID_IDLE])
    assert kinds(events) == ["StationOffline", "StationOnline", "BotStartedOnGrid"]
    assert s.power is GridState.GRID


# low battery


def test_low_soc_fires_once_per_threshold_per_outage() -> None:
    socs = [40, 31, 30, 29, 30, 20, 15, 14, 10]
    steps = [GRID_IDLE, ON_BATTERY, ON_BATTERY] + [make_sample(grid=0, ac=100, soc=v) for v in socs]
    s, events = run(ATS, steps)
    lows = [e for _, e in events if isinstance(e, LowBattery)]
    assert [(e.threshold, e.soc) for e in lows] == [(30, 30), (15, 15)]

    # Next outage: alerts are armed again.
    s, _ = run(ATS, [GRID_IDLE, GRID_IDLE], s, start=20)
    _, events = run(
        ATS, [make_sample(grid=0, ac=100, soc=25), make_sample(grid=0, ac=100, soc=25)], s, 30
    )
    assert kinds(events) == ["GridLost", "LowBattery"]


def test_low_soc_only_while_on_battery() -> None:
    _, events = run(ATS, [make_sample(grid=0, ac=0, soc=10), make_sample(grid=200, soc=12)])
    assert "LowBattery" not in kinds(events)


def test_low_soc_several_thresholds_at_once_sends_one_message() -> None:
    s, events = run(ATS, [make_sample(grid=0, ac=50, soc=12, DsgFullTime="45")])
    assert kinds(events) == ["BotStartedOnBattery", "LowBattery"]
    low = events[1][1]
    assert isinstance(low, LowBattery)
    assert low.threshold == 15
    assert low.runtime_min == 45
    assert s.low_soc_fired == (30, 15)


def test_low_soc_disabled() -> None:
    cfg = replace(ATS, low_soc_thresholds=())
    _, events = run(cfg, [make_sample(grid=0, ac=50, soc=5)])
    assert kinds(events) == ["BotStartedOnBattery"]


# AC output off


def test_ac_off_alert_debounced_once_until_back_on() -> None:
    off = make_sample(grid=0, ac=0, ac_on="0")
    on = make_sample(grid=0, ac=0, ac_on="1")
    _, events = run(ATS, [on, off, off, off, off, on, off, off])
    acs = [m for m, e in events if isinstance(e, AcOutputOff)]
    assert acs == [2, 7]


def test_ac_off_single_poll_is_ignored() -> None:
    _, events = run(ATS, [GRID_IDLE, make_sample(ac_on="0"), GRID_IDLE])
    assert "AcOutputOff" not in kinds(events)


def test_ac_off_alert_can_be_disabled() -> None:
    cfg = replace(ATS, alert_ac_off=False)
    off = make_sample(ac_on="0")
    _, events = run(cfg, [off, off, off])
    assert "AcOutputOff" not in kinds(events)


def test_ac_unknown_value_does_not_alert() -> None:
    _, events = run(ATS, [make_sample(ac_on=None), make_sample(ac_on="x"), make_sample(ac_on="x")])
    assert "AcOutputOff" not in kinds(events)


# robustness / persistence


def test_garbage_values_are_unknown_not_crashes() -> None:
    weird = make_sample(grid="", ac="n/a", soc="abc", DsgFullTime="--")
    s, events = run(ATS, [weird, weird])
    assert events == []
    assert s.baseline_pending
    assert set(weird.invalid_fields) == {"ACLoadAllTotalPower", "SOC", "DsgFullTime"}


def test_evaluate_does_not_mutate_input() -> None:
    s = DeviceState(SN)
    before = s.to_dict()
    evaluate(s, ON_BATTERY, ATS, at(0))
    assert s.to_dict() == before


def test_state_round_trip() -> None:
    cfg = replace(ATS, offline_grace=timedelta(minutes=1))
    s, _ = run(cfg, [GRID_IDLE, ON_BATTERY, ON_BATTERY, None, None, None, ON_BATTERY])
    s = replace(s, low_soc_fired=(30,), candidate=GridState.GRID, candidate_count=1)
    assert DeviceState.from_dict(s.to_dict()) == s


def test_from_dict_ignores_unknown_keys() -> None:
    s = DeviceState.from_dict({"sn": SN, "power": "grid", "future_field": 1, "power_since": None})
    assert s.power is GridState.GRID


def test_startup_grid_event_only_once() -> None:
    _, events = run(ATS, [GRID_IDLE, GRID_IDLE, GRID_IDLE])
    assert [type(e) for _, e in events] == [BotStartedOnGrid]
