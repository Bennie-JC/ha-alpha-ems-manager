"""What Phase 8 publishes, and everything it must leave alone.

One sensor, one diagnostics section, one logbook line. The sensor's state is the
**desired** action -- what the optimizer wants -- with what implemented actuators
could achieve beside it and the reason nothing is sent beside that. Three separate
facts, deliberately: a state saying ``export`` with no way to tell whether
anything could happen would be worse than no state at all.

The other half of this file is the promise that matters more than the figure:
**nothing changed.** The recommendation, the planned power, the usable energy, the
dynamic reserve and the control state are what beta.13 published for the same
inputs, and no economic figure moves any of them.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_LOGBOOK_ENTRY
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.activity import ACTIVITY_NAME
from custom_components.alpha_ems_manager.const import (
    CONTROL_EXECUTION_AVAILABLE,
    ECONOMIC_ACTION_OPTIONS,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_BLOCKED_NOT_ENABLED,
    ECONOMIC_BUCKET_BAND_KWH,
    ECONOMIC_MODEL_VERSION,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.alpha_ems_manager.economic import TERMINAL_BASIS

from .conftest import FakeFrank
from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
from .frank_capture import synthetic_day
from .live_capability import assert_charge_only_capability
from .test_beta24_live_charge import charge_now_price

ECONOMIC_ENTITY = "sensor.alpha_ems_economic_action"
PLANNED_ENTITY = "sensor.alpha_ems_next_planned_action"

#: Exactly the eleven the contract allows, and no more. A plan with twenty ways of
#: being interesting must not unpack all twenty into an entity.
#:
#: **Rewritten for beta.35, and the shape of the change is the argument.** This
#: entity used to publish a *plan* view -- ``window``, ``energy_kwh``,
#: ``price_eur_kwh``, ``expected_net_value_eur`` -- while its state claimed to
#: describe the present. Those are different runs, and on 2026-08-29 they visibly
#: were: the state read ``idle`` beside a window, an energy and a price all
#: belonging to a sale planned for 21:00, rendered ``HH:MM-HH:MM`` with no date.
#:
#: So the plan-shaped fields moved to ``Next Planned Action``, which prints full
#: ISO instants and is the entity that can honestly carry them, and what is left
#: here all comes from the one execution the state describes: whether it is owned,
#: under which mode, for which campaign and run, what it promised, what it has
#: delivered, at what power, since when.
ECONOMIC_ATTRIBUTES = {
    "capability_action",
    "execution_blocked_reason",
    "owned",
    "mode",
    "purpose",
    "campaign_id",
    "run_id",
    "planned_kwh",
    "realised_kwh",
    "power_kw",
    "started_at",
}
CORE_ATTRIBUTES = {
    "unit_of_measurement",
    "icon",
    "friendly_name",
    "device_class",
    "options",
    "state_class",
}

#: The five figures beta.13 published. Named rather than counted, so a change to
#: any one of them is visible in a diff.
UNCHANGED = (
    "sensor.alpha_ems_battery_recommendation",
    "sensor.alpha_ems_planned_battery_power",
    "sensor.alpha_ems_usable_battery_energy",
    "sensor.alpha_ems_dynamic_battery_reserve",
    "sensor.alpha_ems_control_state",
)


async def drive(coordinator, frank: FakeFrank, *, hour: int = 12) -> None:
    """Give the model history and prices, and refresh at a fixed instant.

    Prices are what makes the economic layer available at all: without them the
    horizon is empty and the honest answer is that there is no plan. So every test
    that wants a plan takes the price fixture, and the ones that want an absence
    deliberately do not.
    """
    seed(coordinator, history_before(NORMAL))
    # beta.31: a shape that gives the plan a *reason* to act. The default sawtooth
    # carried six cents of wholesale spread and relied on the autonomy reserve to
    # make a purchase compulsory; reachability makes nothing compulsory while the
    # pack can hold its floor, so a fixture wanting a run has to justify one.
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    await refresh_at(coordinator, local(NORMAL, hour, 5))


def allow_trading(coordinator, **flags) -> None:
    """Turn the economic opt-ins on for one test.

    Both default to **off**, which is why the default fixture plans nothing: a
    battery that may neither buy nor sell, on a day whose reserve is met, has no
    economic decision to make. Tests that need advice say so here rather than
    relying on a fixture that happens to produce it.
    """
    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(**{**fields, **flags})


def attributes_of(hass: HomeAssistant, entity_id: str) -> dict:
    """Return one entity's attributes, asserting the entity exists."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return dict(state.attributes)


async def diagnostics_at_noon(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Read diagnostics on the day the refresh was driven on.

    Diagnostics reads its own clock, so a payload taken after a driven refresh
    would otherwise describe whatever day it really is -- which is how a sibling
    module's tests passed on the day they were written and failed two days later.
    """
    with patch(
        "custom_components.alpha_ems_manager.diagnostics.dt_util.now",
        return_value=local(NORMAL, 12, 5),
    ):
        return await async_get_config_entry_diagnostics(hass, entry)


# --- the entity -------------------------------------------------------------


async def test_the_state_is_one_of_the_six_declared_actions(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """An enum, so Home Assistant records it as a category rather than a number.

    No state class: a device class of ENUM permits none, and a long-term statistic
    over a category means nothing anyway.
    """
    await drive(setup_integration.runtime_data, frank)

    state = hass.states.get(ECONOMIC_ENTITY)
    attributes = attributes_of(hass, ECONOMIC_ENTITY)

    assert state.state in set(ECONOMIC_ACTION_OPTIONS)
    assert attributes["device_class"] == "enum"
    assert set(attributes["options"]) == set(ECONOMIC_ACTION_OPTIONS)
    assert "state_class" not in attributes
    assert "unit_of_measurement" not in attributes


async def test_the_economic_entity_carries_exactly_eight_attributes(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The cap, asserted as a set rather than a count.

    A count could be satisfied by swapping a useful attribute for a useless one,
    so the names are pinned. Everything else -- both plans' totals, the per-run
    detail, the counterfactuals, the solver figures and the provenance -- is in
    diagnostics.
    """
    await drive(setup_integration.runtime_data, frank)

    attributes = attributes_of(hass, ECONOMIC_ENTITY)

    assert set(attributes) - CORE_ATTRIBUTES == ECONOMIC_ATTRIBUTES
    assert len(ECONOMIC_ATTRIBUTES) == 11


async def test_the_capability_action_is_published_beside_the_desired_one(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Both, always. A desired action alone would be ambiguous about what can happen.

    ``capability_action`` is what implemented actuators could produce -- computed
    *before* the execution barrier, and therefore not a claim that anything was or
    could be executed.

    The blocked reason is the deepest one that applies. This installation has not
    enabled command sending, so that is the answer -- and it staying the answer
    across the beta.24 upgrade is the upgrade-safety property: opening the barrier
    for a charge changes nothing for someone who never opted in.
    """
    await drive(setup_integration.runtime_data, frank)

    attributes = attributes_of(hass, ECONOMIC_ENTITY)

    assert attributes["capability_action"] in set(ECONOMIC_ACTION_OPTIONS)
    assert attributes["execution_blocked_reason"] == ECONOMIC_BLOCKED_NOT_ENABLED


async def test_the_economic_entity_exposes_no_array_at_all(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Ninety-six values would be written on every state change."""
    await drive(setup_integration.runtime_data, frank)

    attributes = attributes_of(hass, ECONOMIC_ENTITY)
    for key, value in attributes.items():
        if key == "options":
            continue
        assert not isinstance(value, dict), key
        if isinstance(value, (list, tuple)):
            assert len(value) <= 8, f"{key} has {len(value)}"


async def test_no_plan_reads_unknown_rather_than_hold(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """``hold`` is the value of a decision, so it must not mean "no decision".

    A young installation has no publishable forecast, so there is no horizon to
    optimise over. The honest answer is that there is no plan -- not that the plan
    is to do nothing.
    """
    coordinator = setup_integration.runtime_data
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    state = hass.states.get(ECONOMIC_ENTITY)

    assert state.state == "unknown"


async def test_an_unavailable_plan_still_says_why_nothing_is_sent(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The barrier is reported even when there is no plan to block."""
    coordinator = setup_integration.runtime_data
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    attributes = attributes_of(hass, ECONOMIC_ENTITY)

    assert (
        attributes["execution_blocked_reason"] == ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE
    )


async def test_the_published_power_and_energy_agree_with_the_plan(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The plan view is a view of the plan, not a second calculation of it.

    **Asked of ``Next Planned Action`` since beta.35.** The figures are the same
    figures; what changed is which entity is allowed to carry them. ``Economic
    Action`` now describes the execution its state describes, and asking it for the
    plan's power was asking one entity to be two -- which is exactly the confusion
    that let ``idle`` be published beside a 21:00 sale's price and energy.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True)
    await drive(coordinator, frank)

    outcome = (coordinator.data or {}).get("economic")
    if outcome is None or not outcome.available:
        pytest.skip("no economic plan for this fixture")
    attributes = attributes_of(hass, PLANNED_ENTITY)

    from custom_components.alpha_ems_manager.sensor import _next_planned_run

    run, _target = _next_planned_run(coordinator)
    if run is None:
        assert attributes["planned_kwh"] is None
        return
    assert attributes["planned_kwh"] == pytest.approx(
        round(run.energy_kwh, 2), abs=0.005
    )
    assert attributes["power_kw"] == pytest.approx(
        round(run.first_power_kw, 2), abs=0.005
    )
    # The optimiser's rationale follows ``published_run``, so it is carried only
    # where it actually describes this run -- never borrowed for a different one.
    published = outcome.desired.published_run
    assert attributes["reason"] == (outcome.reason if published is run else None)


# --- diagnostics ------------------------------------------------------------


async def test_diagnostics_carries_the_economic_plan_section(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """One dict rather than a list entry, so the section ceiling is untouched."""
    await drive(setup_integration.runtime_data, frank)

    payload = await diagnostics_at_noon(hass, setup_integration)
    section = payload["economic_plan"]

    assert section["model_version"] == ECONOMIC_MODEL_VERSION
    assert "decides_nothing" in section
    # **Scoped to the module in beta.33.** It claimed "no service call reaches the
    # inverter" -- true of the whole integration when written, false from beta.24
    # on, and published where a user auditing safety would read it as a live
    # guarantee. What the field is actually for is still asserted: Stage A itself
    # actuates nothing, and the boundary tests check that at the syntax level.
    assert (
        "this module calls no service and names no helper" in section["decides_nothing"]
    )
    assert "never executes one" in section["decides_nothing"]
    assert "no service call reaches the inverter" not in section["decides_nothing"]


async def test_the_diagnostics_section_reports_both_plans_and_the_gap(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Desired totals, capability totals, and what the difference costs."""
    await drive(setup_integration.runtime_data, frank)

    section = (await diagnostics_at_noon(hass, setup_integration))["economic_plan"]

    assert section["available"] is True
    assert set(section["desired"]) == {
        "action",
        "reason",
        "power_kw",
        "energy_kwh",
        "price_eur_kwh",
        "totals",
    }
    # **Both of these were literals and both were false.** Alpha EMS has sent real
    # commands since beta.24, so a capability block asserting execution was
    # unavailable contradicted the control section of the same download. They now
    # read the runtime, and on this fixture the barrier is the user's own switch.
    assert section["capability"]["execution_available"] is CONTROL_EXECUTION_AVAILABLE
    assert section["capability"]["execution_available"] is True
    assert (
        section["capability"]["execution_blocked_reason"]
        == setup_integration.runtime_data.economic_blocked_reason
    )
    assert (
        section["capability"]["execution_blocked_reason"]
        != ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE
    )
    assert "economic_value_forgone_eur" in section["forgone"]


async def test_every_run_states_all_five_boundaries_separately(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A euro figure is only meaningful against the boundary it was measured at.

    This is where a reader audits that the energy-volume scheduling did what it
    claims: two battery-side figures, two grid-side ones and the curtailment, all
    on the same row as the price and the value.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await drive(coordinator, frank)

    section = (await diagnostics_at_noon(hass, setup_integration))["economic_plan"]

    assert section["available"] is True
    assert section["runs"], "the fixture produced no planned run to audit"
    assert len(section["runs"]) <= 8
    assert section["runs_total"] >= len(section["runs"])
    for run in section["runs"]:
        assert {
            "battery_charge_ac_kwh",
            "battery_discharge_ac_kwh",
            "grid_import_kwh",
            "grid_export_kwh",
            "pv_curtailed_kwh",
        } <= set(run)


async def test_the_diagnostics_section_publishes_no_per_interval_trajectory(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A hundred and ninety-two rows against a sixteen-entry ceiling.

    And a truncated one would be worse than none: it would read as a short horizon
    rather than as a clipped payload.
    """
    await drive(setup_integration.runtime_data, frank)

    section = (await diagnostics_at_noon(hass, setup_integration))["economic_plan"]

    def walk(value, path="") -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                walk(inner, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            assert len(value) <= 16, f"{path} has {len(value)}"
            for index, inner in enumerate(value):
                walk(inner, f"{path}[{index}]")

    walk(section, "economic_plan")


async def test_the_reserve_and_terminal_rules_are_stated_in_the_payload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A lexicographic order and a terminal bound both need to be readable.

    The terminal basis says ``on_bucket_grid`` because that is what is enforced:
    the hold trajectory's endpoint clamped to the same discretisation the states
    live on. Publishing the unclamped request would be publishing a bound the
    solver did not apply.
    """
    await drive(setup_integration.runtime_data, frank)

    section = (await diagnostics_at_noon(hass, setup_integration))["economic_plan"]
    if not section["available"]:
        pytest.skip("no economic plan for this fixture")

    assert (
        "shortfall can never unlock a profitable export" in section["reserve"]["rule"]
    )
    # One state-space bucket, and since beta.17 the bucket is chosen per
    # installation rather than fixed at 0.25 -- so this asserts the relationship,
    # which is the actual contract, rather than a number that now depends on the
    # configured power.
    assert (
        section["reserve"]["quantisation_margin_kwh"] == section["solver"]["bucket_kwh"]
    )
    low, high = ECONOMIC_BUCKET_BAND_KWH
    assert low <= section["solver"]["bucket_kwh"] <= high
    assert section["terminal"]["basis"] == TERMINAL_BASIS
    # Not a tautology: the basis must name the *configured floor*, because since
    # beta.18 that is what is enforced. Naming the hold trajectory here would be a
    # false statement about a number the dashboard shows.
    assert section["terminal"]["basis"] == "configured_floor_on_bucket_grid"
    assert "hold_trajectory" not in section["terminal"]["basis"]
    assert "configured physical floor" in section["terminal"]["rule"]
    # And it says what it is no longer, so a reader of an older download is
    # not left guessing which release changed underneath them.
    assert "idle trajectory" in section["terminal"]["rule"]


async def test_the_provenance_states_that_no_grid_limit_is_known(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """An advisory peak that might exceed the connection, said out loud.

    The integration has no way to learn a connection or contractual limit, so a
    reported peak is bounded by the inverter and the battery only. Executing
    nothing is what makes that safe, and the payload says so rather than implying
    a limit was respected.
    """
    await drive(setup_integration.runtime_data, frank)

    section = (await diagnostics_at_noon(hass, setup_integration))["economic_plan"]
    if not section["available"]:
        pytest.skip("no economic plan for this fixture")

    limits = section["provenance"]["grid_limits"]

    assert limits["connection_limit_kw"] is None
    assert limits["connection_limit_source"] == "unknown"
    assert limits["export_limit_honoured_by_dispatch"] is False
    assert "may exceed what the connection can carry" in limits["basis"]


async def test_the_provenance_records_the_settings_the_plan_rested_on(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A threshold the user changed would otherwise make the plan unverifiable."""
    await drive(setup_integration.runtime_data, frank)

    section = (await diagnostics_at_noon(hass, setup_integration))["economic_plan"]
    if not section["available"]:
        pytest.skip("no economic plan for this fixture")

    settings = section["provenance"]["settings"]

    assert settings["minimum_trade_gain_eur"] == 0.10
    assert settings["allow_grid_charging"] is False
    assert settings["allow_battery_export"] is False
    assert "lexicographic priority" in settings["threshold_rule"]


async def test_an_absent_plan_names_the_missing_input_rather_than_the_horizon(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """No forecast is a different failure from an empty horizon, and says so."""
    coordinator = setup_integration.runtime_data
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    section = (await diagnostics_at_noon(hass, setup_integration))["economic_plan"]

    assert section["available"] is False
    assert section["unavailable_reason"] is not None
    assert section["unavailable_reason"].startswith("economic_")


# --- the Activity surface ---------------------------------------------------


async def test_a_plan_appearing_files_one_logbook_line(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Filed against the entity as well as the domain, so it shows on its history.

    The domain alone files the line under the integration but attaches it to
    nothing, which is not where a user looking at the economic action will look.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True)
    entries: list[dict] = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: entries.append(event.data))

    await drive(coordinator, frank)
    await hass.async_block_till_done()

    assert entries, "no Activity entry was filed"
    first = entries[0]
    assert first["domain"] == "alpha_ems_manager"
    assert first["entity_id"] == ECONOMIC_ENTITY
    assert first["name"] == ACTIVITY_NAME


async def test_every_activity_line_carries_the_advisory_qualifier(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """While the barrier stands, no line may read as a claim about the battery."""
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True)
    entries: list[dict] = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: entries.append(event.data))

    await drive(coordinator, frank)
    await hass.async_block_till_done()

    assert entries, "no Activity entry was filed"
    for entry in entries:
        message = entry["message"]
        if message.startswith(("plans to", "wants to")):
            assert "Advisory only" in message, message
        assert "started" not in message
        assert "cancelled" not in message


async def test_an_unchanged_plan_files_nothing_further(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Ninety-six refreshes against an unchanged answer produce one line.

    Change-triggered on a *coarse* fingerprint: the action, the window and the
    power and energy rounded to the material thresholds. A plan that shifts by a
    watt has not done anything a person needs to read about.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True)
    await drive(coordinator, frank)
    await hass.async_block_till_done()

    entries: list[dict] = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: entries.append(event.data))

    for _ in range(3):
        await refresh_at(coordinator, local(NORMAL, 12, 5))
        await hass.async_block_till_done()

    assert entries == []


# --- nothing changed --------------------------------------------------------


@pytest.mark.parametrize("entity_id", UNCHANGED)
async def test_the_earlier_entities_still_publish_a_value(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank: FakeFrank,
    entity_id: str,
) -> None:
    """Phase 8 computes and reports. It moves nothing that came before it."""
    await drive(setup_integration.runtime_data, frank)

    state = hass.states.get(entity_id)

    assert state is not None, entity_id
    assert state.state not in ("unavailable",)


async def test_the_economic_plan_does_not_reach_the_control_report(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The control pipeline still describes the Phase-3 plan and nothing else.

    A trading decision that leaked into the control report would be one refactor
    away from being executed, so the separation is asserted rather than assumed.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator, frank)

    report = coordinator.control_report or {}

    assert "economic" not in report
    assert "desired_action" not in report
    # The barrier is open for a charge since beta.24. What this test protects is
    # unchanged: no trading decision appears in the control report, so none is one
    # refactor away from being executed.
    assert report.get("execution_available") is True
    assert_charge_only_capability()
