from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from bluetti_grid_bot import stats
from bluetti_grid_bot.config import ConfigError, load_settings
from bluetti_grid_bot.events import BatteryFull, GridLost
from bluetti_grid_bot.messages import Formatter, Translator
from bluetti_grid_bot.models import Station
from bluetti_grid_bot.schedule import (
    Schedule,
    ScheduleError,
    Slot,
    day_fingerprints,
    parse_outage_data,
    parse_yasno,
)
from bluetti_grid_bot.storage import Outage

from .conftest import SN, at

KYIV = ZoneInfo("Europe/Kyiv")  # UTC+2 in November; T0 = 20.11 14:00 Kyiv
HOME = Station(SN, "PR200V2", "Home")


def outage(start: float, end: float | None, n: int = 1, approx: bool = False) -> Outage:
    return Outage(
        n, SN, at(start), at(start - 5) if approx else None, None if end is None else at(end), None
    )


def fmt(lang: str = "en") -> Formatter:
    return Formatter(Translator(lang), KYIV, {SN: "Home"})


# stats


def test_summarize_clips_to_window() -> None:
    now = at(7 * 24 * 60)
    s = stats.summarize(
        [outage(-60, 60), outage(1000, 1120), outage(-500, -400), outage(now_m := 10050, None)],
        now,
        7,
    )
    assert now_m < 7 * 24 * 60
    assert s.count == 3  # the one that ended before the window is out
    assert s.total == timedelta(minutes=60 + 120 + 30)
    assert s.longest == timedelta(minutes=120)
    assert not s.approximate
    assert round(s.share_with_power) == 98


def test_by_day_and_by_hour_use_local_time() -> None:
    # 21:30-23:30 UTC on 20.11 = 23:30 on 20.11 to 01:30 on 21.11 in Kyiv.
    now = at(24 * 60)  # 21.11 14:00 Kyiv
    o = [outage(570, 690)]
    days = stats.by_day(o, now, 3, KYIV)
    assert days == [
        (date(2026, 11, 19), timedelta()),
        (date(2026, 11, 20), timedelta(minutes=30)),
        (date(2026, 11, 21), timedelta(minutes=90)),
    ]
    hours = stats.by_hour(o, now, 1, KYIV)
    assert hours[23] == pytest.approx(0.5)
    assert hours[0] == pytest.approx(1.0)
    assert hours[1] == pytest.approx(0.5)
    assert sum(hours) == pytest.approx(2.0)


def test_typical_duration_is_median_of_finished() -> None:
    assert stats.typical_duration([outage(0, 60), outage(100, 130)]) is None
    three = [outage(0, 60), outage(100, 130), outage(200, 400), outage(500, None)]
    assert stats.typical_duration(three) == timedelta(minutes=60)
    four = [*three, outage(600, 690)]
    assert stats.typical_duration(four) == timedelta(minutes=75)


# schedule parsing


def yasno_day(day: str, status: str, slots: list[tuple[int, int, str]]) -> dict[str, Any]:
    return {
        "date": f"{day}T00:00:00+02:00",
        "status": status,
        "slots": [{"start": a, "end": b, "type": t} for a, b, t in slots],
    }


YASNO = {
    "3.1": {
        "today": yasno_day(
            "2026-11-20",
            "ScheduleApplies",
            [
                (0, 120, "NotPlanned"),
                (120, 450, "Definite"),
                (450, 780, "NotPlanned"),
                (780, 1050, "Definite"),
                (1050, 1380, "NotPlanned"),
                (1380, 1440, "Definite"),
            ],
        ),
        "tomorrow": yasno_day("2026-11-21", "WaitingForSchedule", []),
        "updatedOn": "2026-11-20T21:45:02+00:00",
    },
    "1.1": {},
}


def test_parse_yasno() -> None:
    s = parse_yasno(YASNO, "3.1")
    f = fmt()
    assert [f.slot(x) for x in s.slots] == ["02:00–07:30", "13:00–17:30", "23:00–24:00"]
    assert s.days == (date(2026, 11, 20),)
    assert s.notice is None
    assert s.current(at(0)) == s.slots[1]  # 14:00 Kyiv
    assert s.upcoming(at(0)) == s.slots[2]
    assert Schedule.from_json(s.to_json()) == s


def test_parse_yasno_emergency_and_unknown_group() -> None:
    data = {"3.1": {"today": yasno_day("2026-11-20", "EmergencyShutdowns", [])}}
    s = parse_yasno(data, "3.1")
    assert s.days == ()
    assert s.notice == "20.11: EmergencyShutdowns"
    with pytest.raises(ScheduleError, match=r"3\.2"):
        parse_yasno(YASNO, "3.2")


def test_parse_outage_data_half_hours_and_merging() -> None:
    hours = {str(h): "yes" for h in range(1, 25)}
    hours.update({"3": "no", "4": "no", "5": "first", "10": "second", "11": "maybe", "24": "no"})
    tomorrow = {str(h): "yes" for h in range(1, 25)} | {"1": "no", "2": "mfirst"}
    data = {
        "fact": {"data": {"1795125600": {"GPV3.1": hours}, "1795212000": {"GPV3.1": tomorrow}}},
        "attention": "Екстрені відключення",
        "lastUpdateStatus": {"ok": True},
    }
    s = parse_outage_data(data, "3.1")
    f = fmt()
    assert s.days == (date(2026, 11, 20), date(2026, 11, 21))
    assert [(f.slot(x), x.possible) for x in s.slots] == [
        ("02:00–04:30", False),
        ("09:30–10:00", False),
        ("10:00–11:00", True),
        ("23:00–01:00", False),  # joined across midnight
        ("01:00–01:30", True),
    ]
    assert s.notice == "Екстрені відключення"
    with pytest.raises(ScheduleError):
        parse_outage_data(data, "4.1")


def test_day_fingerprints_change_with_slots() -> None:
    s = parse_yasno(YASNO, "3.1")
    changed = Schedule(s.group, s.slots[:2], s.days)
    assert day_fingerprints(s) != day_fingerprints(changed)
    assert list(day_fingerprints(s)) == ["2026-11-20"]


# formatting


def test_stats_report() -> None:
    now = at(3 * 24 * 60)
    o = [outage(0, 60), outage(1440, 1560, 2), outage(2880, 2940, 3, approx=True)]
    text = fmt().stats_report([HOME], o, now, 3)
    assert text.startswith("📊 Last 3 days\n\nOutages: 3\n")
    assert "Without power: ≈4 h" in text
    assert "Typical outage: ≈ 1 h (median)" in text
    assert "By day (last 3):" in text
    # Days are local calendar days; 20.11 is outside the last 3 (21-23.11).
    assert "21.11 █░░░░░░░░░ 2 h\n22.11 ░░░░░░░░░░ 1 h\n23.11 ░░░░░░░░░░ —" in text
    # 14-15 Kyiv out on all 3 days, 15-16 on one of them.
    assert "14–16 ███████░░░ 67%" in text
    assert fmt().stats_report([], [], now, 3) == fmt().t("status_no_stations")


def test_weekly_summary() -> None:
    now = at(14 * 24 * 60)
    o = [outage(100, 160), outage(8 * 24 * 60, 8 * 24 * 60 + 30, 2)]
    text = fmt("uk").weekly([HOME], o, now)
    assert text.splitlines()[0] == "📅 Підсумок тижня 27.11 – 04.12"
    assert "Відключень: 1" in text
    assert "Попередній тиждень: відключень 1, без світла 1 год" in text


def test_battery_full_and_grid_lost_runtime_messages() -> None:
    f = fmt()
    full = f.event(BatteryFull(SN, 100, at(0), at(130)), at(130))
    assert full == "🔋 Battery charged: 100% (2 h 10 min after power came back)"
    lost = f.event(GridLost(SN, at(0), None, 90, 100, 300), at(0)) or ""
    assert lost.endswith("Estimated runtime: 5 h")


def test_schedule_texts() -> None:
    s = parse_yasno(YASNO, "3.1")
    f = fmt()
    text = f.schedule(s, at(0))
    assert text.splitlines()[:6] == [
        "📅 Outage schedule, group 3.1",
        "",
        "Today, 20.11:",
        "• 02:00–07:30",
        "• 13:00–17:30",
        "• 23:00–24:00",
    ]
    assert "Without power: 11 h" in text
    assert "not available" in f.schedule(None, at(0))
    assert "not published yet" in f.schedule(s, at(24 * 60))
    assert f.schedule_match(s, at(-50), timedelta(minutes=15)) == "📅 On the schedule: 13:00–17:30"
    assert f.schedule_match(s, at(-120), timedelta(minutes=15)) == "📅 Not on the schedule"
    assert f.schedule_match(s, at(24 * 60), timedelta(minutes=15)) is None
    status = f.status([], at(0), False, schedules=[("", s)])
    assert status.startswith("📅 Planned outage now: 13:00–17:30 (group 3.1)")
    status = f.status([], at(240), False, schedules=[("", s)])
    assert status.startswith("📅 Next planned outage: 23:00–24:00")


def test_schedule_warning_text() -> None:
    slot = Slot(at(30), at(90))
    text = fmt().schedule_warning(slot, "3.1", [], at(0))
    assert text == (
        "⏰ Planned outage in 30 min: 14:30–15:30 (group 3.1)\n"
        "🔌 Don't forget to switch off the heater, the boiler and other power-hungry appliances."
    )


# config


def test_report_settings(tmp_path: Path) -> None:
    s = load_settings(_env_file=None, state_dir=tmp_path)
    assert s.weekly_summary_at is not None
    assert s.weekly_summary_at[0] == 0
    assert load_settings(_env_file=None, weekly_summary="off").weekly_summary_at is None
    assert (
        load_settings(_env_file=None, weekly_summary="Sunday 21:30").weekly_summary == "sun 21:30"
    )
    with pytest.raises(ConfigError, match="WEEKLY_SUMMARY"):
        load_settings(_env_file=None, weekly_summary="every day")
    ok = load_settings(
        _env_file=None,
        schedule_provider="outage-data-ua",
        schedule_region="Kyiv-Region",
        schedule_group=" 3.1 ",
    )
    assert (ok.schedule_region, ok.schedule_group) == ("kyiv-region", "3.1")
    with pytest.raises(ConfigError, match="SCHEDULE_REGION"):
        load_settings(_env_file=None, schedule_provider="yasno", schedule_region="odesa")
    with pytest.raises(ConfigError, match="SCHEDULE_GROUP"):
        load_settings(
            _env_file=None, schedule_provider="yasno", schedule_region="kyiv", schedule_group="x"
        )
    with pytest.raises(ConfigError, match="FULL_CHARGE_SOC"):
        load_settings(_env_file=None, full_charge_soc=101)
