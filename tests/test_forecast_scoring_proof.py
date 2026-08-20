"""End-to-end proof of the forecast-error pipeline, at exact values.

The rest of the Phase-2 suite proves properties: that a missing actual is never
zero, that a flagged day never scores, that the sign convention holds. This file
proves *arithmetic*. Every figure below is derived by hand from the synthetic
history first and only then asserted, so a change in the pipeline that keeps all
the properties intact while moving the numbers still fails here.

The model behind every case
---------------------------

``history_before`` builds six identical flat days immediately before the target.
2026-08-19 is a Wednesday, so those six days are 13-18 August: Thu, Fri, Sat,
Sun, Mon, Tue -- four weekdays and two weekend days. Every look-back window is
nested inside the next, and no day older than seven exists, so all five windows
observe the same set and their blend collapses to the plain mean of the
day-type-matching days. That makes each prediction a one-line calculation:

* **19 Aug (Wed)**: four weekday days at 12.0 kWh -> 0.125 kWh/interval,
  12.0 kWh for the day.
* **20 Aug (Thu)**, after 19 Aug has been learned at 9.6 kWh: five weekday days,
  four at 0.125 and one at 0.100 -> 0.6/5 = 0.12 kWh/interval, 11.52 kWh.

A day of 96 intervals at 9.6 kWh is 0.1 kWh/interval, and at 14.4 kWh is
0.15 kWh/interval. Nothing rounds: every figure here is exact in binary or
exact after the pipeline's own four-decimal rounding, which is why the
assertions use ``==`` rather than a tolerance.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FORECAST_MIN_INTERVALS_FOR_METRIC,
    STATUS_FLEXIBLE_MISSING,
    STATUS_MEASURED_MISSING,
    STATUS_VALID,
)
from custom_components.alpha_ems_manager.forecast_history import (
    LIFECYCLE_VALIDATED,
    lifecycle_from_summary,
)
from custom_components.alpha_ems_manager.metrics import best_snapshot

from .conftest import EV_POWER, set_sensor
from .forecast_helpers import (
    FALL_BACK,
    NORMAL,
    SPRING_FORWARD,
    frozen,
    history_before,
    local,
    refresh_at,
    reseed,
    seed,
)
from .synthetic import empty_day, flat_day, shaped_day

pytestmark = pytest.mark.usefixtures("setup_integration")

ERROR_YESTERDAY = "sensor.alpha_ems_forecast_error_yesterday"
ERROR_WINDOW = "sensor.alpha_ems_forecast_error_7_days"

#: 19, 20 and 21 August 2026: Wednesday, Thursday, Friday.
DAY_ONE = NORMAL
DAY_TWO = NORMAL + timedelta(days=1)
DAY_THREE = NORMAL + timedelta(days=2)


async def score_one_day(coordinator, actual) -> None:
    """Issue a forecast for 19 Aug, then turn the day with ``actual`` recorded."""
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: actual})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))


def state_of(hass: HomeAssistant, entity_id: str):
    """Return one sensor's state object, asserting it exists."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state


# -- Case A: a perfect forecast is zero, and says so -------------------------


async def test_case_a_a_perfect_forecast_scores_exactly_zero(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Predicted 0.125 kWh in every interval; the house used exactly that.

    Zero is the hardest value for this pipeline to publish honestly, because it
    is also what every "no data" bug looks like. So it is asserted here as an
    exact number sitting beside a full interval count, which no missing-data
    path can produce.
    """
    coordinator = setup_integration.runtime_data
    await score_one_day(coordinator, flat_day(DAY_ONE, 12.0))

    facts = coordinator.last_record.yesterday
    assert facts == {
        "signed_error_kwh": 0.0,
        "absolute_error_kwh": 0.0,
        "mae_kwh_per_interval": 0.0,
        "predicted_kwh": 12.0,
        "actual_kwh": 12.0,
        "error_percent": 0.0,
        "intervals_compared": 96,
        "intervals_in_day": 96,
        "horizon_days": 0,
    }
    assert state_of(hass, ERROR_YESTERDAY).state == "0.0"


async def test_case_a_a_perfect_week_reports_zero_percent_not_no_percent(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Two flawless days: WAPE 0 %, MAE 0, bias 0, over 192 intervals.

    A model that is never wrong must be distinguishable from a model that was
    never measured, and the two published sensors are the only place a user can
    tell them apart.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)

    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    first = flat_day(DAY_ONE, 12.0)
    reseed(coordinator, {**base, DAY_ONE: first})
    await refresh_at(coordinator, local(DAY_TWO, 12, 5))
    second = flat_day(DAY_TWO, 12.0)
    reseed(coordinator, {**base, DAY_ONE: first, DAY_TWO: second})
    await refresh_at(coordinator, local(DAY_THREE, 0, 5))

    window = coordinator.last_record.window
    assert window.days_compared == 2
    assert window.intervals_compared == 192
    assert window.wape_percent == 0.0
    assert window.mae_kwh == 0.0
    assert window.bias_kwh == 0.0
    assert window.predicted_kwh == 24.0
    assert window.actual_kwh == 24.0
    assert state_of(hass, ERROR_WINDOW).state == "0.0"
    assert state_of(hass, ERROR_YESTERDAY).state == "0.0"


# -- Case B: over-prediction -------------------------------------------------


async def test_case_b_an_over_prediction_is_signed_positive_and_exact(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Predicted 12.0 kWh, measured 9.6 kWh: +2.4 kWh, 25 % of the actual."""
    coordinator = setup_integration.runtime_data
    await score_one_day(coordinator, flat_day(DAY_ONE, 9.6))

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["signed_error_kwh"] == 2.4
    assert facts["absolute_error_kwh"] == 2.4
    # 2.4 kWh spread over 96 intervals.
    assert facts["mae_kwh_per_interval"] == 0.025
    assert facts["predicted_kwh"] == 12.0
    assert facts["actual_kwh"] == 9.6
    assert facts["error_percent"] == 25.0
    assert facts["intervals_compared"] == 96
    assert state_of(hass, ERROR_YESTERDAY).state == "2.4"


async def test_case_b_the_rolling_figures_are_exact_over_two_days(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The whole published window, computed by hand.

    19 Aug: predicted 12.00, actual 9.6, absolute error 2.40.
    20 Aug: predicted 11.52 -- the model has learned 19 Aug, so the weekday mean
    is (4x0.125 + 0.100)/5 = 0.12 -- actual 9.6, absolute error 1.92.

    Window: 4.32 kWh of absolute error against 19.2 kWh measured = 22.5 %,
    over 192 intervals, all of it in one direction.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)

    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    first = flat_day(DAY_ONE, 9.6)
    reseed(coordinator, {**base, DAY_ONE: first})
    await refresh_at(coordinator, local(DAY_TWO, 12, 5))
    second = flat_day(DAY_TWO, 9.6)
    reseed(coordinator, {**base, DAY_ONE: first, DAY_TWO: second})
    await refresh_at(coordinator, local(DAY_THREE, 0, 5))

    assert coordinator.history.days[DAY_ONE].summary["ps"] == 12.0
    assert coordinator.history.days[DAY_ONE].summary["ae"] == 2.4
    assert coordinator.history.days[DAY_TWO].summary["ps"] == 11.52
    assert coordinator.history.days[DAY_TWO].summary["ae"] == 1.92

    window = coordinator.last_record.window
    assert window.days_compared == 2
    assert window.intervals_compared == 192
    assert window.predicted_kwh == 23.52
    assert window.actual_kwh == 19.2
    assert window.wape_percent == 22.5
    assert window.mae_kwh == pytest.approx(0.0225, abs=1e-12)
    # Positive throughout: a persistent over-prediction, not cancelling errors.
    assert window.bias_kwh == pytest.approx(0.0225, abs=1e-12)

    state = state_of(hass, ERROR_WINDOW)
    assert state.state == "22.5"
    assert state.attributes["mae_kwh_per_interval"] == 0.0225
    assert state.attributes["bias_kwh_per_interval"] == 0.0225
    assert state.attributes["predicted_kwh"] == 23.52
    assert state.attributes["actual_kwh"] == 19.2
    assert state_of(hass, ERROR_YESTERDAY).state == "1.92"


# -- Case C: under-prediction ------------------------------------------------


async def test_case_c_an_under_prediction_is_signed_negative_and_exact(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Predicted 12.0 kWh, measured 14.4 kWh: -2.4 kWh.

    The percentage is -16.67 and not -25: the denominator is what actually
    happened, so an under-prediction and an over-prediction of the same absolute
    size are deliberately not mirror images.
    """
    coordinator = setup_integration.runtime_data
    await score_one_day(coordinator, flat_day(DAY_ONE, 14.4))

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["signed_error_kwh"] == -2.4
    assert facts["absolute_error_kwh"] == 2.4
    assert facts["mae_kwh_per_interval"] == 0.025
    assert facts["predicted_kwh"] == 12.0
    assert facts["actual_kwh"] == 14.4
    assert facts["error_percent"] == -16.67
    assert state_of(hass, ERROR_YESTERDAY).state == "-2.4"


# -- Case D: missing actuals are never zero ----------------------------------


async def test_case_d_only_observed_intervals_are_compared(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Sixty intervals observed at 0.1 kWh; thirty-six never happened.

    The prediction covers the whole day, so the tempting arithmetic is
    ``12.0 - 6.0 = 6.0 kWh`` of error. That figure would be entirely
    manufactured by the outage. Only the sixty observed intervals are compared,
    against the sixty predictions that pair with them: 60 x 0.125 = 7.5 kWh
    predicted against 60 x 0.1 = 6.0 kWh measured, an error of 1.5 kWh.
    """
    coordinator = setup_integration.runtime_data
    await score_one_day(coordinator, flat_day(DAY_ONE, 9.6, accepted_intervals=60))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.status == STATUS_VALID * 60 + STATUS_MEASURED_MISSING * 36
    assert all(value is None for value in outcome.actual[60:])

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["intervals_compared"] == 60
    assert facts["intervals_in_day"] == 96
    assert facts["predicted_kwh"] == 7.5
    assert facts["actual_kwh"] == 6.0
    assert facts["signed_error_kwh"] == 1.5
    assert facts["mae_kwh_per_interval"] == 0.025
    assert state_of(hass, ERROR_YESTERDAY).state == "1.5"
    assert state_of(hass, ERROR_YESTERDAY).attributes["intervals_compared"] == 60


async def test_case_d_a_day_with_nothing_observed_publishes_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Zero observed intervals must not become a zero-kilowatt-hour error."""
    coordinator = setup_integration.runtime_data
    await score_one_day(coordinator, empty_day(DAY_ONE))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert set(outcome.status) == {STATUS_MEASURED_MISSING}
    assert coordinator.last_record.yesterday is None
    assert state_of(hass, ERROR_YESTERDAY).state == "unknown"
    assert state_of(hass, ERROR_WINDOW).state == "unknown"


# -- Case E: the flexible-load basis -----------------------------------------


async def test_case_e_both_sides_of_the_comparison_are_the_baseline(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Measured 14.4 kWh with 4.8 kWh of it flexible: the baseline is 9.6 kWh.

    ``baseline = max(measured - flexible, 0)``, and the model predicts baseline,
    so the error is 12.0 - 9.6 = +2.4 kWh. Scoring against the 14.4 kWh the
    house drew would report the model as *under*-predicting by 2.4 kWh -- the
    sign inverted by charging the model for energy it was never asked to
    predict.
    """
    coordinator = setup_integration.runtime_data
    coordinator.config = coordinator.config.__class__(
        **{
            **{
                name: getattr(coordinator.config, name)
                for name in coordinator.config.__dataclass_fields__
            },
            "ev_power_entity": EV_POWER,
        }
    )
    set_sensor(hass, EV_POWER, 0, "W", "power")

    # 0.15 kWh measured per interval, 0.05 kWh of it flexible: 0.1 kWh baseline.
    day = flat_day(DAY_ONE, 14.4, ev_kwh_per_interval=0.05, ev_expected=True)
    assert day.measured_total_kwh == 14.4
    assert day.baseline_total_kwh == 9.6

    await score_one_day(coordinator, day)

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert set(outcome.status) == {STATUS_VALID}
    assert outcome.flexible_total_kwh == 4.8

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["actual_kwh"] == 9.6
    assert facts["predicted_kwh"] == 12.0
    assert facts["signed_error_kwh"] == 2.4
    assert state_of(hass, ERROR_YESTERDAY).state == "2.4"
    assert (
        state_of(hass, ERROR_YESTERDAY)
        .attributes["comparison_basis"]
        .startswith("baseline house load")
    )


async def test_case_e_an_unreadable_charger_removes_only_its_own_intervals(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Measured energy intact, baseline undefined for eight intervals.

    Those eight are dropped from the comparison rather than scored against a
    baseline that assumed no charging: 88 x 0.125 = 11.0 kWh predicted against
    88 x 0.1 = 8.8 kWh of baseline.
    """
    coordinator = setup_integration.runtime_data
    coordinator.config = coordinator.config.__class__(
        **{
            **{
                name: getattr(coordinator.config, name)
                for name in coordinator.config.__dataclass_fields__
            },
            "ev_power_entity": EV_POWER,
        }
    )
    set_sensor(hass, EV_POWER, 0, "W", "power")

    day = empty_day(DAY_ONE)
    for index in range(day.interval_count):
        day.record_interval(
            index,
            measured_kwh=0.15,
            ev_kwh=None if index < 8 else 0.05,
            ev_expected=True,
        )
    await score_one_day(coordinator, day)

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.status == STATUS_FLEXIBLE_MISSING * 8 + STATUS_VALID * 88
    # The configuration never changed, so the day is still comparable.
    assert outcome.flags == ()

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["intervals_compared"] == 88
    assert facts["predicted_kwh"] == 11.0
    assert facts["actual_kwh"] == 8.8
    assert facts["signed_error_kwh"] == 2.2


# -- Case F: modelled versus filled provenance -------------------------------


async def test_case_f_the_split_between_modelled_and_filled_survives_scoring(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A day whose first eight intervals were never observed in the history.

    The model has no observations of those behavioural slots, so it extrapolates
    them from the nearest neighbour and records that per interval. Scoring must
    keep the two populations apart: eight extrapolated intervals and 88 blended
    ones, with their own error sums, all still recoverable after the raw arrays
    have been pruned.
    """
    coordinator = setup_integration.runtime_data
    base = {
        day: flat_day(day, 11.0, accepted_intervals=88, tz=record.tz)
        for day, record in history_before(DAY_ONE).items()
    }
    # Shift the observed window so the *first* eight slots are the unobserved
    # ones: rebuild each day with intervals 0..7 missing instead of 88..95.
    base = {}
    for offset in range(1, 7):
        day = DAY_ONE - timedelta(days=offset)
        record = empty_day(day)
        for index in range(8, record.interval_count):
            record.record_interval(
                index, measured_kwh=0.125, ev_kwh=None, ev_expected=False
            )
        base[day] = record

    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    snapshot = coordinator.history.snapshots(DAY_ONE)[0]
    assert snapshot.filled[:8] == (True,) * 8
    assert snapshot.filled[8:] == (False,) * 88
    assert sum(snapshot.filled) == 8

    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    # The reduced summary keeps the filled count and the filled error sum, so
    # "was the error concentrated in the extrapolated slots" stays answerable
    # for as long as the row itself is retained.
    summary = coordinator.history.days[DAY_ONE].summary
    assert summary["fn"] == 8
    assert summary["c"] == 96
    assert summary["fe"] == pytest.approx(8 * 0.025, abs=1e-9)
    assert summary["ae"] == pytest.approx(96 * 0.025, abs=1e-9)

    # And the deep statistics split them explicitly.
    detail = coordinator.recorder.scored_days(DAY_ONE, DAY_TWO)
    assert len(detail) == 1
    filled = [entry for entry in detail[0].scored if entry[3]]
    modelled = [entry for entry in detail[0].scored if not entry[3]]
    assert len(filled) == 8
    assert len(modelled) == 88


# -- Case J: daylight saving -------------------------------------------------


@pytest.mark.parametrize(
    ("target", "intervals", "day_kwh"),
    [(SPRING_FORWARD, 92, 11.5), (FALL_BACK, 100, 12.5)],
)
async def test_case_j_a_daylight_saving_day_is_scored_at_its_real_length(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    target: date,
    intervals: int,
    day_kwh: float,
) -> None:
    """92 and 100 intervals, matched by chronological index end to end.

    The history is six ordinary 96-interval days at 0.125 kWh per interval, and
    the actual day carries the same 0.125 kWh in every one of its own intervals.
    The whole-day totals therefore differ from 12 kWh, and differ *correctly*:

    * on the spring-forward day the 02:00 hour never happens, so four
      behavioural slots are neither predicted nor measured -- 11.5 kWh;
    * on the fall-back day the 02:00 hour happens twice, so four slots are
      predicted twice and measured twice -- 12.5 kWh.

    Both sides of the comparison agree because both are indexed
    chronologically. Matching by wall-clock slot would collapse the repeated
    hour onto itself and invent the skipped one, and the resulting error would
    look entirely plausible.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(target)
    seed(coordinator, base)
    await refresh_at(coordinator, local(target, 12, 5))

    snapshot = coordinator.history.snapshots(target)[0]
    assert snapshot.interval_count == intervals
    assert len(snapshot.predicted) == intervals
    assert snapshot.predicted == (0.125,) * intervals
    assert snapshot.total_kwh() == day_kwh

    actual = shaped_day(target, [0.125] * 100)
    assert actual.interval_count == intervals
    reseed(coordinator, {**base, target: actual})
    await refresh_at(coordinator, local(target + timedelta(days=1), 0, 5))

    outcome = coordinator.history.outcome(target)
    assert outcome is not None
    assert outcome.interval_count == intervals
    assert len(outcome.status) == intervals
    assert set(outcome.status) == {STATUS_VALID}
    assert outcome.flags == ()

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["intervals_compared"] == intervals
    assert facts["intervals_in_day"] == intervals
    assert facts["predicted_kwh"] == day_kwh
    assert facts["actual_kwh"] == day_kwh
    assert facts["signed_error_kwh"] == 0.0


# -- Case M: the first validated day, as the user meets it -------------------


async def test_case_m_the_first_scored_day_makes_yesterday_a_real_number(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One validated comparison is enough for the daily sensor, and only that.

    The rolling sensor has a documented minimum sample of
    ``FORECAST_MIN_INTERVALS_FOR_METRIC`` intervals -- roughly two full days --
    and reports its sample size honestly while it waits rather than publishing a
    figure or a zero.
    """
    coordinator = setup_integration.runtime_data
    await score_one_day(coordinator, flat_day(DAY_ONE, 9.6))

    row = coordinator.history.days[DAY_ONE]
    assert (
        lifecycle_from_summary(
            DAY_ONE,
            DAY_TWO,
            finalized=row.finalized_at is not None,
            summary=row.summary,
        )
        == LIFECYCLE_VALIDATED
    )

    yesterday = state_of(hass, ERROR_YESTERDAY)
    assert yesterday.state == "2.4"
    assert yesterday.attributes["intervals_compared"] == 96

    window = state_of(hass, ERROR_WINDOW)
    assert window.state == "unknown"
    assert window.attributes["days_compared"] == 1
    assert window.attributes["intervals_compared"] == 96
    assert window.attributes["intervals_compared"] < FORECAST_MIN_INTERVALS_FOR_METRIC
    # The sample is too small for a rate, but the two energies are facts and
    # must not be published as zeros.
    assert window.attributes["predicted_kwh"] == 12.0
    assert window.attributes["actual_kwh"] == 9.6
    assert window.attributes["mae_kwh_per_interval"] is None
    assert window.attributes["bias_kwh_per_interval"] is None


async def test_case_m_the_second_scored_day_makes_the_rolling_sensor_real(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """192 intervals is the documented threshold, and it is met exactly here."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)

    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    first = flat_day(DAY_ONE, 9.6)
    reseed(coordinator, {**base, DAY_ONE: first})
    await refresh_at(coordinator, local(DAY_TWO, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: first, DAY_TWO: flat_day(DAY_TWO, 9.6)})
    await refresh_at(coordinator, local(DAY_THREE, 0, 5))

    window = state_of(hass, ERROR_WINDOW)
    assert window.attributes["intervals_compared"] == FORECAST_MIN_INTERVALS_FOR_METRIC
    assert float(window.state) == 22.5
    assert state_of(hass, ERROR_YESTERDAY).state == "1.92"


# -- Case N: near-zero actuals -----------------------------------------------


async def test_case_n_a_near_zero_day_gets_a_percentage_only_where_it_means_something(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A day of 0.0096 kWh: the kWh error is real, the percentage is enormous.

    That percentage is *correct* -- the model predicted 1250 times what the
    house used -- and the day total is a real denominator, so it is published
    rather than suppressed. What is never computed is a per-interval percentage,
    where a 0.0001 kWh overnight actual would produce a meaningless figure that
    then dominates every average it entered.
    """
    coordinator = setup_integration.runtime_data
    await score_one_day(coordinator, flat_day(DAY_ONE, 0.0096))

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["actual_kwh"] == 0.01
    assert facts["signed_error_kwh"] == 11.99
    assert facts["error_percent"] == 124900.0
    assert facts["intervals_compared"] == 96


async def test_case_n_a_day_that_measured_nothing_at_all_has_no_percentage(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Every interval observed, every one of them exactly zero.

    The comparison is real -- 96 intervals, 12 kWh of error -- but there is no
    denominator, so the percentage is ``None`` rather than an infinity travelling
    into a sensor attribute.
    """
    coordinator = setup_integration.runtime_data
    await score_one_day(coordinator, flat_day(DAY_ONE, 0.0))

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["intervals_compared"] == 96
    assert facts["actual_kwh"] == 0.0
    assert facts["signed_error_kwh"] == 12.0
    assert facts["error_percent"] is None
    assert state_of(hass, ERROR_YESTERDAY).state == "12.0"
    assert state_of(hass, ERROR_YESTERDAY).attributes["error_percent"] is None


# -- Case O: which snapshot a day is scored against --------------------------


async def test_case_o_the_day_of_prediction_is_the_one_scored(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Two snapshots for 20 Aug: 12.0 kWh a day ahead, 11.52 kWh on the day.

    Both are kept, and both are scored in the horizon breakdown. The headline
    figure uses the lowest horizon -- the model's final word, made with the most
    history behind it -- so the published error is 11.52 - 9.6 = 1.92 and not
    12.0 - 9.6 = 2.4.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)

    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    first = flat_day(DAY_ONE, 9.6)
    reseed(coordinator, {**base, DAY_ONE: first})
    await refresh_at(coordinator, local(DAY_TWO, 12, 5))

    snapshots = coordinator.history.snapshots(DAY_TWO)
    assert [(s.horizon_days, s.total_kwh()) for s in snapshots] == [
        (1, 12.0),
        (0, 11.52),
    ]
    chosen = best_snapshot(snapshots)
    assert chosen is not None
    assert chosen.horizon_days == 0
    assert chosen.total_kwh() == 11.52

    reseed(coordinator, {**base, DAY_ONE: first, DAY_TWO: flat_day(DAY_TWO, 9.6)})
    await refresh_at(coordinator, local(DAY_THREE, 0, 5))

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["horizon_days"] == 0
    assert facts["predicted_kwh"] == 11.52
    assert facts["signed_error_kwh"] == 1.92

    # The day-ahead prediction is not discarded: it is scored separately, which
    # is what makes "is a day-ahead forecast measurably worse" answerable.
    by_horizon = coordinator.recorder.scored_days_by_horizon(DAY_ONE, DAY_THREE)
    assert sorted(by_horizon) == [0, 1]
    day_ahead = [day for day in by_horizon[1] if day.target_day == DAY_TWO]
    assert len(day_ahead) == 1
    assert day_ahead[0].predicted_kwh == 12.0
    assert day_ahead[0].signed_error_kwh == pytest.approx(2.4, abs=1e-9)


async def test_case_o_a_duplicate_refresh_cannot_change_the_scored_candidate(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Ninety-five of every ninety-six refreshes must leave the record alone."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)

    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    fingerprints = list(coordinator.history.days[DAY_ONE].fingerprints)
    for minute in (20, 35, 50):
        await refresh_at(coordinator, local(DAY_ONE, 12, minute))

    assert coordinator.history.days[DAY_ONE].fingerprints == fingerprints
    assert coordinator.recorder.duplicate_issuances == 6

    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))
    assert coordinator.last_record.yesterday["predicted_kwh"] == 12.0
    assert coordinator.last_record.yesterday["horizon_days"] == 0


# -- Case G and H: restarts either side of midnight --------------------------


async def test_case_g_a_restart_before_midnight_keeps_the_prediction(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The snapshot is on disk before the day turns, and is what gets scored.

    Reloading the entry rebuilds every in-memory structure from the documents,
    so a prediction that only existed in memory would silently be replaced by a
    freshly computed one -- scoring the day against a forecast made after it
    ended.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    await coordinator.async_shutdown_store()
    issued = coordinator.history.snapshots(DAY_ONE)[0]

    # Still the same civil day, so the reload's own first refresh must find
    # nothing to finalise and nothing new to issue.
    with frozen(local(DAY_ONE, 12, 30)):
        assert await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()
    restarted = setup_integration.runtime_data
    # The load-bearing assertion: nothing new was issued, so the prediction
    # already on disk is the one that stands.
    assert restarted.last_record.issued == ()
    # Two refreshes now happen on a reload rather than one -- the setup refresh
    # and the one beta.10 added once Home Assistant reports itself started, which
    # exists so a value read before the sources had published cannot stand for a
    # quarter of an hour. Each refresh recognises today and tomorrow as already
    # issued, so the duplicate tally is two per refresh rather than two in total.
    assert restarted.recorder.duplicate_issuances == 4

    await restarted.history.async_ensure_days([DAY_ONE])
    reloaded = restarted.history.snapshots(DAY_ONE)
    assert len(reloaded) == 1
    assert reloaded[0].fingerprint == issued.fingerprint
    assert reloaded[0].predicted == issued.predicted
    assert reloaded[0].issued_at == issued.issued_at

    reseed(restarted, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(restarted, local(DAY_TWO, 0, 5))

    assert restarted.last_record.finalized == (DAY_ONE,)
    assert restarted.last_record.yesterday["predicted_kwh"] == 12.0
    assert restarted.last_record.yesterday["signed_error_kwh"] == 2.4


async def test_case_h_a_restart_after_midnight_does_not_score_the_day_twice(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Finalisation is once per target day, and the figures are identical."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    history = {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)}
    reseed(coordinator, history)
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    first_pass = coordinator.last_record.yesterday
    finalized_at = coordinator.history.days[DAY_ONE].finalized_at
    summary = dict(coordinator.history.days[DAY_ONE].summary)
    await coordinator.async_shutdown_store()

    with frozen(local(DAY_TWO, 0, 15)):
        assert await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()
    restarted = setup_integration.runtime_data
    reseed(restarted, history)
    await refresh_at(restarted, local(DAY_TWO, 0, 20))

    # Not re-finalised, not re-timestamped, and not re-derived.
    assert restarted.last_record.finalized == ()
    assert restarted.last_record.restated == ()
    assert restarted.history.days[DAY_ONE].finalized_at == finalized_at
    assert dict(restarted.history.days[DAY_ONE].summary) == summary
    assert restarted.last_record.yesterday == first_pass
    await restarted.history.async_ensure_days([DAY_ONE])
    assert len(restarted.history.snapshots(DAY_ONE)) == 1


# -- Case I: a store that cannot be read -------------------------------------


async def test_case_i_an_unreadable_learning_store_suspends_matching(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Never write "every measurement was missing" because of a read error.

    The learning store degrades an unreadable document to an empty history so
    setup can continue, which is right for availability -- but an empty history
    means every actual reads as absent. Finalising against it would write an
    immutable record saying so, for a day whose measurements are very probably
    sitting intact on disk.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    coordinator.store.corrupt = True
    coordinator.store.days = {}
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    assert coordinator.last_record.finalization_suspended is True
    assert coordinator.last_record.finalized == ()
    assert coordinator.last_record.restated == ()
    assert coordinator.history.outcome(DAY_ONE) is None
    assert coordinator.history.is_finalized(DAY_ONE) is False
    assert coordinator.last_record.unresolved_days == 1
    assert state_of(hass, ERROR_YESTERDAY).state == "unknown"

    # The read recovers, and the day resolves normally with its real actual.
    coordinator.store.corrupt = False
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 20))

    assert coordinator.last_record.finalized == (DAY_ONE,)
    assert coordinator.last_record.yesterday["signed_error_kwh"] == 2.4
    assert coordinator.last_record.unresolved_days == 0


async def test_case_i_an_unreadable_forecast_index_publishes_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No evidence and no writes, rather than an empty history written back."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    coordinator.history.corrupt = True

    with patch.object(
        coordinator.history, "schedule_save", side_effect=AssertionError("wrote")
    ):
        await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    assert coordinator.last_record.issued == ()
    assert coordinator.last_record.yesterday is None
    assert coordinator.last_record.window.days_compared == 0
    assert coordinator.last_record.window.actual_kwh is None
    assert state_of(hass, ERROR_YESTERDAY).state == "unknown"
    assert state_of(hass, ERROR_WINDOW).state == "unknown"
    # The four Phase-1 sensors are untouched by any of it.
    assert state_of(hass, "sensor.alpha_ems_learning_days").state != "unknown"


# -- the energy-balance check cannot reach any of this -----------------------


async def test_a_failing_energy_balance_never_changes_a_score(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The balance check is a data-quality signal, and it stops at confidence.

    It is deliberately outside the fingerprint -- it resamples every sixty
    seconds and would force a snapshot per refresh -- and outside every scoring
    path. A boundary artefact between a P1 meter and an inverter must not be
    able to invalidate a forecast comparison, or to alter one.
    """
    coordinator = setup_integration.runtime_data
    actual = flat_day(DAY_ONE, 9.6)

    await score_one_day(coordinator, actual)
    healthy = coordinator.last_record.yesterday
    healthy_summary = dict(coordinator.history.days[DAY_ONE].summary)

    # Same timeline, but every balance sample fails throughout.
    coordinator.store.balance.ok_samples = 0
    coordinator.store.balance.total_samples = 400
    await score_one_day(coordinator, actual)

    assert coordinator.store.balance.score == 0.0
    assert coordinator.last_record.yesterday == healthy
    failing_summary = dict(coordinator.history.days[DAY_ONE].summary)
    # Confidence is the one figure the balance score legitimately moves, and it
    # is recorded as provenance rather than used as a gate.
    assert failing_summary.pop("cf") != healthy_summary.pop("cf")
    assert failing_summary == healthy_summary
    assert coordinator.history.outcome(DAY_ONE).flags == ()


def test_the_scoring_modules_never_import_the_balance_check() -> None:
    """A static guarantee, so the independence above cannot regress quietly."""
    from pathlib import Path

    root = (
        Path(__file__).resolve().parents[1] / "custom_components" / "alpha_ems_manager"
    )
    for name in (
        "forecast_history.py",
        "forecast_recorder.py",
        "metrics.py",
        "history_store.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "energy_balance" not in source
        assert "BalanceSample" not in source
        assert "BalanceMonitor" not in source


# -- the stored instant is absolute ------------------------------------------


def test_every_stored_instant_is_timezone_aware_utc() -> None:
    """Naive instants cannot be ordered, and local ones move under the reader."""
    from custom_components.alpha_ems_manager.forecast import DayForecast
    from custom_components.alpha_ems_manager.forecast_history import (
        ForecastSnapshot,
        build_snapshot,
    )

    forecast = DayForecast(
        day=DAY_ONE,
        day_type="weekday",
        interval_count=96,
        intervals=[0.125] * 96,
        filled=[False] * 96,
        windows_used=(7,),
        source_days=4,
        usable_days=4,
        modelled_intervals=96,
    )
    issued = datetime(2026, 8, 19, 10, 5, tzinfo=ZoneInfo("Europe/Amsterdam"))
    snapshot = build_snapshot(
        forecast,
        issued_at=issued.astimezone(UTC),
        issuance_day=DAY_ONE,
        tz_key="Europe/Amsterdam",
        learned_days=4,
        confidence_percent=17.5,
        confidence=None,
        ev_power_entity=None,
    )
    assert snapshot.issued_at.tzinfo is UTC
    assert snapshot.issued_at.isoformat().endswith("+00:00")

    rebuilt = ForecastSnapshot.from_dict(DAY_ONE, snapshot.to_dict())
    assert rebuilt is not None
    assert rebuilt.issued_at == snapshot.issued_at
