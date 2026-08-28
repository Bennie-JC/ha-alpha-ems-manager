"""Two published fields that stated something other than the truth.

Both are reporting defects rather than behavioural ones -- nothing armed that
should not have, and nothing was refused that should have run. Both are the same
shape: **a field whose value was decided once, in the source, instead of read off
the runtime.** That shape is worth a suite of its own, because it is invisible to
every behavioural test: the plan is right, the control is right, and the surface
describing them is wrong.

* ``not_executable: null`` on a ``serve_load`` quarter row. In this contract that
  is a positive claim -- *Stage B may arm this row* -- and Stage B never could.
* ``execution_blocked_reason: "execution_unavailable"`` in diagnostics and in every
  stored evidence snapshot, a release-level claim that no command reaches the
  battery. Untrue since beta.24, and read by a user downloading diagnostics while a
  Live charge was running.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_BLOCKED_MODE_NOT_ACTIVE,
    ECONOMIC_BLOCKED_NOT_ENABLED,
    EXECUTION_INTENT_ACTIONS,
    QUARTER_NOT_EXECUTABLE_INTENT,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .test_beta33_campaign_wiring import multi_segment_targets, planning_coordinator
from .test_control_modes import set_mode

pytestmark = pytest.mark.usefixtures("control_surface")


def enable(coordinator, allowed: bool) -> None:
    """Set the user's "allow sending commands" switch on a live coordinator."""
    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(
        **{**fields, "control_execution_enabled": allowed}
    )


# ===========================================================================
# 1. a row Stage B cannot arm must not report that it can
# ===========================================================================


async def test_a_serve_load_row_names_its_intent_as_the_reason(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The reference defect, through the production publisher.

    Every run is published as a target, ``serve_load`` runs included -- they carry
    the campaign identity that holds one lifecycle open across the gap between two
    exports. Their rows reported ``not_executable: null``.

    *Mutation: drop the intent branch in ``quarter_schedule_for`` and every one of
    these rows goes back to claiming it is armable.*
    """
    _coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )
    assert targets, "the fixture must publish targets"

    unarmable = [t for t in targets if t["intent"] not in EXECUTION_INTENT_ACTIONS]
    assert unarmable, "the fixture must publish at least one non-executable intent"

    for target in unarmable:
        rows = target["quarter_schedule"]
        assert rows, f"{target['intent']} published no rows"
        for row in rows:
            assert row["not_executable"] == QUARTER_NOT_EXECUTABLE_INTENT, target[
                "intent"
            ]


async def test_an_executable_intent_still_reports_by_magnitude(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The fix must not make everything unexecutable.

    An intent with an actuator is still judged on the energy it asks for, which is
    the only judgement the field carried before. A blanket reason here would hide
    ``below_actuator_resolution`` -- the defect beta.30 added the field to report.
    """
    _coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )
    armable = [
        target for target in targets if target["intent"] in EXECUTION_INTENT_ACTIONS
    ]
    assert armable, "the fixture must publish at least one executable intent"

    reasons = {
        row["not_executable"]
        for target in armable
        for row in target["quarter_schedule"]
    }
    assert reasons, "executable targets must publish rows"
    assert QUARTER_NOT_EXECUTABLE_INTENT not in reasons
    assert None in reasons, "an executable target must have at least one armable row"


# ===========================================================================
# 2. the blocked reason is read, not asserted
# ===========================================================================


async def test_diagnostics_report_the_runtime_barrier_not_a_constant(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """CFG / §9. The download must say what actually stands in the way.

    *Mutation: restore the hardcoded constant and the second assertion fails.*
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    published = payload["economic_plan"]["capability"]["execution_blocked_reason"]
    assert published == coordinator.economic_blocked_reason
    assert published != ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE

    # And its neighbour in the same block, which was a literal ``False``.
    assert payload["economic_plan"]["capability"]["execution_available"] is True


async def test_the_barrier_moves_with_the_mode_and_the_switch(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """Each layer of the gate is named by the reason, deepest first.

    A user turning Active on and still seeing nothing sent needs to be told which
    of their own switches is the one holding it -- not a release-level statement
    that is the same in every configuration.
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    enable(coordinator, True)

    await set_mode(hass, CONTROL_MODE_OFF)
    assert coordinator.economic_blocked_reason == ECONOMIC_BLOCKED_MODE_NOT_ACTIVE
    await set_mode(hass, CONTROL_MODE_SHADOW)
    assert coordinator.economic_blocked_reason == ECONOMIC_BLOCKED_MODE_NOT_ACTIVE
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    assert coordinator.economic_blocked_reason != ECONOMIC_BLOCKED_MODE_NOT_ACTIVE

    # The switch is deeper than the mode: turning it off must be what is reported,
    # whatever the mode currently reads.
    enable(coordinator, False)
    assert coordinator.economic_blocked_reason == ECONOMIC_BLOCKED_NOT_ENABLED


async def test_every_surface_reports_the_same_barrier(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The attribute, the diagnostics payload and the stored snapshot, in agreement.

    Three surfaces published this field and two of them hardcoded it, so they could
    and did disagree with each other about the same instant.
    """
    from custom_components.alpha_ems_manager.sensor import _economic_blocked_reason

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    expected = coordinator.economic_blocked_reason

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert (
        payload["economic_plan"]["capability"]["execution_blocked_reason"] == expected
    )
    assert _economic_blocked_reason(coordinator) == expected

    from .forecast_helpers import NORMAL

    recorded = coordinator.history.latest_economic_snapshot(NORMAL)
    assert recorded is not None, "the refresh must have stored an evidence snapshot"
    assert recorded.execution_blocked_reason == expected
