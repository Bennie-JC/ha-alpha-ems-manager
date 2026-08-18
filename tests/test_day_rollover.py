"""What the model does when the in-progress day becomes history.

This freezes the behaviour a live installation was validated against on
2026-08-18, so the numbers a manual check reads are pinned rather than merely
observed. The live state was:

* 2026-08-16 (Sunday) -- installed during the evening, 19 valid intervals;
* 2026-08-17 (Monday) -- the first complete day, 96 intervals, the only learned one;
* 2026-08-18 (Tuesday) -- in progress, 45 intervals by mid-morning.

Diagnostics reported ``modelled_intervals: 19`` for both today and tomorrow, and
withheld both forecasts with ``insufficient_baseline_coverage``. All of that is
correct, and none of it had a test asserting the actual figures -- only that they
were greater than zero and below the publication bar. The value 19 is not
arbitrary: it is exactly the number of behavioural slots observed on *both* prior
days, since a look-back window needs ``MIN_OBSERVATIONS_PER_WINDOW`` of them.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    MIN_DAY_COMPLETENESS,
    MIN_DAYS_FOR_DAY_TYPE,
    MIN_OBSERVATIONS_PER_WINDOW,
)
from custom_components.alpha_ems_manager.forecast import (
    REASON_INSUFFICIENT_COVERAGE,
    build_forecast,
)
from custom_components.alpha_ems_manager.storage import day_type_of

from .synthetic import TZ, empty_day
from .test_forecast_publication import (
    FIRST_FULL_DAY,
    INSTALL_DAY,
    INSTALL_FIRST_INTERVAL,
    TODAY,
    complete_day,
    install_evening_day,
    live_records,
)

#: The day after the live snapshot: a Wednesday, so also a weekday.
TOMORROW = TODAY + timedelta(days=1)

#: The observed value the live system reported, and the arithmetic behind it.
LIVE_MODELLED_INTERVALS = 19


def partial_today(intervals: int):
    """Return 2026-08-18 with ``intervals`` valid intervals from midnight."""
    record = empty_day(TODAY, TZ)
    for index in range(intervals):
        record.record_interval(index, measured_kwh=0.2, ev_kwh=0.0, ev_expected=True)
    return record


# -- the day types the scenario depends on ------------------------------------


def test_the_scenario_day_types_are_what_the_analysis_assumed() -> None:
    """Everything downstream turns on 08-16 being the only weekend day."""
    assert day_type_of(INSTALL_DAY) == "weekend"  # Sunday
    assert day_type_of(FIRST_FULL_DAY) == "weekday"  # Monday
    assert day_type_of(TODAY) == "weekday"  # Tuesday
    assert day_type_of(TOMORROW) == "weekday"  # Wednesday


# -- why there are exactly 19 -------------------------------------------------


def test_the_modelled_interval_count_is_exactly_nineteen() -> None:
    """The live figure, pinned. Previously only ``> 0`` was asserted."""
    forecast = build_forecast(live_records(), TODAY, TODAY, TZ)

    assert forecast.modelled_intervals == LIVE_MODELLED_INTERVALS
    assert forecast.interval_count == 96
    assert forecast.usable_days == 2
    assert forecast.source_days == 0
    assert forecast.available is False
    assert forecast.unavailable_reason == REASON_INSUFFICIENT_COVERAGE


def test_nineteen_is_the_overlap_of_the_two_prior_days() -> None:
    """It is a set intersection, not a coincidence.

    A behavioural slot needs ``MIN_OBSERVATIONS_PER_WINDOW`` observations before
    any window will blend it. With two prior days that means the slot must occur
    on *both*, so the modelled set is their intersection -- the evening slots the
    install day managed to record.
    """
    install = install_evening_day()
    full = complete_day()

    install_slots = {
        index
        for index in range(install.interval_count)
        if install.baseline_at(index) is not None
    }
    full_slots = {
        index
        for index in range(full.interval_count)
        if full.baseline_at(index) is not None
    }
    overlap = install_slots & full_slots

    assert MIN_OBSERVATIONS_PER_WINDOW == 2
    assert overlap == set(range(INSTALL_FIRST_INTERVAL, 96))
    assert len(overlap) == LIVE_MODELLED_INTERVALS
    # Those are the late-evening slots, 19:15 onwards.
    assert min(overlap) == 77
    assert max(overlap) == 95

    forecast = build_forecast(live_records(), TODAY, TODAY, TZ)
    blended = {
        index for index, value in enumerate(forecast.intervals) if value is not None
    }
    assert blended == overlap


def test_the_publication_bar_is_what_withholds_it() -> None:
    """19 against a bar of 76.8 -- and the exact float matters."""
    required = 96 * MIN_DAY_COMPLETENESS

    assert required == pytest.approx(76.8)
    assert required > LIVE_MODELLED_INTERVALS
    # 76 is still short, 77 is enough. The boundary is asserted, not assumed.
    assert required > 76
    assert required < 77


# -- determinism within the active day ----------------------------------------


def test_the_modelled_count_cannot_change_during_the_active_day() -> None:
    """Rebuilding all day gives the same answer, however much today accrues.

    Only days strictly before the reference are inputs, and those are frozen once
    midnight passes. So a user watching diagnostics through the day should expect
    19 to sit still -- it is not a counter that creeps up as today fills in.
    """
    counts = set()
    for elapsed in (0, 1, 24, 45, 60, 90, 96):
        records = [install_evening_day(), complete_day(), partial_today(elapsed)]
        forecast = build_forecast(records, TODAY, TODAY, TZ)
        counts.add(forecast.modelled_intervals)

    assert counts == {LIVE_MODELLED_INTERVALS}


def test_today_and_tomorrow_are_modelled_identically_before_the_rollover() -> None:
    """Both targets are weekdays drawing on the same history."""
    today = build_forecast(live_records(), TODAY, TODAY, TZ)
    tomorrow = build_forecast(live_records(), TODAY, TOMORROW, TZ)

    assert tomorrow.modelled_intervals == today.modelled_intervals
    assert tomorrow.available is today.available
    assert tomorrow.unavailable_reason == today.unavailable_reason


def test_the_in_progress_day_is_never_its_own_input() -> None:
    """Today's own intervals must not train the forecast for today.

    Asserted structurally: a today record stuffed with a wildly different profile
    changes nothing about the forecast built for that same day.
    """
    quiet = build_forecast(
        [install_evening_day(), complete_day(), partial_today(0)], TODAY, TODAY, TZ
    )
    busy_today = empty_day(TODAY, TZ)
    for index in range(96):
        busy_today.record_interval(
            index, measured_kwh=9.9, ev_kwh=0.0, ev_expected=True
        )
    busy = build_forecast(
        [install_evening_day(), complete_day(), busy_today], TODAY, TODAY, TZ
    )

    assert busy.intervals == quiet.intervals
    assert busy.modelled_intervals == quiet.modelled_intervals
    assert busy.usable_days == quiet.usable_days


# -- after the rollover -------------------------------------------------------


def completed_today(intervals: int = 96):
    """Return 2026-08-18 as it will be stored once the day is over."""
    return partial_today(intervals)


def rolled_over(intervals: int = 96):
    """Return the history as it stands on 2026-08-19."""
    return [install_evening_day(), complete_day(), completed_today(intervals)]


def test_a_completed_day_becomes_eligible_history() -> None:
    """The whole point of tonight's rollover.

    Once 08-18 is in the past, the two complete weekdays cover every behavioural
    slot twice, so the modelled set jumps from the 19 evening slots to the whole
    day and both forecasts publish.
    """
    today = build_forecast(rolled_over(), TOMORROW, TOMORROW, TZ)
    tomorrow = build_forecast(rolled_over(), TOMORROW, TOMORROW + timedelta(days=1), TZ)

    for forecast in (today, tomorrow):
        assert forecast.modelled_intervals == 96
        assert forecast.available is True
        assert forecast.unavailable_reason is None
        assert forecast.usable_days == 3
        assert forecast.source_days == 2
        assert forecast.total_kwh is not None


def test_the_day_type_split_engages_once_there_are_two_weekdays() -> None:
    """Two same-type days is exactly the documented threshold."""
    assert MIN_DAYS_FOR_DAY_TYPE == 2

    before = build_forecast(live_records(), TODAY, TODAY, TZ)
    after = build_forecast(rolled_over(), TOMORROW, TOMORROW, TZ)

    # One weekday in history: pooled, because one Monday is not "weekdays".
    assert before.day_type_pooled is True
    # Two weekdays: the split is trusted, and Sunday stops contributing.
    assert after.day_type_pooled is False


def test_availability_needs_fewer_intervals_than_the_publication_bar_suggests() -> None:
    """A result worth knowing before reading tonight's numbers.

    The bar is 76.8 modelled intervals out of 96, but 08-18 does not have to
    supply all of them: the install evening already pairs 19 slots with 08-17. So
    the forecast can publish on rather less than a complete day -- and in that
    band it publishes with ``model_days: 1``, because 08-18 itself is not learned.
    Reading ``model_days: 1`` tomorrow is therefore not evidence of a fault.
    """
    outcomes = {}
    for intervals in (40, 57, 58, 76, 77, 96):
        forecast = build_forecast(rolled_over(intervals), TOMORROW, TOMORROW, TZ)
        outcomes[intervals] = (forecast.available, forecast.source_days)

    assert outcomes[40] == (False, 0)
    assert outcomes[57] == (False, 0)
    # From here the union clears the bar, but 08-18 is still not a learned day.
    assert outcomes[58] == (True, 1)
    assert outcomes[76] == (True, 1)
    # 77/96 = 80.2 %, so 08-18 now counts as learned too.
    assert outcomes[77] == (True, 2)
    assert outcomes[96] == (True, 2)


def dead_ev_day(valid_intervals: int):
    """Return 2026-08-18 with the flexible-load sensor dead after N intervals.

    Measured house load is perfect throughout; only the EV reading stops. That
    invalidates the *baseline* for the affected intervals without discarding the
    measured ground truth, which is the behaviour the EV exclusion exists for.
    """
    record = empty_day(TODAY, TZ)
    for index in range(96):
        record.record_interval(
            index,
            measured_kwh=0.2,
            ev_kwh=0.0 if index < valid_intervals else None,
            ev_expected=True,
        )
    return record


def test_a_dead_flexible_load_keeps_measured_history_but_not_learned_status() -> None:
    """The two quantities must part company, and they do."""
    record = dead_ev_day(40)

    assert record.measured_valid_count == 96
    assert record.baseline_valid_count == 40
    assert record.is_learned is False


def test_a_badly_broken_flexible_load_withholds_the_forecast_entirely() -> None:
    """With most of the day's baseline invalid there is nothing to publish.

    58 % of the day carrying no usable baseline is not a day the model can stand
    on, and ``model_days`` must not claim it. Withholding is the correct answer.
    """
    forecast = build_forecast(
        [install_evening_day(), complete_day(), dead_ev_day(40)],
        TOMORROW,
        TOMORROW,
        TZ,
    )

    # Slots 0..39 from 08-17 plus the broken day, slots 77..95 from 08-17 plus
    # the install evening: 59 in total, still short of the 76.8 bar.
    assert forecast.modelled_intervals == 59
    assert forecast.available is False
    assert forecast.unavailable_reason == REASON_INSUFFICIENT_COVERAGE
    assert forecast.source_days == 0, "the broken day must not be claimed"


def test_an_overnight_flexible_load_outage_still_publishes_honestly() -> None:
    """A smaller outage publishes, but says only one day backs the model.

    This is the realistic case -- a charger that reports ``unavailable`` while
    idle -- and the honest reporting of it is ``model_days: 1``: the forecast is
    usable, and the day whose EV sensor failed is not counted as learned.
    """
    forecast = build_forecast(
        [install_evening_day(), complete_day(), dead_ev_day(72)],
        TOMORROW,
        TOMORROW,
        TZ,
    )

    assert dead_ev_day(72).is_learned is False  # 75 % < 80 %
    assert forecast.available is True
    assert forecast.source_days == 1
    assert forecast.usable_days == 3


def test_the_forecast_target_advances_with_the_reference_day() -> None:
    """A forecast built on 08-19 is for 08-19, sized to that civil day."""
    forecast = build_forecast(rolled_over(), TOMORROW, TOMORROW, TZ)

    assert forecast.day == TOMORROW
    assert forecast.day == date(2026, 8, 19)
    assert forecast.interval_count == 96
    assert forecast.day_type == "weekday"
