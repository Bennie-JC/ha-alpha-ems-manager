"""The ownership hole beta.24 shipped, and the two guarantees that close it.

**The fault was not a lifecycle bug.** On the live installation
``owner_marker`` read ``null``, which means the entity was *absent* -- not off.
``input_boolean.alpha_ems_dispatch_owner`` had never been created, and nothing
refused to execute without it: the marker was missing from
:data:`REQUIRED_ENTITIES`, which is the only tuple :func:`discover` walks. So the
arm wrote ``turn_on`` to a non-existent entity, the write reported success, the
causal record could never match a dispatch it had not really claimed, and every
later refresh read ``foreign``.

That is a charge Alpha EMS could **start** and provably never own, sustain or
stop. The event log's fifteen-minute alternation was the whole cycle: arm at
:00, inhibited at :15, device dead-man at :20, arm again at :30.

Two independent guarantees are asserted here, because either alone would have
been enough to prevent it and depending on one is how it happened:

* the marker is a **required entity**, so its absence makes the capability
  unready and Live refuses before anything is planned;
* the arm is **staged**, so the activation cannot be issued until the ownership
  claim has been read back -- which holds even if the capability check is wrong.

The test surface was healthier than production, and that is worth saying plainly:
``control_surface`` has always created the marker, so no existing test could have
caught this. Two of the tests below deliberately take it away.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_adapter import (
    discover,
    marker_state,
    read_snapshot,
)
from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    REQUIRED_ENTITIES,
    plan_arm_parameters,
    plan_commands,
    plan_marker_claim,
    plan_reset,
    plan_reset_cleanup,
    plan_reset_deactivate,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    CONTROL_MODE_SHADOW,
    CONTROL_REFUSE_MARKER_NOT_VERIFIED,
    CONTROL_REFUSE_STOP_NOT_VERIFIED,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_VERIFY_MARKER_ON,
    EXECUTION_VERIFY_NO_FAMILY_ACTIVE,
    INHIBIT_MISSING_CONTROL_ENTITY,
    MARKER_ABSENT,
    MARKER_OFF,
    MARKER_ON,
    MARKER_UNAVAILABLE,
)

from .test_beta24_live_charge import (
    LiveSurface,
    charge_command,
    drive_live_charge,
    owned_live_charge,
    step_once,
)

pytestmark = pytest.mark.usefixtures("control_surface")


class DeafSurface(LiveSurface):
    """A control surface that accepts a write and then does not perform it.

    **The failure mode beta.24 could not see.** A service call that succeeds is
    not a state that changed: the entity may be absent, unavailable, or simply
    not honouring the write. Every entity id added to :attr:`deaf` is recorded as
    written and left at whatever it already held, which is exactly what a missing
    helper looks like from the caller's side.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Start out fully responsive, so a run can be established normally."""
        super().__init__(hass)
        self.deaf: set[str] = set()

    def _apply(self, call, value: str) -> None:
        if call.data["entity_id"] in self.deaf:
            self.calls.append(call)
            return
        super()._apply(call, value)


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes.

    Redefined rather than imported: a fixture is registered by the module that
    declares it, and importing the function only brings the callable across.
    """
    return LiveSurface(hass)


@pytest.fixture
def deaf_surface(hass: HomeAssistant, control_surface: None) -> DeafSurface:
    """Return a surface that can be made selectively unresponsive mid-test."""
    return DeafSurface(hass)


def sent_entities(surface: LiveSurface) -> list[str]:
    """Return every entity written, in order."""
    return [call.data["entity_id"] for call in surface.calls]


# -- 1. the marker is required ------------------------------------------------


def test_the_owner_marker_is_a_required_entity() -> None:
    """**The one-line root cause, asserted where it lived.**

    ``discover`` walks :data:`REQUIRED_ENTITIES` and nothing else, so an entity
    absent from this tuple is an entity whose absence nothing reports and nothing
    refuses. It was absent for the whole of beta.24.
    """
    assert BOOLEAN_EXECUTION_OWNER in REQUIRED_ENTITIES


def test_a_missing_marker_makes_the_capability_unready(hass: HomeAssistant) -> None:
    """An absent marker is *named* as missing, not merely counted."""
    hass.states.async_remove(BOOLEAN_EXECUTION_OWNER)

    capability = discover(hass)

    assert BOOLEAN_EXECUTION_OWNER in capability.missing
    assert capability.ready is False


def test_an_unavailable_marker_makes_the_capability_unready(
    hass: HomeAssistant,
) -> None:
    """Present but telling us nothing is also unusable, and reported separately."""
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "unavailable")

    capability = discover(hass)

    assert BOOLEAN_EXECUTION_OWNER in capability.unavailable
    assert capability.ready is False


def test_the_marker_state_distinguishes_absent_from_off(hass: HomeAssistant) -> None:
    """**Four facts, not one boolean.**

    ``owner_marker`` was ``None`` for a missing helper and ``False`` for one that
    is off. Both mean "not ours", which is right for attribution and useless for
    diagnosis: a user whose charge never owned anything needs to be told the
    helper does not exist.
    """
    assert marker_state(hass) == MARKER_OFF

    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "on")
    assert marker_state(hass) == MARKER_ON

    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "unavailable")
    assert marker_state(hass) == MARKER_UNAVAILABLE

    hass.states.async_remove(BOOLEAN_EXECUTION_OWNER)
    assert marker_state(hass) == MARKER_ABSENT
    assert read_snapshot(hass).owner_marker_state == MARKER_ABSENT


# -- 2. the staging is structural --------------------------------------------


def test_the_arm_stages_concatenate_to_the_published_list() -> None:
    """The report cannot describe a sequence the send site would not send."""
    command = charge_command()

    assert plan_marker_claim() + plan_arm_parameters(command) == plan_commands(command)


def test_the_reset_stages_concatenate_to_the_published_list() -> None:
    """Same invariant on the stop, including the no-direction case."""
    assert plan_reset_deactivate(ACTION_CHARGE) + plan_reset_cleanup(
        ACTION_CHARGE
    ) == plan_reset(ACTION_CHARGE)
    assert plan_reset_deactivate(None) + plan_reset_cleanup(None) == plan_reset(None)


def test_the_claim_is_alone_in_stage_one_and_the_activation_is_not() -> None:
    """**The guarantee, stated as a shape.**

    "Activation last" was already true in beta.24 and was not enough: last in a
    list that runs unconditionally is still reached when the first step did
    nothing. What beta.24.1 adds is that the activation is in a *different stage*
    from the claim, so a claim that cannot be read back cannot be followed.
    """
    command = charge_command()
    stage_one = plan_marker_claim()
    stage_two = plan_arm_parameters(command)

    assert [step.entity_id for step in stage_one] == [BOOLEAN_EXECUTION_OWNER]
    assert stage_two[-1].entity_id == CHARGE_FAMILY.activate


def test_the_deactivation_is_alone_in_stage_one_of_the_stop() -> None:
    """Nothing a running dispatch depends on is in the same stage as the stop.

    The duration write is the reason this matters rather than being tidy: writing
    it restarts the vendor package timer, so a cleanup issued against a dispatch
    that did not actually stop would *extend* the run it was ending.
    """
    stage_one = plan_reset_deactivate(ACTION_CHARGE)
    cleanup = [step.entity_id for step in plan_reset_cleanup(ACTION_CHARGE)]

    assert [step.entity_id for step in stage_one] == [CHARGE_FAMILY.activate]
    assert CHARGE_FAMILY.duration in cleanup
    assert cleanup[-1] == BOOLEAN_EXECUTION_OWNER


def test_a_command_that_moves_nothing_claims_nothing() -> None:
    """No claim without something to arm, or the marker needs clearing as stale."""
    hold = charge_command(power_kw=0.0)

    assert plan_arm_parameters(hold) == ()
    assert plan_commands(hold) == ()


# -- 3. the missing marker cannot reach the hardware -------------------------


async def test_a_missing_marker_refuses_live_and_writes_nothing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**The beta.24 regression, closed at the capability boundary.**

    The exact production condition: Live, command sending enabled, an economic
    charge available, and no marker helper. beta.24 armed. beta.24.1 refuses
    before a single service call, and says which entity is missing.
    """
    hass.states.async_remove(BOOLEAN_EXECUTION_OWNER)

    coordinator, trace = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=4
    )

    assert live_surface.calls == []
    assert hass.states.get(CHARGE_FAMILY.activate).state == "off"
    assert coordinator.store.execution_record is None
    assert all(row["authorized"] is not True for row in trace), trace

    report = coordinator.control_report or {}
    capability = report.get("capability") or {}
    assert BOOLEAN_EXECUTION_OWNER in (capability.get("missing") or []), capability
    safety = report.get("safety") or {}
    assert safety.get("inhibit_reason") == INHIBIT_MISSING_CONTROL_ENTITY, safety
    assert safety.get("checks_passed") == 0, safety


async def test_an_unavailable_marker_refuses_live_and_writes_nothing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Present but unreadable is refused too, and is a different reported fact."""
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "unavailable")

    coordinator, trace = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=4
    )

    assert live_surface.calls == []
    assert hass.states.get(CHARGE_FAMILY.activate).state == "off"
    assert coordinator.store.execution_record is None
    assert all(row["authorized"] is not True for row in trace), trace

    capability = (coordinator.control_report or {}).get("capability") or {}
    assert BOOLEAN_EXECUTION_OWNER in (capability.get("unavailable") or []), capability


# -- 4. the staged arm, closing the loop -------------------------------------


async def test_an_unverified_claim_never_reaches_the_activation(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    deaf_surface: DeafSurface,
) -> None:
    """**The independent guarantee, with the capability check deliberately satisfied.**

    The marker entity exists -- so ``discover`` is content and Live is authorised
    -- but the write does not take. That is the shape of every silent
    control-surface failure, and it is the case a capability snapshot taken
    before the write cannot see.

    Stage one is sent, the readback disagrees, and stage two never runs: no
    power, no cutoff, no duration, no activation. The causal record is withdrawn,
    because a claim that was never real must not outlive the attempt.
    """
    deaf_surface.deaf.add(BOOLEAN_EXECUTION_OWNER)

    coordinator, _ = await drive_live_charge(
        hass, config_data, frank, deaf_surface, quarters=4
    )

    written = sent_entities(deaf_surface)
    assert written, "stage one should have been attempted"
    assert set(written) == {BOOLEAN_EXECUTION_OWNER}, written
    assert hass.states.get(CHARGE_FAMILY.activate).state == "off"
    assert coordinator.store.execution_record is None

    execution = (coordinator.control_report or {}).get("execution") or {}
    assert (execution.get("result") or {}).get(
        "execution_error"
    ) == CONTROL_REFUSE_MARKER_NOT_VERIFIED, execution.get("result")


async def test_a_healthy_arm_publishes_both_stages_and_its_check(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """The happy path still arms, and now says how it was staged."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    execution = (coordinator.control_report or {}).get("execution") or {}
    boundary = execution.get("write_boundary") or {}
    stage_one = [step["entity_id"] for step in (boundary.get("stage_one") or [])]
    stage_two = [step["entity_id"] for step in (boundary.get("stage_two") or [])]

    if boundary.get("sequence") == "arm":
        assert stage_one == [BOOLEAN_EXECUTION_OWNER], boundary
        assert boundary.get("stage_verification") == EXECUTION_VERIFY_MARKER_ON
        assert stage_two[-1] == CHARGE_FAMILY.activate, boundary
    # Whatever this refresh was, the two stages must sum to the published list.
    steps = [step["entity_id"] for step in (boundary.get("steps") or [])]
    assert stage_one + stage_two == steps, boundary


# -- 5. the staged stop ------------------------------------------------------


async def test_an_unverified_stop_withholds_cleanup_and_keeps_the_evidence(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    deaf_surface: DeafSurface,
) -> None:
    """**A stop that cannot be confirmed must not publish a clean state.**

    The deactivation is written and does not take, so something may well still be
    charging. The cleanup is withheld -- writing the duration here would restart
    the vendor timer and extend the very run being stopped -- the marker stays
    on, and the causal record is kept.

    Keeping the record is the load-bearing part. Clearing it would drop ownership
    to ``unproven``, and an unproven dispatch is never touched again: the run
    would latch on until the device dead-man expired.
    """
    from .test_control_modes import set_mode

    coordinator = await owned_live_charge(hass, config_data, frank, deaf_surface)
    deaf_surface.deaf.add(CHARGE_FAMILY.activate)

    await set_mode(hass, CONTROL_MODE_SHADOW)
    report = await step_once(hass, coordinator, deaf_surface)

    written = sent_entities(deaf_surface)
    assert written == [CHARGE_FAMILY.activate], written
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "on"
    assert coordinator.store.execution_record is not None

    execution = report.get("execution") or {}
    assert (execution.get("result") or {}).get(
        "execution_error"
    ) == CONTROL_REFUSE_STOP_NOT_VERIFIED, execution.get("result")


async def test_a_verified_stop_completes_and_releases_the_marker_last(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    deaf_surface: DeafSurface,
) -> None:
    """And once the deactivation does take, the same path finishes the job.

    The retry is the point: the withheld cleanup is not lost, it is deferred to a
    refresh that can prove the dispatch stopped.
    """
    from .test_control_modes import set_mode

    coordinator = await owned_live_charge(hass, config_data, frank, deaf_surface)
    deaf_surface.deaf.add(CHARGE_FAMILY.activate)
    await set_mode(hass, CONTROL_MODE_SHADOW)
    await step_once(hass, coordinator, deaf_surface)
    assert coordinator.store.execution_record is not None

    # The surface starts responding again, and the next refresh retries.
    deaf_surface.deaf.clear()
    deaf_surface.calls.clear()
    report = await step_once(hass, coordinator, deaf_surface, hour=11, minute=0)

    written = sent_entities(deaf_surface)
    assert written[0] == CHARGE_FAMILY.activate, written
    assert written[-1] == BOOLEAN_EXECUTION_OWNER, written
    assert hass.states.get(CHARGE_FAMILY.activate).state == "off"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"
    assert coordinator.store.execution_record is None

    execution = report.get("execution") or {}
    boundary = execution.get("write_boundary") or {}
    assert boundary.get("stage_verification") == EXECUTION_VERIFY_NO_FAMILY_ACTIVE
    assert (execution.get("result") or {}).get(
        "stop_reason"
    ) == EXECUTION_STOP_SWITCHED_TO_SHADOW
