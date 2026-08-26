"""Break beta.24.1 in the three ways it could regress, and catch each one.

The beta.24 fault was a *single point of failure*: one tuple was missing one
entry, and nothing behind it asked the question again. So the mutations here are
shaped to prove there are now **two** independent guarantees rather than one
guarantee tested twice.

Each mutation removes one layer and asserts the other still holds. The last two
also assert the *consequence* of getting it wrong, because "the test fails" is a
weaker statement than "the run latches on until the device dead-man expires".
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager import alphaess_adapter
from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    DISPATCH_ENABLE,
    plan_arm_parameters,
    plan_marker_claim,
    plan_reset_cleanup,
    plan_reset_deactivate,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    CONTROL_MODE_SHADOW,
)
from custom_components.alpha_ems_manager.coordinator import (
    AlphaEmsCoordinator,
)

from .test_beta241_ownership_safety import (
    DeafSurface,
    charge_command,
    drive_live_charge,
    owned_live_charge,
    sent_entities,
    step_once,
)

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def deaf_surface(hass: HomeAssistant, control_surface: None) -> DeafSurface:
    """Return a surface that can be made selectively unresponsive mid-test."""
    return DeafSurface(hass)


# -- M1: the marker falls back out of the required set -----------------------


async def test_m1_a_missing_marker_is_still_caught_without_the_capability_check(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    deaf_surface: DeafSurface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Put the beta.24 defect back and the charge still cannot arm.**

    ``REQUIRED_ENTITIES`` loses the marker again -- exactly the state that
    shipped -- *and* the helper genuinely does not respond. beta.24 armed under
    both conditions. beta.24.1 gets no further than stage one, because the
    staged arm asks a question the capability snapshot cannot: not "was the
    entity there when we looked" but "did our write land".

    This is the test that makes the two guarantees independent rather than
    decorative.
    """
    without_marker = tuple(
        entity
        for entity in alphaess_adapter.REQUIRED_ENTITIES
        if entity != BOOLEAN_EXECUTION_OWNER
    )
    monkeypatch.setattr(alphaess_adapter, "REQUIRED_ENTITIES", without_marker)
    deaf_surface.deaf.add(BOOLEAN_EXECUTION_OWNER)

    coordinator, _ = await drive_live_charge(
        hass, config_data, frank, deaf_surface, quarters=4
    )

    written = sent_entities(deaf_surface)
    assert set(written) <= {BOOLEAN_EXECUTION_OWNER}, written
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert coordinator.store.execution_record is None


# -- M2: the verification is disabled ----------------------------------------


async def test_m2_disabling_the_readback_check_is_what_lets_the_activation_through(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    deaf_surface: DeafSurface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The verification is load-bearing, demonstrated by removing it.**

    With ``_staged_write_landed`` forced to agree, the same deaf marker produces
    the beta.24 outcome: parameters written and the activation issued while the
    claim was never real. So the guarantee in
    ``test_an_unverified_claim_never_reaches_the_activation`` comes from the
    check and not from some incidental ordering that might change.
    """
    monkeypatch.setattr(
        AlphaEmsCoordinator, "_staged_write_landed", lambda self, verify: True
    )
    deaf_surface.deaf.add(BOOLEAN_EXECUTION_OWNER)

    await drive_live_charge(hass, config_data, frank, deaf_surface, quarters=4)

    written = sent_entities(deaf_surface)
    assert DISPATCH_ENABLE in written, written


# -- M3: the evidence is dropped on an unverified stop -----------------------


async def test_m3_an_unverified_stop_does_not_clear_the_record(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    deaf_surface: DeafSurface,
) -> None:
    """The guarantee itself, asserted at the method that would break it."""
    from .test_control_modes import set_mode

    coordinator = await owned_live_charge(hass, config_data, frank, deaf_surface)
    cleared: list[bool] = []
    original = type(coordinator)._clear_execution_record

    def watched(self) -> None:
        cleared.append(True)
        original(self)

    type(coordinator)._clear_execution_record = watched
    try:
        deaf_surface.deaf.add(DISPATCH_ENABLE)
        await set_mode(hass, CONTROL_MODE_SHADOW)
        await step_once(hass, coordinator, deaf_surface)
    finally:
        type(coordinator)._clear_execution_record = original

    assert cleared == [], "an unverified stop must keep every piece of evidence"
    assert coordinator.store.execution_record is not None


async def test_m3_clearing_it_would_strand_the_run(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    deaf_surface: DeafSurface,
) -> None:
    """**And the consequence, so the reason survives a refactor.**

    Dropping the record on a stop that could not be confirmed takes ownership to
    ``unproven`` -- and an unproven dispatch is never touched again. The marker
    is left on, the charge is left running, and no later refresh will retry the
    stop: it latches until the device dead-man expires.
    """
    from .test_control_modes import set_mode

    coordinator = await owned_live_charge(hass, config_data, frank, deaf_surface)
    deaf_surface.deaf.add(DISPATCH_ENABLE)
    await set_mode(hass, CONTROL_MODE_SHADOW)
    await step_once(hass, coordinator, deaf_surface)

    # The mutation, applied by hand: the stop failed, and the evidence is dropped.
    coordinator._clear_execution_record()
    deaf_surface.deaf.clear()
    deaf_surface.calls.clear()

    await step_once(hass, coordinator, deaf_surface, hour=11, minute=0)

    # Nothing retries the stop, and the marker is stranded on.
    assert sent_entities(deaf_surface) == []
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "on"


# -- structural guards -------------------------------------------------------


def test_the_stages_never_share_a_step() -> None:
    """Neither split may leak the step the other stage exists to withhold."""
    command = charge_command()

    claim = {step.entity_id for step in plan_marker_claim()}
    parameters = {step.entity_id for step in plan_arm_parameters(command)}
    assert claim == {BOOLEAN_EXECUTION_OWNER}
    assert not claim & parameters
    assert CHARGE_FAMILY.activate not in claim

    deactivate = {step.entity_id for step in plan_reset_deactivate(ACTION_CHARGE)}
    cleanup = {step.entity_id for step in plan_reset_cleanup(ACTION_CHARGE)}
    assert deactivate == {CHARGE_FAMILY.activate}
    assert not deactivate & cleanup
    assert BOOLEAN_EXECUTION_OWNER not in deactivate


def test_the_readback_check_never_reads_the_lagging_device_register() -> None:
    """**The one decision in beta.24.1 that a future edit could quietly undo.**

    Both checks read a local ``input_boolean`` the stage itself wrote, because
    those settle inside the blocking service call. ``sensor.alphaess_dispatch_start``
    is the device's own readback and lags a poll behind, so gating the cleanup on
    it would withhold the resting values every single time and release the marker
    never -- a stop that can never finish is worse than the fault the staging
    exists to fix.

    Asserted structurally rather than described in a comment, so switching to the
    deadlocking signal fails here rather than on hardware.
    """
    source = inspect.getsource(AlphaEmsCoordinator._staged_write_landed)
    tree = ast.parse(textwrap.dedent(source))
    read = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }

    assert "dispatch_active" not in read, read
    assert "SENSOR_DISPATCH_START" not in read, read
    assert "dispatch_start" not in read, read
    # And it does read the two things it is supposed to.
    assert {"owner_marker", "active_modes"} <= read, read
