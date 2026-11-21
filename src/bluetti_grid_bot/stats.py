"""Outage statistics over a time window: totals, breakdowns by day and by hour of day.

Pure functions of their inputs (``now`` is passed in), so they are easy to test.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .storage import Outage

# Typical outage length needs at least this many finished outages to mean anything.
TYPICAL_MIN_OUTAGES = 3


@dataclass(frozen=True, slots=True)
class Summary:
    days: int
    count: int
    total: timedelta
    longest: timedelta
    approximate: bool

    @property
    def share_with_power(self) -> float:
        """Percent of the window with grid power."""
        return 100 * (1 - self.total / timedelta(days=self.days))


def clipped(
    outages: Sequence[Outage], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Outage intervals cut to ``[start, end]``; an ongoing one lasts until ``end``."""
    spans = []
    for o in outages:
        a, b = max(o.start, start), min(o.end or end, end)
        if b > a:
            spans.append((a, b))
    return spans


def summarize(outages: Sequence[Outage], now: datetime, days: int) -> Summary:
    start = now - timedelta(days=days)
    inside = [o for o in outages if (o.end or now) > start and o.start < now]
    spans = [b - a for a, b in clipped(inside, start, now)]
    return Summary(
        days=days,
        count=len(inside),
        total=sum(spans, timedelta()),
        longest=max(spans, default=timedelta()),
        approximate=any(o.approximate for o in inside),
    )


def _hours(a: datetime, b: datetime, tz: ZoneInfo) -> Iterator[tuple[datetime, timedelta]]:
    """Split ``[a, b)`` at local hour boundaries; yields (local hour start, piece length)."""
    cursor = a
    while cursor < b:
        local = cursor.astimezone(tz).replace(minute=0, second=0, microsecond=0)
        boundary = local.astimezone(UTC) + timedelta(hours=1)
        piece_end = min(boundary, b)
        yield local, piece_end - cursor
        cursor = piece_end


def by_day(
    outages: Sequence[Outage], now: datetime, days: int, tz: ZoneInfo
) -> list[tuple[date, timedelta]]:
    """Time without power for each of the last ``days`` local calendar days (oldest first)."""
    today = now.astimezone(tz).date()
    totals = {today - timedelta(days=i): timedelta() for i in range(days)}
    first = min(totals)
    start = datetime.combine(first, datetime.min.time(), tz).astimezone(UTC)
    for a, b in clipped(outages, start, now):
        for hour, length in _hours(a, b, tz):
            if hour.date() in totals:
                totals[hour.date()] += length
    return sorted(totals.items())


def by_hour(outages: Sequence[Outage], now: datetime, days: int, tz: ZoneInfo) -> list[float]:
    """For each local hour of the day (0-23): share of the window's days it was out, 0..1."""
    totals = [timedelta()] * 24
    for a, b in clipped(outages, now - timedelta(days=days), now):
        for hour, length in _hours(a, b, tz):
            totals[hour.hour] += length
    return [min(1.0, t / timedelta(hours=days)) for t in totals]


def typical_duration(outages: Sequence[Outage]) -> timedelta | None:
    """Median length of finished outages, or None when there are too few of them."""
    lengths = sorted(o.end - o.start for o in outages if o.end is not None)
    if len(lengths) < TYPICAL_MIN_OUTAGES:
        return None
    mid = len(lengths) // 2
    return lengths[mid] if len(lengths) % 2 else (lengths[mid - 1] + lengths[mid]) / 2
