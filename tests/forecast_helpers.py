"""Shared helpers for driving the Phase-2 forecast evidence layer in tests.

Every helper works through the real coordinator refresh rather than calling the
recorder directly, so the tests exercise the path a live installation actually
takes -- including the ordering between measurement, forecast and issuance.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch

from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator
from custom_components.alpha_ems_manager.storage import DayRecord

from .conftest import TZ
from .synthetic import flat_day, shaped_day

#: A Wednesday, well clear of a daylight-saving transition.
NORMAL = date(2026, 8, 19)
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)


def local(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Return a local instant on ``day``."""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)


@contextmanager
def frozen(moment: datetime):
    """Pin the coordinator's clock, which is the only clock it reads."""
    with patch(
        "custom_components.alpha_ems_manager.coordinator.dt_util.now",
        return_value=moment,
    ):
        yield


async def refresh_at(coordinator: AlphaEmsCoordinator, moment: datetime) -> None:
    """Run one full coordinator refresh at a fixed instant."""
    with frozen(moment):
        await coordinator.async_refresh()


def history_before(
    reference: date, days: int = 6, daily_kwh: float = 12.0
) -> dict[date, DayRecord]:
    """Return complete learned days immediately before ``reference``."""
    return {
        reference - timedelta(days=offset): flat_day(
            reference - timedelta(days=offset), daily_kwh
        )
        for offset in range(1, days + 1)
    }


def seed(coordinator: AlphaEmsCoordinator, records: dict[date, DayRecord]) -> None:
    """Install a synthetic learning history and clear the evidence layer.

    Setting the integration up runs a real first refresh against the real wall
    clock, which legitimately issues snapshots for the actual current day. A
    test that then rewinds to a fixed date would be counting those alongside its
    own, so the evidence is reset here and every assertion below is about the
    timeline the test itself drove.

    Only the in-memory view is cleared. Nothing is written, so this cannot mask
    a persistence bug -- the round-trip tests reload from the store rather than
    from here.
    """
    coordinator.store.days = dict(records)
    reset_history(coordinator)


def reseed(coordinator: AlphaEmsCoordinator, records: dict[date, DayRecord]) -> None:
    """Change the learning history *without* touching the evidence layer.

    Used where a test needs the model to move mid-timeline and then asserts on
    what the evidence layer did about it.
    """
    coordinator.store.days = dict(records)


def reset_history(coordinator: AlphaEmsCoordinator) -> None:
    """Empty the forecast evidence so a test starts from a known state."""
    history = coordinator.history
    history.days.clear()
    history.months.clear()
    history._partitions.clear()
    history.snapshot_cap_hits = 0
    history.pruned_days = 0
    coordinator.recorder.duplicate_issuances = 0
    coordinator.recorder._last_day = None


def shaped_history(
    reference: date, days: int, profile: list[float]
) -> dict[date, DayRecord]:
    """Return days carrying an explicit per-interval profile."""
    return {
        reference - timedelta(days=offset): shaped_day(
            reference - timedelta(days=offset), profile
        )
        for offset in range(1, days + 1)
    }


def snapshot_days(coordinator: AlphaEmsCoordinator) -> dict[date, int]:
    """Return how many snapshots are retained per target day."""
    return {
        day: row.snapshot_count
        for day, row in coordinator.history.days.items()
        if row.snapshot_count
    }


def total_snapshots(coordinator: AlphaEmsCoordinator) -> int:
    """Return the number of snapshots retained across every target day."""
    return coordinator.history.snapshot_total
