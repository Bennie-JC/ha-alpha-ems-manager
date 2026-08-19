"""Builders for deterministic synthetic learning history.

Every forecast and confidence test works from history built here, so the inputs
are fully specified and no test depends on an accident of another test's data.

Days are built with their *real* interval count, so a spring-forward day is 92
intervals long and a fall-back day 100. Anything that quietly assumed 96 shows
up immediately.
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from custom_components.alpha_ems_manager.storage import (
    DayRecord,
    day_type_of,
    expected_quarters_for,
)

TZ = ZoneInfo("Europe/Amsterdam")


def empty_day(day: date, tz: ZoneInfo = TZ) -> DayRecord:
    """Return a record sized to the real length of the civil day."""
    return DayRecord(
        day=day,
        tz_key=str(tz),
        interval_count=expected_quarters_for(day, tz),
    )


def flat_day(
    day: date,
    daily_kwh: float,
    *,
    accepted_intervals: int | None = None,
    ev_kwh_per_interval: float | None = None,
    ev_expected: bool = False,
    ev_valid_intervals: int | None = None,
    tz: ZoneInfo = TZ,
) -> DayRecord:
    """Return a day whose consumption is spread evenly across its intervals.

    ``accepted_intervals`` controls how many intervals carry a measured reading,
    which is how coverage behaviour is exercised. ``ev_valid_intervals`` does the
    same for the flexible load, so a day can have perfect measured data and
    partial EV data -- the case that must keep measured history while
    invalidating baseline.

    ``daily_kwh`` is the *measured* daily total; any EV energy is on top of the
    baseline, so baseline works out to ``daily_kwh - ev_total``.
    """
    record = empty_day(day, tz)
    count = record.interval_count
    measured_count = count if accepted_intervals is None else accepted_intervals
    ev_count = count if ev_valid_intervals is None else ev_valid_intervals
    per_interval = daily_kwh / count

    for index in range(count):
        if index >= measured_count:
            # Faithful to the coordinator, which files a quarter only once it is
            # *accepted*: an interval that never reached coverage, or that fell
            # inside a restart, is never handed to ``record_interval`` at all and
            # so keeps the padded defaults -- including ``ev_expected=False``.
            # Filing it here instead wrote a flexible-load expectation onto an
            # interval that was never observed, which is precisely the state a
            # live installation can never be in, and it hid the beta.5
            # definition-change false positive from every test in this suite.
            continue
        has_ev = ev_expected and index < ev_count
        record.record_interval(
            index,
            measured_kwh=per_interval,
            ev_kwh=(ev_kwh_per_interval or 0.0) if has_ev else None,
            ev_expected=ev_expected,
        )
    return record


def shaped_day(day: date, interval_kwh: list[float], *, tz: ZoneInfo = TZ) -> DayRecord:
    """Return a day with an explicit per-interval profile."""
    record = empty_day(day, tz)
    for index, value in enumerate(interval_kwh[: record.interval_count]):
        record.record_interval(
            index, measured_kwh=value, ev_kwh=None, ev_expected=False
        )
    return record


def history(
    reference: date,
    days: int,
    daily_kwh: float | dict[str, float],
    *,
    accepted_intervals: int | None = None,
    tz: ZoneInfo = TZ,
) -> list[DayRecord]:
    """Return ``days`` consecutive days ending the day before ``reference``.

    ``daily_kwh`` may be a single figure, or a mapping of day type to figure so
    weekday and weekend behaviour can differ.
    """
    records: list[DayRecord] = []
    for offset in range(1, days + 1):
        day = reference - timedelta(days=offset)
        total = (
            daily_kwh[day_type_of(day)] if isinstance(daily_kwh, dict) else daily_kwh
        )
        records.append(
            flat_day(day, total, accepted_intervals=accepted_intervals, tz=tz)
        )
    return records
