"""Diagnostics and the forecast entities must agree on what has been published.

Found on a live installation two days after install. The Today entity correctly
read ``unknown`` with ``model_days: 0``, while diagnostics for the same refresh
reported ``today_total_kwh: 4.546``. One of those two had to be wrong, and a user
comparing them cannot tell which.

The withholding itself was correct: two partial days of history, only one of them
complete enough to count as learned, is not enough to model a day. What leaked was
that ``DayForecast.remaining_kwh()`` sums whatever intervals happened to blend and
does **not** consult ``available``. ``adapt_today()`` calls it unconditionally, so
an intentionally unpublishable baseline still produced a confident-looking day
total for anything reading ``TodayForecast`` directly.

These tests reconstruct that exact state and pin both halves: the entity stays
unavailable, and nothing downstream invents a total for it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    MIN_DAY_COMPLETENESS,
    MIN_OBSERVATIONS_PER_WINDOW,
)
from custom_components.alpha_ems_manager.forecast import adapt_today, build_forecast

from .synthetic import TZ, empty_day

#: The live dates, kept so the scenario stays recognisable against the report.
INSTALL_DAY = date(2026, 8, 16)  # partial: integration added during the evening
FIRST_FULL_DAY = date(2026, 8, 17)  # the single day that qualifies as learned
TODAY = date(2026, 8, 18)  # a Tuesday, a few intervals in

#: Install happened around 19:00, so only the evening of day one was measured.
INSTALL_FIRST_INTERVAL = 77
EVENING_KWH = 0.21
NIGHT_KWH = 0.08
DAY_KWH = 0.16
#: Two quarters measured by 00:30, matching the reported actual_so_far of 0.48.
TODAY_INTERVALS = 2
TODAY_KWH_PER_INTERVAL = 0.24


def _value_for(index: int) -> float:
    """Return a plausible baseline for one interval of a domestic day."""
    if index >= INSTALL_FIRST_INTERVAL:
        return EVENING_KWH
    if index < 24:
        return NIGHT_KWH
    return DAY_KWH


def install_evening_day():
    """Return the partial first day: evening only, far below completeness."""
    record = empty_day(INSTALL_DAY, TZ)
    for index in range(INSTALL_FIRST_INTERVAL, record.interval_count):
        record.record_interval(
            index, measured_kwh=EVENING_KWH, ev_kwh=0.0, ev_expected=True
        )
    return record


def complete_day():
    """Return the one day complete enough to count as learned."""
    record = empty_day(FIRST_FULL_DAY, TZ)
    for index in range(record.interval_count):
        record.record_interval(
            index, measured_kwh=_value_for(index), ev_kwh=0.0, ev_expected=True
        )
    return record


def today_so_far():
    """Return the in-progress day with two quarters measured."""
    record = empty_day(TODAY, TZ)
    for index in range(TODAY_INTERVALS):
        record.record_interval(
            index, measured_kwh=TODAY_KWH_PER_INTERVAL, ev_kwh=0.0, ev_expected=True
        )
    return record


def live_records():
    """Return the three retained days exactly as the live system held them."""
    return [install_evening_day(), complete_day(), today_so_far()]


def build_today():
    """Return today's baseline forecast, as the coordinator builds it."""
    return build_forecast(live_records(), TODAY, TODAY, TZ)


def adapt(baseline):
    """Adapt ``baseline`` with today's two measured quarters."""
    record = today_so_far()
    measured = [record.baseline_at(i) for i in range(record.interval_count)]
    return adapt_today(
        baseline,
        measured,
        record.baseline_total_kwh,
        elapsed_intervals=TODAY_INTERVALS,
    )


# -- the reproduced live state ------------------------------------------------


def test_the_scenario_matches_the_reported_live_state() -> None:
    """Guard the fixture: this really is the situation that was reported."""
    records = live_records()
    learned = [record for record in records if record.is_learned]

    assert len(records) == 3
    assert len(learned) == 1  # learned_days: 1
    assert learned[0].day == FIRST_FULL_DAY
    # measured_valid_intervals across the three retained days, ~117 live.
    total_valid = sum(record.measured_valid_count for record in records)
    assert 110 <= total_valid <= 125
    assert today_so_far().baseline_total_kwh == pytest.approx(0.48)


def test_today_is_correctly_withheld() -> None:
    """Withholding is right: too little of the day can be modelled.

    Only the evening slots occur on both prior days, so only they reach the
    two-observation minimum. That is far below the completeness a publishable
    forecast requires, and an `unknown` state is the honest answer.
    """
    baseline = build_today()
    modelled = sum(1 for value in baseline.intervals if value is not None)

    assert not baseline.available
    assert baseline.source_days == 0  # model_days: 0
    assert baseline.total_kwh is None
    # Some intervals did blend -- that is what leaked downstream.
    assert modelled > 0
    assert modelled < baseline.interval_count * MIN_DAY_COMPLETENESS


def test_the_entity_and_diagnostics_agree_that_today_is_unavailable() -> None:
    """The regression: an unavailable baseline must not yield a day total.

    Against v1.0.0-beta.1 this fails with ``forecast_total_kwh`` around 4.5 kWh,
    which is exactly the figure diagnostics reported while the entity showed
    ``unknown``.
    """
    baseline = build_today()
    today = adapt(baseline)

    assert not baseline.available
    assert today.forecast_total_kwh is None
    assert today.forecast_remaining_kwh is None
    # Measured energy is still real and must keep flowing.
    assert today.actual_so_far_kwh == pytest.approx(0.48)


def test_no_adaptation_is_claimed_for_an_unavailable_baseline() -> None:
    """Adaptation against a forecast that does not exist is meaningless."""
    today = adapt(build_today())

    assert today.adapted is False
    assert today.adaptation_ratio == pytest.approx(1.0)


def test_the_unavailable_reason_names_the_actual_cause() -> None:
    """A live diagnosis must not require reading the source."""
    baseline = build_today()

    assert baseline.unavailable_reason == "insufficient_baseline_coverage"
    assert baseline.usable_days == 2  # two prior days contributed observations
    assert baseline.modelled_intervals > 0


# -- tomorrow: intentionally unavailable, not a bug ---------------------------


def test_tomorrow_is_intentionally_unavailable_with_this_history() -> None:
    """Tomorrow is correct as it stands, and must stay that way.

    The same two prior days back it, so it fails the same completeness test. It
    is not a separate defect and must not be "fixed" into a number.
    """
    tomorrow = build_forecast(live_records(), TODAY, TODAY + timedelta(days=1), TZ)

    assert not tomorrow.available
    assert tomorrow.total_kwh is None
    assert tomorrow.source_days == 0
    assert tomorrow.unavailable_reason == "insufficient_baseline_coverage"


def test_a_single_prior_day_can_never_satisfy_the_observation_minimum() -> None:
    """One day of history cannot model anything, by design.

    Every behavioural slot would carry exactly one observation, and a window
    needs at least ``MIN_OBSERVATIONS_PER_WINDOW``. This is the safeguard that
    stops a first-day forecast being a copy of yesterday.
    """
    assert MIN_OBSERVATIONS_PER_WINDOW >= 2

    forecast = build_forecast([complete_day()], TODAY, TODAY, TZ)

    assert not forecast.available
    assert forecast.total_kwh is None
    assert forecast.unavailable_reason == "insufficient_model_days"


def test_no_history_at_all_reports_its_own_reason() -> None:
    """Startup with an empty store is distinguishable from a modelling failure."""
    forecast = build_forecast([], TODAY, TODAY, TZ)

    assert not forecast.available
    assert forecast.unavailable_reason == "no_history"
    assert forecast.usable_days == 0


# -- the fix must not suppress a forecast the model can legitimately make ------


def test_enough_complete_days_still_publish_normally() -> None:
    """Three complete days model the whole day and must publish a total."""
    records = [
        complete_day_on(FIRST_FULL_DAY - timedelta(days=offset)) for offset in range(3)
    ]
    reference = FIRST_FULL_DAY + timedelta(days=1)

    baseline = build_forecast(records, reference, reference, TZ)

    assert baseline.available
    assert baseline.total_kwh is not None
    assert baseline.total_kwh > 0
    assert baseline.source_days == 3
    assert baseline.unavailable_reason is None


def complete_day_on(day: date):
    """Return a complete day shaped like ``complete_day`` but on ``day``."""
    record = empty_day(day, TZ)
    for index in range(record.interval_count):
        record.record_interval(
            index, measured_kwh=_value_for(index), ev_kwh=0.0, ev_expected=True
        )
    return record


async def test_diagnostics_and_the_today_entity_tell_the_same_story(
    hass, freezer, mock_config_entry
) -> None:
    """End to end, against the exact state the live system was in.

    This is the reported symptom in one assertion: the entity said `unknown`
    while the diagnostics download for the same refresh said 4.546 kWh. Whatever
    the availability rule decides, both must now report it identically.
    """
    from datetime import datetime

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    from .conftest import HOUSE_LOAD, TEST_TIMEZONE, set_sensor

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    # 00:30 local on the reported day, two quarters into it.
    freezer.move_to(datetime(2026, 8, 18, 0, 30, tzinfo=TZ))
    set_sensor(hass, HOUSE_LOAD, 960, "W", "power")

    assert isinstance(mock_config_entry, MockConfigEntry)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    # Seed the three retained days exactly as the live store held them.
    for record in live_records():
        coordinator.store.days[record.day] = record
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    forecast = payload["forecast"]
    state = hass.states.get("sensor.alpha_ems_expected_house_load_today")

    # The entity withholds, as it correctly did before.
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["forecast_total_kwh"] is None
    assert state.attributes["model_days"] == 0

    # And diagnostics no longer contradicts it. This is the regression: before
    # the fix `today_total_kwh` was a number here.
    assert forecast["today_total_kwh"] is None
    assert forecast["today_remaining_kwh"] is None
    assert forecast["today_available"] is False
    assert forecast["forecast_today"]["available"] is False
    assert (
        forecast["forecast_today"]["unavailable_reason"]
        == "insufficient_baseline_coverage"
    )
    assert forecast["forecast_today"]["model_days"] == 0
    assert forecast["forecast_today"]["usable_days"] == 2
    # Tomorrow is withheld for the same reason and says so.
    assert forecast["forecast_tomorrow"]["available"] is False
    assert forecast["forecast_tomorrow"]["unavailable_reason"] is not None
    # Learning itself is unaffected and still reports real measured energy.
    assert payload["learning"]["learned_days"] == 1


def test_a_published_forecast_still_adapts_and_reports_a_total() -> None:
    """The available path keeps working end to end."""
    records = [
        complete_day_on(FIRST_FULL_DAY - timedelta(days=offset)) for offset in range(3)
    ]
    reference = FIRST_FULL_DAY + timedelta(days=1)
    baseline = build_forecast(records, reference, reference, TZ)

    today = adapt_today(baseline, [], 0.0, elapsed_intervals=0)

    assert baseline.available
    assert today.forecast_total_kwh is not None
    assert today.forecast_remaining_kwh is not None
    assert today.forecast_total_kwh == pytest.approx(baseline.total_kwh, rel=1e-6)
