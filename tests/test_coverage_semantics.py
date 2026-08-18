"""Coverage must be measured against time that has actually happened.

Diagnostics divided every valid interval by the *full* civil length of every
retained day, including the day still in progress. A perfectly healthy
installation therefore reported 25 % coverage at 06:00 and recovered by itself
at midnight, because the denominator counted an evening that had not occurred.

The four populations are now distinguished explicitly:

``learning.*``
    so-far coverage across retained days, against intervals that have elapsed.
``learning.completed_days.*``
    finalised days only, against their full civil length.
``learning.current_day.*``
    the running day on its own, against the quarters that have closed.
``confidence.*``
    learned days only -- the population that actually feeds the score.

Daylight saving is respected throughout: 92, 96 or 100, never a hard-coded 96.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.confidence import compute_confidence
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.alpha_ems_manager.storage import (
    elapsed_quarters_for,
    expected_quarters_for,
)

from .conftest import TZ
from .synthetic import empty_day, flat_day

FALL_BACK_DAY = date(2026, 10, 25)
SPRING_FORWARD_DAY = date(2026, 3, 29)
NORMAL_DAY = date(2026, 8, 18)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """Return a local instant on ``day``."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


# -- the primitive -----------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, 0),
        (0, 15, 1),
        (0, 14, 0),
        (12, 0, 48),
        (23, 45, 95),
        (23, 59, 95),
    ],
)
def test_elapsed_quarters_counts_only_closed_intervals(
    hour: int, minute: int, expected: int
) -> None:
    """An interval counts once it has fully elapsed, not once it has begun."""
    assert (
        elapsed_quarters_for(NORMAL_DAY, TZ, at(NORMAL_DAY, hour, minute)) == expected
    )


def test_a_finalised_day_is_measured_against_its_whole_civil_length() -> None:
    """Yesterday can no longer gain intervals, so nothing is discounted."""
    later = at(NORMAL_DAY + timedelta(days=1), 9)
    assert elapsed_quarters_for(NORMAL_DAY, TZ, later) == 96
    assert elapsed_quarters_for(NORMAL_DAY, TZ, later) == expected_quarters_for(
        NORMAL_DAY, TZ
    )


def test_a_future_day_has_not_elapsed_at_all() -> None:
    """A day that has not started contributes no denominator."""
    assert (
        elapsed_quarters_for(NORMAL_DAY + timedelta(days=2), TZ, at(NORMAL_DAY, 9)) == 0
    )


@pytest.mark.parametrize(
    ("day", "length"),
    [(SPRING_FORWARD_DAY, 92), (NORMAL_DAY, 96), (FALL_BACK_DAY, 100)],
)
def test_daylight_saving_days_saturate_at_their_real_length(
    day: date, length: int
) -> None:
    """92, 96 or 100 -- never a hard-coded 96, at either end of the clamp."""
    assert expected_quarters_for(day, TZ) == length
    assert elapsed_quarters_for(day, TZ, at(day + timedelta(days=1), 12)) == length
    # And midway through, absolute elapsed time is what counts: the fall-back
    # day's repeated hour advances the index rather than rewinding it.
    midday = elapsed_quarters_for(day, TZ, at(day, 12))
    assert 0 < midday < length


def test_the_fall_back_repeated_hour_advances_the_count() -> None:
    """Both occurrences of 02:30 are distinct instants, an hour apart."""
    first = datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=1)
    assert (
        elapsed_quarters_for(FALL_BACK_DAY, TZ, second)
        - elapsed_quarters_for(FALL_BACK_DAY, TZ, first)
        == 4
    )


# -- the reported defect, through diagnostics --------------------------------


async def _diagnostics(
    hass: HomeAssistant, entry: MockConfigEntry, now: datetime, records
):
    """Return the diagnostics payload with ``records`` stored and clock ``now``."""
    coordinator = entry.runtime_data
    coordinator.store.days = {record.day: record for record in records}
    with patch(
        "custom_components.alpha_ems_manager.diagnostics.dt_util.now", return_value=now
    ):
        return await async_get_config_entry_diagnostics(hass, entry)


@pytest.mark.parametrize(
    ("hour", "minute"),
    [(0, 15), (12, 0), (23, 45)],
    ids=["just_after_midnight", "midday", "last_quarter"],
)
async def test_the_running_day_is_not_penalised_for_its_own_future(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hour: int,
    minute: int,
) -> None:
    """Perfect measurement so far must read as perfect coverage so far.

    Fails on beta.3, where the denominator is the whole civil day and coverage
    reads as low as 0.01 at 00:15.
    """
    now = at(NORMAL_DAY, hour, minute)
    elapsed = elapsed_quarters_for(NORMAL_DAY, TZ, now)
    today = flat_day(NORMAL_DAY, 10.0, accepted_intervals=elapsed)

    payload = await _diagnostics(hass, setup_integration, now, [today])
    learning = payload["learning"]

    assert learning["occurred_intervals"] == elapsed
    assert learning["measured_coverage"] == 1.0
    assert learning["baseline_coverage"] == 1.0
    assert learning["measured_missing_intervals"] == 0
    assert learning["current_day"]["elapsed_intervals"] == elapsed
    assert learning["current_day"]["measured_coverage_so_far"] == 1.0
    # The full civil day is still reported, just not used as the denominator.
    assert learning["retained_real_intervals"] == 96
    assert learning["current_day"]["expected_intervals"] == 96


async def test_a_real_gap_in_the_running_day_is_still_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The fix must not make coverage unconditionally 1.0."""
    now = at(NORMAL_DAY, 12)
    elapsed = elapsed_quarters_for(NORMAL_DAY, TZ, now)
    today = flat_day(NORMAL_DAY, 10.0, accepted_intervals=elapsed - 12)

    learning = (await _diagnostics(hass, setup_integration, now, [today]))["learning"]

    assert learning["measured_missing_intervals"] == 12
    assert learning["measured_coverage"] == pytest.approx(
        (elapsed - 12) / elapsed, abs=1e-4
    )


async def test_a_finalised_partial_day_keeps_its_full_denominator(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A day that ended half-measured is half-covered, permanently."""
    yesterday = flat_day(NORMAL_DAY, 10.0, accepted_intervals=48)
    now = at(NORMAL_DAY + timedelta(days=1), 9)

    learning = (await _diagnostics(hass, setup_integration, now, [yesterday]))[
        "learning"
    ]

    assert learning["occurred_intervals"] == 96
    assert learning["measured_coverage"] == 0.5
    assert learning["completed_days"]["days"] == 1
    assert learning["completed_days"]["measured_coverage"] == 0.5


async def test_completed_and_current_populations_are_reported_separately(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The two questions have different answers and must not share a field."""
    now = at(NORMAL_DAY, 6)
    elapsed = elapsed_quarters_for(NORMAL_DAY, TZ, now)
    records = [
        flat_day(NORMAL_DAY - timedelta(days=1), 10.0),
        flat_day(NORMAL_DAY, 1.0, accepted_intervals=elapsed),
    ]

    learning = (await _diagnostics(hass, setup_integration, now, records))["learning"]

    assert learning["completed_days"]["days"] == 1
    assert learning["completed_days"]["real_intervals"] == 96
    assert learning["completed_days"]["measured_coverage"] == 1.0
    assert learning["current_day"]["date"] == NORMAL_DAY.isoformat()
    assert learning["current_day"]["elapsed_intervals"] == elapsed
    assert learning["current_day"]["counts_toward_learned_days"] is False


@pytest.mark.parametrize(
    ("day", "length"),
    [(SPRING_FORWARD_DAY, 92), (FALL_BACK_DAY, 100)],
    ids=["spring_forward", "fall_back"],
)
async def test_a_daylight_saving_day_uses_its_real_length_in_diagnostics(
    hass: HomeAssistant, setup_integration: MockConfigEntry, day: date, length: int
) -> None:
    """Nothing in the coverage path may assume 96."""
    now = at(day + timedelta(days=1), 9)
    learning = (
        await _diagnostics(hass, setup_integration, now, [flat_day(day, 10.0)])
    )["learning"]

    assert learning["occurred_intervals"] == length
    assert learning["retained_real_intervals"] == length
    assert learning["measured_coverage"] == 1.0


async def test_a_restart_mid_day_shows_the_gap_it_really_left(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Downtime is missing coverage; the hours after it are not."""
    now = at(NORMAL_DAY, 18)
    elapsed = elapsed_quarters_for(NORMAL_DAY, TZ, now)
    today = empty_day(NORMAL_DAY)
    # Measured 00:00-08:00, down 08:00-10:00, measured 10:00 until now.
    for index in range(elapsed):
        if 32 <= index < 40:
            continue
        today.record_interval(index, measured_kwh=0.1, ev_kwh=None, ev_expected=False)

    learning = (await _diagnostics(hass, setup_integration, now, [today]))["learning"]

    assert learning["measured_missing_intervals"] == 8
    assert learning["occurred_intervals"] == elapsed


async def test_a_day_with_no_record_at_all_is_simply_absent(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A missing historical day is not a covered day and not a gap either.

    Retention holds days that exist; a day never recorded contributes to no
    numerator and no denominator, so it cannot silently depress coverage.
    """
    now = at(NORMAL_DAY, 12)
    records = [flat_day(NORMAL_DAY - timedelta(days=3), 10.0)]

    learning = (await _diagnostics(hass, setup_integration, now, records))["learning"]

    assert learning["retained_days"] == 1
    assert learning["occurred_intervals"] == 96
    assert learning["measured_coverage"] == 1.0
    assert learning["current_day"]["elapsed_intervals"] > 0
    assert learning["current_day"]["measured_valid_intervals"] == 0
    assert learning["current_day"]["measured_coverage_so_far"] == 0.0


async def test_a_day_that_has_not_begun_reports_no_coverage_rather_than_zero(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """At exactly local midnight there is nothing yet to have covered."""
    now = at(NORMAL_DAY, 0, 0)
    learning = (
        await _diagnostics(hass, setup_integration, now, [empty_day(NORMAL_DAY)])
    )["learning"]

    assert learning["current_day"]["elapsed_intervals"] == 0
    # None, not 0.0: no coverage has been *missed*, none has been achieved.
    assert learning["current_day"]["measured_coverage_so_far"] is None
    assert learning["measured_coverage"] is None


# -- the score itself was never affected -------------------------------------


def test_confidence_coverage_was_never_measured_against_the_running_day() -> None:
    """The defect was confined to diagnostics, and this pins that.

    ``compute_confidence`` is handed learned days only, and a learned day is by
    definition finalised, so its denominator was always the full civil length.
    Fixing the reported coverage therefore does not move the score -- which is
    the honest outcome, not a convenient one.
    """
    reference = NORMAL_DAY + timedelta(days=1)
    learned = [
        flat_day(reference - timedelta(days=offset), 10.0) for offset in range(1, 6)
    ]

    breakdown = compute_confidence(learned, reference, balance_score=1.0)

    assert breakdown.learned_days == 5
    assert breakdown.coverage == 1.0
    assert breakdown.measured_coverage == 1.0


def test_a_partly_covered_finalised_day_still_lowers_confidence_coverage() -> None:
    """Real incompleteness must keep costing what it always cost."""
    reference = NORMAL_DAY + timedelta(days=1)
    learned = [
        flat_day(reference - timedelta(days=1), 10.0, accepted_intervals=80),
        flat_day(reference - timedelta(days=2), 10.0),
    ]

    breakdown = compute_confidence(learned, reference, balance_score=1.0)

    assert breakdown.coverage == pytest.approx((80 + 96) / 192, abs=1e-4)
