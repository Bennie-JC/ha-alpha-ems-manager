"""The Dispatch surface: what may be written to it, and what may never be.

**Two surfaces, two sign conventions, and they must never meet.** The helper
families take a positive magnitude and carry direction in *which family* was
written. Raw Dispatch takes a **signed** power: negative charges, positive
discharges. Mixing them would command a charge as a discharge, so the tests here
assert the separation structurally as well as behaviourally.

The interlock is two-part on purpose. Direction on this surface is a *value*, not
a choice of entity, so the entity subset test that keeps a discharge off the
helper families cannot see a wrong-way dispatch at all -- and the value check
cannot see an unknown entity. Both run, and neither is a substitute for the other.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_adapter import (
    ControlActionNotPermitted,
    async_execute,
    steps_outside_capability,
)
from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    CONFLICTING_FAMILIES,
    DISCHARGE_FAMILY,
    DISPATCH_CUTOFF_SOC,
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_ENTITIES,
    DISPATCH_MODE_LABELS,
    DISPATCH_MODE_SELECT,
    DISPATCH_POWER,
    DISPATCH_PV_SWITCH,
    DISPATCH_TIMER,
    PERMITTED_SERVICES,
    SERVICE_SELECT_OPTION,
    CommandStep,
    dispatch_mode_step,
    dispatch_refusal,
    plan_dispatch_arm,
    plan_dispatch_cleanup,
    plan_dispatch_cutoff,
    plan_dispatch_power,
    plan_dispatch_rearm,
    plan_dispatch_stop,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_EXECUTABLE_DISPATCH_MODES,
    CONTROL_EXECUTABLE_DISPATCH_SIGN,
    CONTROL_REFUSE_DISPATCH_MODE,
    CONTROL_REFUSE_DISPATCH_SIGN,
)


def arm(power_kw: float = -2.3, mode: int = 2) -> tuple[CommandStep, ...]:
    """Return a healthy mode-2 charge arm."""
    return plan_dispatch_arm(
        mode=mode, power_kw=power_kw, cutoff_soc_percent=95, duration_minutes=20
    )


def entities(steps: tuple[CommandStep, ...]) -> list[str]:
    """Return the entities a step list touches, in order."""
    return [step.entity_id for step in steps]


# -- 1. the surface is the real one ------------------------------------------


def test_every_dispatch_entity_is_a_helper_and_not_a_readback_sensor() -> None:
    """**The read-only sensors are not a write surface**, and never were.

    ``sensor.alphaess_dispatch_*`` is the device's own readback. Designing against
    it would mean writing to something that only reports.
    """
    for entity in DISPATCH_ENTITIES:
        assert not entity.startswith("sensor."), entity
        domain = entity.split(".", 1)[0]
        assert domain in {"input_boolean", "input_number", "input_select"}
    assert DISPATCH_TIMER.startswith("timer."), DISPATCH_TIMER


def test_the_six_conflicting_families_are_all_declared() -> None:
    """All six the vendor automation disables, not the four modelled before."""
    names = [name for name, _ in CONFLICTING_FAMILIES]

    assert names == [
        "force_charging",
        "force_discharging",
        "force_import",
        "force_export",
        "excess_export",
        "peak_shaving",
    ]
    assert len({entity for _, entity in CONFLICTING_FAMILIES}) == 6


def test_selecting_a_mode_uses_the_exact_package_label() -> None:
    """The package parses the number out of the label, so near enough is wrong."""
    step = dispatch_mode_step(2)

    assert (step.domain, step.service) == SERVICE_SELECT_OPTION
    assert step.entity_id == DISPATCH_MODE_SELECT
    assert step.option == "State of Charge Control (2)"
    assert step.value is None, "a label is not a number"


def test_an_unknown_mode_fails_loudly_rather_than_selecting_nothing() -> None:
    """A select that silently sends nothing is worse than one that raises."""
    with pytest.raises(KeyError):
        dispatch_mode_step(19)


def test_the_select_service_is_permitted_and_is_the_only_addition() -> None:
    """One new service, and it is the one the label needs."""
    assert SERVICE_SELECT_OPTION in PERMITTED_SERVICES
    assert len(PERMITTED_SERVICES) == 4
    assert ("input_button", "press") not in PERMITTED_SERVICES


# -- 2. the sequences --------------------------------------------------------


def test_the_arm_settles_every_parameter_before_enabling() -> None:
    """**Enable last**, because it is edge-triggered: it is what makes the
    settled values take effect.

    The happy consequence is that an interrupted sequence is inert rather than
    dangerous -- the numbers mean nothing until the boolean changes, so a partial
    run commands nothing at all.
    """
    steps = arm()

    assert entities(steps) == [
        DISPATCH_MODE_SELECT,
        DISPATCH_POWER,
        DISPATCH_CUTOFF_SOC,
        DISPATCH_DURATION,
        DISPATCH_PV_SWITCH,
        DISPATCH_ENABLE,
    ]
    assert steps[-1].entity_id == DISPATCH_ENABLE
    assert steps[-1].service == "turn_on"


def test_the_arm_asserts_the_pv_switch_rather_than_assuming_it() -> None:
    """Its fail-safe state is on, and a previous run of ours may have left it off."""
    on = arm()
    pv = next(step for step in on if step.entity_id == DISPATCH_PV_SWITCH)

    assert pv.service == "turn_on"

    curtailing = plan_dispatch_arm(
        mode=2,
        power_kw=-1.0,
        cutoff_soc_percent=95,
        duration_minutes=20,
        pv_enabled=False,
    )
    pv_off = next(step for step in curtailing if step.entity_id == DISPATCH_PV_SWITCH)
    assert pv_off.service == "turn_off"


def test_a_power_correction_is_exactly_one_write() -> None:
    """**One entity, one write.**

    A correction does not touch the duration -- that would re-arm the dead-man on
    a cadence the economics never chose -- and does not touch the enable, because
    the dispatch stays on for the whole run.
    """
    steps = plan_dispatch_power(-3.4)

    assert entities(steps) == [DISPATCH_POWER]
    assert steps[0].value == pytest.approx(-3.4)


def test_a_rearm_is_exactly_one_write_and_it_is_the_duration() -> None:
    """Writing the duration rewrites the register and restarts the vendor timer."""
    assert entities(plan_dispatch_rearm(25)) == [DISPATCH_DURATION]
    assert plan_dispatch_rearm(25)[0].value == pytest.approx(25.0)


def test_a_cutoff_change_is_exactly_one_write() -> None:
    """Live in mode 2 only, and asserted as its own operation."""
    assert entities(plan_dispatch_cutoff(90)) == [DISPATCH_CUTOFF_SOC]


def test_the_stop_is_the_enable_alone() -> None:
    """**And it is also the whole of the emergency self-stop.**

    That authority grants exactly one operation, so it is exactly this function
    and there is no second definition of "the narrow stop" to drift away from.
    Turning the enable off triggers the package's own reset, which writes Dispatch
    Start = 0, so no reset button is needed.
    """
    steps = plan_dispatch_stop()

    assert entities(steps) == [DISPATCH_ENABLE]
    assert steps[0].service == "turn_off"
    assert len(steps) == 1


def test_the_cleanup_leaves_nothing_a_later_run_could_inherit() -> None:
    """Setting power to zero is not a stop, and this deliberately does more.

    A dispatch left armed at zero still holds a duration, a cutoff and a timer,
    so a short run following a long one would silently acquire the long one's
    dead-man.
    """
    steps = plan_dispatch_cleanup()
    touched = entities(steps)

    assert DISPATCH_POWER in touched
    assert DISPATCH_DURATION in touched
    assert DISPATCH_CUTOFF_SOC in touched
    assert DISPATCH_PV_SWITCH in touched
    # The marker last: until it is off the dispatch is still owned, and releasing
    # ownership early would leave Alpha EMS unable to finish its own cleanup.
    assert touched[-1] == BOOLEAN_EXECUTION_OWNER
    # And the PV switch goes back to its fail-safe state.
    pv = next(step for step in steps if step.entity_id == DISPATCH_PV_SWITCH)
    assert pv.service == "turn_on"


def test_the_stop_and_the_cleanup_never_share_a_step() -> None:
    """Whatever is withheld pending verification must actually be withheld."""
    stop = set(entities(plan_dispatch_stop()))
    cleanup = set(entities(plan_dispatch_cleanup()))

    assert not stop & cleanup


# -- 3. the value half of the interlock --------------------------------------


def test_a_healthy_mode_two_charge_is_permitted_at_both_boundaries() -> None:
    """The one combination beta.25 may command."""
    steps = arm()

    assert steps_outside_capability(steps) == ()
    assert dispatch_refusal(steps) is None


@pytest.mark.parametrize("power_kw", [0.1, 1.0, 2.3, 20.0])
def test_a_positive_dispatch_power_is_refused(power_kw: float) -> None:
    """**The sign is part of the barrier, not a downstream check.**

    Direction on this surface is a signed number, so the entity subset test
    cannot see a wrong-way dispatch at all. This is the check that can.
    """
    steps = plan_dispatch_power(power_kw)

    assert steps_outside_capability(steps) == (), "the entity itself is permitted"
    assert dispatch_refusal(steps) == CONTROL_REFUSE_DISPATCH_SIGN


@pytest.mark.parametrize("power_kw", [0.0, -0.1, -2.3, -10.0])
def test_zero_and_negative_powers_are_permitted(power_kw: float) -> None:
    """Zero has to be permitted, and that is not a loophole.

    It is what the direction gate produces when the grid target would require a
    discharge, and what the cleanup writes. Holding the battery still is the
    physical meaning of "do not discharge"; refusing to write it would leave the
    previous charge running into the reversal it was told to stop.
    """
    assert dispatch_refusal(plan_dispatch_power(power_kw)) is None


@pytest.mark.parametrize("mode", [6, 7])
def test_a_gated_mode_is_refused_even_though_it_is_modelled(mode: int) -> None:
    """Modes 6 and 7 are planned, labelled and tested -- and not commandable.

    Neither is a controllable kW primitive in any case: the package writes the
    power register as a bare 32000 for anything outside modes 1, 2, 3 and 5, so a
    kW figure in mode 7 commands nothing at all.
    """
    assert mode in DISPATCH_MODE_LABELS
    assert mode not in CONTROL_EXECUTABLE_DISPATCH_MODES
    assert dispatch_refusal((dispatch_mode_step(mode),)) == CONTROL_REFUSE_DISPATCH_MODE


def test_an_arm_in_a_gated_mode_is_refused_whole() -> None:
    """The refusal is on the list, so there are no partial writes."""
    assert dispatch_refusal(arm(mode=7)) == CONTROL_REFUSE_DISPATCH_MODE


def test_an_unlabelled_mode_string_is_refused() -> None:
    """A near-miss label selects a different mode or none, so it is refused."""
    step = CommandStep(
        *SERVICE_SELECT_OPTION, DISPATCH_MODE_SELECT, option="State of Charge Control"
    )

    assert dispatch_refusal((step,)) == CONTROL_REFUSE_DISPATCH_MODE


def test_the_executable_envelope_is_charge_only_by_construction() -> None:
    """The barrier itself says mode 2 and a negative sign, in one place."""
    assert frozenset({2}) == CONTROL_EXECUTABLE_DISPATCH_MODES
    assert CONTROL_EXECUTABLE_DISPATCH_SIGN == -1


def test_the_discharge_family_is_still_refused_on_the_other_surface() -> None:
    """Widening the Dispatch surface must not have widened the helper one."""
    step = CommandStep("input_boolean", "turn_on", DISCHARGE_FAMILY.activate)

    assert steps_outside_capability((step,)) == (DISCHARGE_FAMILY.activate,)


def test_the_charge_family_and_the_dispatch_surface_are_disjoint() -> None:
    """Two surfaces, and no entity belongs to both."""
    assert not set(CHARGE_FAMILY.entities) & set(DISPATCH_ENTITIES)
    assert not set(DISCHARGE_FAMILY.entities) & set(DISPATCH_ENTITIES)


# -- 4. the send site refuses, not just the planner --------------------------


async def test_the_send_site_refuses_a_positive_power(hass: HomeAssistant) -> None:
    """Asserted at the wire, because that is the interlock that has to hold."""
    with pytest.raises(ControlActionNotPermitted) as caught:
        await async_execute(hass, plan_dispatch_power(2.3))

    assert CONTROL_REFUSE_DISPATCH_SIGN in str(caught.value)


async def test_the_send_site_refuses_a_gated_mode(hass: HomeAssistant) -> None:
    """Same boundary, other half of the value check."""
    with pytest.raises(ControlActionNotPermitted) as caught:
        await async_execute(hass, (dispatch_mode_step(7),))

    assert CONTROL_REFUSE_DISPATCH_MODE in str(caught.value)


async def test_a_refused_list_reaches_no_service(hass: HomeAssistant) -> None:
    """**No partial writes.** The refusal is on the list, before the first call."""
    calls: list = []

    async def record(call) -> None:
        calls.append(call)

    for domain, service in PERMITTED_SERVICES:
        hass.services.async_register(domain, service, record)

    with pytest.raises(ControlActionNotPermitted):
        await async_execute(hass, arm(mode=7))

    assert calls == []
