"""All three phases as one integration, traced along the real data path.

Each phase has its own suite proving it right in isolation. This file walks the
whole chain in one go and checks the boundaries between the parts, because that
is where an integration of three layers actually breaks:

    entities -> normalisation -> Phase-1 storage -> baseline -> Phase-2 forecast
    -> evidence and scoring -> the public interface -> Phase-3 state -> policy
    -> the clamp -> simulation -> coordinator data -> sensors -> diagnostics

Units, signs, ``None`` semantics, interval identity, day identity and failure
isolation are checked at each hand-off. The one rule that outranks the rest:
Phase 3 is additive, so nothing it does may move a Phase-1 or Phase-2 figure.
"""

from __future__ import annotations

import math
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager import api
from custom_components.alpha_ems_manager.const import (
    ACTION_DISCHARGE,
    FLAG_DEFINITION_CHANGED,
    FORECAST_MIN_INTERVALS_FOR_METRIC,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import BATTERY_SOC, EV_POWER, set_sensor
from .forecast_helpers import (
    NORMAL,
    frozen,
    history_before,
    local,
    refresh_at,
    reseed,
    seed,
)
from .synthetic import empty_day, flat_day

pytestmark = pytest.mark.usefixtures("setup_integration")

DAY_ONE = NORMAL
DAY_TWO = NORMAL + timedelta(days=1)
DAY_THREE = NORMAL + timedelta(days=2)

LEARNING_DAYS = "sensor.alpha_ems_learning_days"
CONFIDENCE = "sensor.alpha_ems_learning_confidence"
TODAY = "sensor.alpha_ems_expected_house_load_today"
TOMORROW = "sensor.alpha_ems_expected_house_load_tomorrow"
ERROR_YESTERDAY = "sensor.alpha_ems_forecast_error_yesterday"
ERROR_WINDOW = "sensor.alpha_ems_forecast_error_7_days"
RECOMMENDATION = "sensor.alpha_ems_battery_recommendation"
PLANNED_POWER = "sensor.alpha_ems_planned_battery_power"
USABLE_ENERGY = "sensor.alpha_ems_usable_battery_energy"

PHASE_ONE = (TODAY, TOMORROW, CONFIDENCE, LEARNING_DAYS)
PHASE_TWO = (ERROR_YESTERDAY, ERROR_WINDOW)
PHASE_THREE = (RECOMMENDATION, PLANNED_POWER, USABLE_ENERGY)


def states_of(hass: HomeAssistant, entity_ids) -> dict[str, str]:
    """Return the current state of several entities."""
    return {entity_id: hass.states.get(entity_id).state for entity_id in entity_ids}


# -- the whole chain, once --------------------------------------------------


async def test_one_refresh_carries_a_measurement_all_the_way_to_a_recommendation(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Every hand-off in the chain, checked in the order it happens.

    Six identical 12 kWh days behind a 10 kWh battery at 55 %, above a 20 %
    floor. Every figure is derived by hand, so a change anywhere in the chain
    that kept the layers self-consistent while moving the numbers still fails.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    data = coordinator.data

    # Phase 1: six learned days, each 12 kWh of baseline.
    assert hass.states.get(LEARNING_DAYS).state == "6"
    assert data["learning_days"] == 6
    assert len(coordinator.store.days) == 6

    # Phase 1 -> Phase 2: the unadapted baseline is 0.125 kWh per interval.
    baseline = data["today_baseline"]
    assert baseline.available is True
    assert baseline.interval_count == 96
    assert baseline.intervals[0] == pytest.approx(0.125)
    assert baseline.total_kwh == pytest.approx(12.0)

    # Phase 2 -> the public interface: the same arrays, frozen and copied.
    published = api.current_forecast(coordinator, DAY_ONE)
    assert published is not None
    assert published.available is True
    assert published.intervals == tuple(baseline.intervals)
    assert isinstance(published.intervals, tuple)
    assert published.tz_key == "Europe/Amsterdam"

    # The public interface -> Phase 3: the plan planned against that forecast.
    plan = coordinator.battery_plan
    assert plan is not None
    assert plan.forecast["today"]["available"] is True
    assert plan.forecast["today"]["total_kwh"] == pytest.approx(12.0)

    # Phase 3 state: 55 % of 10 kWh is 5.5 kWh DC, 3.5 above a 20 % floor.
    assert plan.state is not None
    # Derived from the stored energy, so it carries one float round trip; the
    # published figure is rounded to a decimal the sensor actually has.
    assert plan.state.soc_percent == pytest.approx(55.0, abs=1e-9)
    assert plan.state.energy_kwh == 5.5
    assert plan.state.usable_energy_kwh == 3.5
    assert plan.usable_energy_kwh == pytest.approx(3.5 * math.sqrt(0.9))

    # Policy -> clamp: the interval's 0.125 kWh is 0.5 kW, well inside 5 kW.
    assert plan.decision.action == ACTION_DISCHARGE
    assert plan.decision.request.power_kw == pytest.approx(0.5)
    assert plan.decision.allowed_energy_ac_kwh == pytest.approx(0.125)
    assert plan.decision.constraints == ()

    # Simulation: the hold reference and the candidate, over today and tomorrow.
    assert plan.reference is not None
    assert plan.candidate is not None
    assert plan.candidate.intervals == plan.reference.intervals
    assert plan.candidate.grid_import_kwh < plan.reference.grid_import_kwh

    # Coordinator data -> sensors.
    assert hass.states.get(RECOMMENDATION).state == ACTION_DISCHARGE
    assert float(hass.states.get(PLANNED_POWER).state) == -0.5
    assert float(hass.states.get(USABLE_ENERGY).state) == 3.32

    # Sensors -> diagnostics, which must not be able to disagree with them.
    with frozen(local(DAY_ONE, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    battery = payload["battery_plan"]["published"]
    assert battery["battery_recommendation"] == hass.states.get(RECOMMENDATION).state
    assert battery["usable_battery_energy_kwh"] == float(
        hass.states.get(USABLE_ENERGY).state
    )


async def test_phase_three_reads_the_unadapted_forecast_and_not_the_dashboard_figure(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The Today entity is part measurement, and a planner must not eat it.

    ``forecast_total_kwh`` blends the energy already measured today into its
    total, which is useful to a person and wrong as a planning input. The plan
    has to see the model's own prediction, and this is the assertion that says
    the two are different numbers.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    # A day already half measured, so the adapted figure and the model diverge.
    reseed(
        coordinator,
        {
            **history_before(DAY_ONE),
            DAY_ONE: flat_day(DAY_ONE, 6.0, accepted_intervals=48),
        },
    )
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    adapted = coordinator.data["today"]
    baseline = coordinator.data["today_baseline"]
    plan = coordinator.battery_plan

    assert adapted.forecast_total_kwh != pytest.approx(baseline.total_kwh)
    assert plan is not None
    assert plan.forecast["today"]["total_kwh"] == pytest.approx(baseline.total_kwh)


async def test_the_units_and_signs_survive_every_boundary(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Watts in, kilowatt-hours stored, kilowatts published, once each.

    The battery power fixture is -664 W, which under the configured convention
    means *charging*. Phase 1 resolves that into non-negative flows; Phase 3
    reports it positive-for-charging; and the plan's own planned power is
    negative because the plan is to discharge. Three different signs, each
    correct, and none of them derived from another.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    flows = coordinator.read_flows()
    assert flows.battery_charge_w == 664.0
    assert flows.battery_discharge_w == 0.0

    with frozen(local(DAY_ONE, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert payload["battery_plan"]["inputs"]["battery_power_w"] == 664.0
    assert float(hass.states.get(PLANNED_POWER).state) == -0.5
    assert hass.states.get(PLANNED_POWER).attributes["unit_of_measurement"] == "kW"
    assert hass.states.get(USABLE_ENERGY).attributes["unit_of_measurement"] == "kWh"


async def test_the_battery_power_never_redefines_the_stored_energy(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The state of charge is the only source of stored energy.

    A charging battery reading 4 kW and one reading nothing must give the same
    stored energy, because the state of charge did not change. Letting an
    instantaneous power move the energy would be the one shortcut that makes the
    whole model untrustworthy.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    before = hass.states.get(USABLE_ENERGY).state

    from .conftest import BATTERY_POWER

    set_sensor(hass, BATTERY_POWER, -4000, "W", "power")
    await refresh_at(coordinator, local(DAY_ONE, 12, 20))

    assert hass.states.get(USABLE_ENERGY).state == before


# -- Phase 3 disturbs nothing ----------------------------------------------


async def test_the_six_existing_sensors_read_the_same_with_and_without_a_battery(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Phase 3 is additive, asserted at the surface a user reads.

    Driven twice at the same instant, once with the planning figures configured
    and once with them removed, so nothing but the battery configuration differs.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    configured = states_of(hass, PHASE_ONE + PHASE_TWO)
    assert hass.states.get(RECOMMENDATION).state == ACTION_DISCHARGE

    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(
        **{**fields, "battery_capacity_kwh": None}
    )
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    assert states_of(hass, PHASE_ONE + PHASE_TWO) == configured
    assert hass.states.get(RECOMMENDATION).state == "unknown"


async def test_phase_two_scoring_is_unchanged_by_the_battery_layer(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A scoreable day still scores, at exactly the value it always did.

    Predicted 12.0 kWh against a measured 9.6, so the signed error is +2.4 -- the
    same figure the Phase-2 suite pins, re-asserted here with a battery layer
    running alongside it.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    yesterday = hass.states.get(ERROR_YESTERDAY)
    assert float(yesterday.state) == 2.4
    assert yesterday.attributes["intervals_compared"] == 96
    assert yesterday.attributes["predicted_kwh"] == 12.0
    assert yesterday.attributes["actual_kwh"] == 9.6
    row = coordinator.history.days[DAY_ONE]
    assert row.summary["fg"] == []
    assert row.summary["mr"] == 2


async def test_a_legitimately_excluded_day_is_still_excluded(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The flexible-load definition changing mid-day, with Phase 3 present."""
    coordinator = setup_integration.runtime_data
    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(
        **{**fields, "ev_power_entity": EV_POWER}
    )
    set_sensor(hass, EV_POWER, 0, "W", "power")

    day = empty_day(DAY_ONE)
    for index in range(day.interval_count):
        day.record_interval(
            index,
            measured_kwh=0.125,
            ev_kwh=0.0 if index >= 48 else None,
            ev_expected=index >= 48,
        )

    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: day})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.flags == (FLAG_DEFINITION_CHANGED,)
    assert hass.states.get(ERROR_YESTERDAY).state == "unknown"


async def test_a_day_with_a_gap_is_still_not_excluded(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The beta.6 fix, re-asserted with a battery layer running.

    A restart gap on a day with a configured charger is a data gap and not a
    change of definition. This is the regression that took a release to find, so
    it is worth re-proving at every subsequent one.
    """
    coordinator = setup_integration.runtime_data
    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(
        **{**fields, "ev_power_entity": EV_POWER}
    )
    set_sensor(hass, EV_POWER, 0, "W", "power")

    day = empty_day(DAY_ONE)
    for index in range(day.interval_count):
        if index in {48, 49}:
            continue
        day.record_interval(index, measured_kwh=0.125, ev_kwh=0.0, ev_expected=True)

    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: day})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.flags == ()
    assert hass.states.get(ERROR_YESTERDAY).attributes["intervals_compared"] == 94


async def test_insufficient_evidence_still_withholds_the_rolling_rate(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One scored day is not a week, and the sample size is reported honestly."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    window = hass.states.get(ERROR_WINDOW)
    assert window.state == "unknown"
    assert window.attributes["days_compared"] == 1
    assert window.attributes["intervals_compared"] == 96
    assert window.attributes["intervals_compared"] < FORECAST_MIN_INTERVALS_FOR_METRIC
    # The energies are facts and are still reported.
    assert window.attributes["predicted_kwh"] == 12.0
    assert window.attributes["actual_kwh"] == 9.6


async def test_an_unresolved_day_stays_unresolved_with_phase_three_running(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Matching is still suspended while the learning store cannot be read."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    coordinator.store.corrupt = True
    coordinator.store.days = {}
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    assert coordinator.last_record.finalization_suspended is True
    assert coordinator.history.outcome(DAY_ONE) is None
    assert hass.states.get(ERROR_YESTERDAY).state == "unknown"
    # And the battery declines too, because there is no forecast behind it --
    # but it declines for its own stated reason, not by taking anything down.
    assert hass.states.get(USABLE_ENERGY).state != "unavailable"


async def test_the_rolling_window_publishes_once_two_days_have_scored(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The full Phase-2 pipeline, end to end, with Phase 3 alongside it."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    first = flat_day(DAY_ONE, 9.6)
    reseed(coordinator, {**base, DAY_ONE: first})
    await refresh_at(coordinator, local(DAY_TWO, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: first, DAY_TWO: flat_day(DAY_TWO, 9.6)})
    await refresh_at(coordinator, local(DAY_THREE, 0, 5))

    window = hass.states.get(ERROR_WINDOW)
    assert float(window.state) == 22.5
    assert window.attributes["intervals_compared"] == 192
    assert float(hass.states.get(ERROR_YESTERDAY).state) == 1.92


# -- failure isolation, both directions ------------------------------------


async def test_a_forecast_history_failure_does_not_take_the_battery_down(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The battery needs the *forecast*, not the evidence layer behind it."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    coordinator.history.corrupt = True
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    assert hass.states.get(ERROR_YESTERDAY).state == "unknown"
    assert hass.states.get(RECOMMENDATION).state == ACTION_DISCHARGE
    assert float(hass.states.get(USABLE_ENERGY).state) == 3.32


async def test_a_battery_failure_does_not_take_the_forecast_evidence_down(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """And the other direction, which is the one that matters more."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})

    with patch(
        "custom_components.alpha_ems_manager.coordinator.build_plan",
        side_effect=RuntimeError("battery layer exploded"),
    ):
        await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    # The day was still matched and scored.
    assert coordinator.last_record.finalized == (DAY_ONE,)
    assert float(hass.states.get(ERROR_YESTERDAY).state) == 2.4
    assert hass.states.get(LEARNING_DAYS).state == "7"
    for entity_id in PHASE_THREE:
        assert hass.states.get(entity_id).state == "unknown", entity_id


async def test_an_unreadable_state_of_charge_costs_only_the_battery(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The newest source read in the project, and its blast radius."""
    coordinator = setup_integration.runtime_data
    set_sensor(hass, BATTERY_SOC, "unavailable", "%", "battery")
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    assert hass.states.get(LEARNING_DAYS).state == "6"
    assert float(hass.states.get(TODAY).state) > 0.0
    for entity_id in PHASE_THREE:
        assert hass.states.get(entity_id).state == "unknown", entity_id


# -- day identity and rollover ---------------------------------------------


async def test_a_midnight_rollover_advances_all_three_phases_together(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One refresh, three phases, one civil day -- and they agree about which."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 23, 50))
    assert coordinator.battery_plan.target_day == DAY_ONE

    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    # Phase 1 counted the finished day, Phase 2 scored it, Phase 3 replanned.
    assert hass.states.get(LEARNING_DAYS).state == "7"
    assert float(hass.states.get(ERROR_YESTERDAY).state) == 2.4
    assert coordinator.battery_plan.target_day == DAY_TWO
    assert coordinator.data["today_baseline"].day == DAY_TWO


async def test_the_plan_starts_at_the_next_whole_interval(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """So no interval it walks is a partial one and no duration is special.

    At 12:05 forty-eight intervals have closed, so the walk begins at
    forty-nine and covers the rest of today plus all of tomorrow.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    plan = coordinator.battery_plan

    assert coordinator.data["elapsed_intervals"] == 48
    assert plan.start_index == 49
    assert plan.candidate.intervals == (96 - 49) + 96


async def test_the_recommendation_is_for_the_interval_now_in_progress(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Which is a different question from the trajectory, and needs no trajectory."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    # The decision covers interval 48, the one in progress.
    assert coordinator.battery_plan.decision.request.power_kw == pytest.approx(0.5)
    # And it exists even where the trajectory does not.
    seed(coordinator, {})
    await refresh_at(coordinator, local(DAY_ONE, 12, 20))
    assert coordinator.battery_plan.candidate is None
    assert coordinator.battery_plan.decision.action is not None


@pytest.mark.parametrize(
    ("day", "intervals"),
    [
        (NORMAL, 96),
        (NORMAL.replace(month=3, day=29), 92),
        (NORMAL.replace(month=10, day=25), 100),
    ],
)
async def test_every_phase_agrees_about_the_length_of_a_civil_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry, day, intervals: int
) -> None:
    """Interval identity is shared, so a daylight-saving day cannot split them."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(day))
    await refresh_at(coordinator, local(day, 12, 5))

    assert coordinator.data["today_baseline"].interval_count == intervals
    published = api.current_forecast(coordinator, day)
    assert published.interval_count == intervals
    plan = coordinator.battery_plan
    assert plan is not None
    assert plan.candidate is not None
    # The remainder of today, plus all of tomorrow.
    remaining = intervals - plan.start_index
    assert plan.candidate.intervals == remaining + len(
        coordinator.data["tomorrow"].intervals
    )


# -- restart ---------------------------------------------------------------


async def test_a_restart_reproduces_all_nine_sensors(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Nothing is cached anywhere, so equality proves the whole chain recomputed."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))
    before = states_of(hass, PHASE_ONE + PHASE_TWO + PHASE_THREE)
    history = dict(coordinator.store.days)
    await coordinator.async_shutdown_store()

    with frozen(local(DAY_TWO, 0, 15)):
        assert await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()
    restarted = setup_integration.runtime_data
    reseed(restarted, history)
    await refresh_at(restarted, local(DAY_TWO, 0, 5))

    assert states_of(hass, PHASE_ONE + PHASE_TWO + PHASE_THREE) == before
    # And the day was not matched a second time.
    assert restarted.last_record.finalized == ()


async def test_the_whole_diagnostics_payload_stays_bounded_and_serialisable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Three phases of reporting, still small enough to attach to a bug report."""
    import json

    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    with frozen(local(DAY_TWO, 0, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    def oversized(value, path=""):
        found = []
        if isinstance(value, dict):
            for key, item in value.items():
                found += oversized(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            if len(value) > 16:
                found.append((path, len(value)))
            for index, item in enumerate(value):
                found += oversized(item, f"{path}[{index}]")
        return found

    assert oversized(payload) == []
    encoded = json.dumps(payload, allow_nan=False, default=str)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded
    assert len(encoded) < 200_000


async def test_no_phase_reads_another_phase_private_storage(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The forecast evidence stays behind its interface, at runtime as well.

    ``test_api_boundary`` proves it statically. This proves the plan actually
    survives the evidence layer being unreadable, which is the behaviour that
    static rule exists to guarantee.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    coordinator.history.corrupt = True
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    plan = coordinator.battery_plan
    assert plan is not None
    assert plan.available is True
    assert plan.candidate is not None
