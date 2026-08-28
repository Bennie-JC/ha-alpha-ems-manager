"""The first release that writes, and the four things that keep it charge-only.

**beta.24 is the first Alpha EMS release in which a command can reach the
inverter.** Only a Stage-B ``grid_charge``, and the tests here exist because that
sentence has two halves and both need proving: that a charge really does reach the
wire, and that nothing else can.

Tracing the barrier before implementing found three compounding faults, and every
section below corresponds to one of them:

* a boolean barrier would have authorised **reserve-guard discharges** on the first
  refresh with no charge to make -- the command source falls back to Phase 3,
  ``authorize`` never looked at the direction, and ``write_refusal`` only checks a
  command against its own family;
* the causal record stored a dispatch start read *before* arming, so
  ``record_matches`` was permanently false, ownership never left ``unproven``, and
  ``reset_required`` -- gated on ``owned`` -- meant Alpha EMS could arm a charge it
  could never stop;
* ``unproven`` suppresses Stage B's intent, which handed the wheel straight back to
  the reserve guard while our own charge was running.

The negative claims are asserted at the boundary that actually holds them, not at
the one that happened to hold them first. That distinction is the point: a test
that passes because *nothing* executes proves nothing once something does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager import activity as activity_module
from custom_components.alpha_ems_manager.alphaess_adapter import (
    ControlActionNotPermitted,
    async_execute,
    steps_outside_capability,
)
from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    DISPATCH_CUTOFF_SOC,
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_ENTITIES,
    DISPATCH_POWER,
    DISPATCH_PV_SWITCH,
    DISPATCH_TIMER,
    PERMITTED_SERVICES,
    SENSOR_DISPATCH_ACTIVE_POWER,
    SENSOR_DISPATCH_MODE,
    SENSOR_DISPATCH_START,
    build_command,
    plan_commands,
    plan_release_marker,
    plan_reset,
    plan_sustain,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTIVITY_CATEGORY_SAFETY_BUY,
    CONTROL_CUTOFF_MIN_PERCENT,
    CONTROL_EXECUTABLE_ACTIONS,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
    CONTROL_STATE_OFF,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_EVENT_CANCELLED,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_STOP_HEADROOM_REACHED,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_TIMER_NOT_REFRESHED,
    OWNERSHIP_OWNED,
    OWNERSHIP_PROVENANCE_EXACT,
    OWNERSHIP_PROVENANCE_PARAMETERS,
    OWNERSHIP_PROVENANCE_SETTLING,
    OWNERSHIP_UNPROVEN,
    REFUSE_LIVE_ACTION_NOT_PERMITTED,
)
from custom_components.alpha_ems_manager.execution import (
    OWNERSHIP_CLAIM_WINDOW_SECONDS,
    OwnershipEvidence,
    action_for_intent,
    admit,
    carried_from_record,
    decide,
    parse_target,
    target_as_published,
)

from .live_capability import assert_charge_only_capability
from .test_beta23_lifecycle_reporting import (
    ADMITTED,
    TARGET_KWH,
    WINDOW_START,
    charge_publication,
)
from .test_control_pipeline import make_intent
from .test_stage_b_controller import progress_of

pytestmark = pytest.mark.usefixtures("control_surface")


def charge_target():
    """Return the incident's charge target, parsed."""
    target = parse_target(charge_publication(WINDOW_START))
    assert target is not None
    return target


def charge_command(power_kw: float = 2.3):
    """Return a real charge command at ``power_kw``."""
    return build_command(
        make_intent(
            action=ACTION_CHARGE,
            energy_ac_kwh=power_kw * 0.25,
            ceiling_soc_percent=90.0,
        )
    )


# ===========================================================================
# A. the capability boundary
# ===========================================================================


def test_this_release_executes_a_charge_and_nothing_else() -> None:
    """The barrier is a set, so "which direction" is representable at all."""
    assert_charge_only_capability()


def test_the_final_interlock_reads_the_wire_rather_than_the_intention() -> None:
    """**The interlock that survives a defect upstream.**

    Every check above it reasons about a ``DeviceCommand``. This one compares entity
    ids against the set this release may touch, so a command that lies about its own
    action is refused anyway -- which is the only kind of check that is worth
    anything as a *last* line.
    """
    charge = plan_commands(charge_command())
    discharge = plan_commands(build_command(make_intent(energy_ac_kwh=0.5)))

    assert steps_outside_capability(charge) == ()
    assert steps_outside_capability(discharge)
    # And the reset and marker sequences a charge needs are inside the set.
    assert steps_outside_capability(plan_reset(ACTION_CHARGE)) == ()
    assert steps_outside_capability(plan_release_marker()) == ()
    assert steps_outside_capability(plan_sustain(charge_command())) == ()


def test_the_owner_marker_is_permitted_because_it_is_not_a_direction() -> None:
    """It is how a direction becomes attributable, and releasing it is always safe."""
    assert steps_outside_capability(plan_release_marker()) == ()
    assert plan_release_marker()[0].entity_id == BOOLEAN_EXECUTION_OWNER


async def test_a_charge_reaches_the_wire_and_a_discharge_does_not(
    hass: HomeAssistant, writes: list
) -> None:
    """Both halves on the same boundary, which is the only honest comparison."""
    charge = plan_commands(charge_command())

    assert await async_execute(
        hass, charge, intent=EXECUTION_INTENT_GRID_CHARGE
    ) == len(charge)
    assert [call.data["entity_id"] for call in writes] == [
        step.entity_id for step in charge
    ]

    writes.clear()
    with pytest.raises(ControlActionNotPermitted, match="entity_not_executable"):
        await async_execute(
            hass,
            plan_commands(build_command(make_intent())),
            intent=EXECUTION_INTENT_GRID_CHARGE,
        )
    assert writes == []


# ===========================================================================
# B. discharge and export stay unreachable, including the reserve guard
# ===========================================================================


@pytest.mark.parametrize(
    "action", [ACTION_DISCHARGE, "export", "curtail_pv", "hold", "", None]
)
def test_no_action_but_a_charge_is_ever_authorized(action) -> None:
    """Parametrised over every action any layer can name, plus the absent one.

    ``None`` refuses too, deliberately: an authorisation that cannot say what it is
    authorising is not one, and a call site that forgot to pass the direction must
    not thereby gain permission for all of them.
    """
    from custom_components.alpha_ems_manager.safety import authorize

    from .test_control_pipeline import _safe_verdict, make_context

    decision = authorize(
        _safe_verdict(),
        make_context(mode=CONTROL_MODE_ACTIVE, execution_enabled=True),
        commands_planned=5,
        starts_or_increases=False,
        action=action,
    )

    assert decision.authorized is False
    assert decision.refusal == REFUSE_LIVE_ACTION_NOT_PERMITTED


def test_the_reserve_guard_is_silent_while_stage_b_holds_a_run() -> None:
    """**The third fault, and the one that needed a change of its own.**

    Stage B returns no intent on a refresh where its own run is *waiting* --
    ownership settling, a window not yet open, a request reduced to nothing. The
    fallback would then hand the wheel to a layer that only ever discharges, while
    our own charge is physically running.

    Read structurally, at the one place the command source is decided.
    """
    from custom_components.alpha_ems_manager import coordinator as module

    source = module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "stage_b_holds_the_run" in text
    assert "if intent is None and not stage_b_holds_the_run:" in text


def test_a_discharge_step_list_is_refused_whole() -> None:
    """No partial writes. Six steps in, and every one of them refused together."""
    steps = plan_commands(build_command(make_intent(energy_ac_kwh=0.5)))
    outside = steps_outside_capability(steps)

    assert len(steps) == 6
    # Five of the six name discharge helpers; the sixth is the owner marker, which
    # is permitted -- so the refusal comes from the direction, not from the marker.
    assert len(outside) == 5
    assert BOOLEAN_EXECUTION_OWNER not in outside


def test_the_raw_dispatch_surface_is_outside_the_capability() -> None:
    """Its power field is signed and its convention is the opposite of the helpers'."""
    from custom_components.alpha_ems_manager.alphaess_device import CommandStep

    raw = tuple(
        CommandStep("input_number", "set_value", entity, 1.0)
        for entity in (
            SENSOR_DISPATCH_START,
            SENSOR_DISPATCH_MODE,
            SENSOR_DISPATCH_ACTIVE_POWER,
        )
    )

    assert len(steps_outside_capability(raw)) == 3


# ===========================================================================
# C. ownership: stamping, the settle window, and the restart rule
# ===========================================================================

NOW = datetime(2026, 8, 25, 11, 15, tzinfo=UTC)


def evidence_at(now: datetime, **overrides) -> OwnershipEvidence:
    """Return ownership evidence for a run of ours that is under way."""
    params = {
        "dispatch_active": True,
        "marker_on": True,
        "run_id": "run-1",
        "now": now,
        "dispatch_start": now,
        "record": {
            "run_id": "run-1",
            "written_at": (now - timedelta(seconds=30)).isoformat(),
            "dispatch_start": None,
        },
        # **beta.30: the readback is the third ownership factor.** Evidence for a
        # run of ours must now say the device reflects the command this claim
        # wrote -- mode and sign. The power is reported rather than judged, because
        # the sixty-second controller varies it by design, and the duration is
        # judged against the permitted dead-man set for the same reason.
        "readback_compatible": True,
    }
    params.update(overrides)
    return OwnershipEvidence(**params)


def test_a_fresh_claim_is_owned_while_it_settles() -> None:
    """**The second fault, and why a bounded settle window had to exist.**

    The record is written *before* the writes, so the device reports no dispatch
    start and the record stores ``None``. Requiring an exact match from there is
    unsatisfiable: ownership would never leave ``unproven``, and ``reset_required``
    is gated on ``owned`` -- so Alpha EMS could arm a charge it could never stop.
    """
    evidence = evidence_at(NOW)

    assert evidence.record_provenance == OWNERSHIP_PROVENANCE_SETTLING
    assert evidence.record_matches is True


def test_an_old_claim_is_still_owned_when_the_readback_agrees() -> None:
    """**The beta.30 model change, stated where the old rule used to be.**

    Until beta.30 a claim older than the settle window proved nothing, because both
    provenance paths needed the vendor dispatch-start register. On the real inverter
    neither could ever be satisfied, so ownership was permanently ``unproven``: no
    correction landed on a thirty-minute run, the EMS could not stop its own
    dispatch, and the dead-man had to finish every run.

    Age is no longer the question. The question is whether the device reflects the
    command this claim wrote -- and it does, so this is ours however long ago it was
    armed. That is what makes a long run controllable at all.
    """
    old = (NOW - timedelta(seconds=OWNERSHIP_CLAIM_WINDOW_SECONDS + 60)).isoformat()
    evidence = evidence_at(
        NOW, record={"run_id": "run-1", "written_at": old, "dispatch_start": None}
    )

    assert evidence.record_provenance == OWNERSHIP_PROVENANCE_PARAMETERS
    assert evidence.record_matches is True


def test_the_register_may_strengthen_a_claim_but_never_withhold_one() -> None:
    """**The register is corroborating-only**, which is the whole of the beta.30 fix.

    A register that agrees upgrades the label a reader sees to ``exact``. A register
    that disagrees -- or reads zero, or is in some other epoch entirely, which is
    what the hardware may well be doing -- leaves ownership intact and merely stops
    corroborating it.

    This is the property that makes the release safe to ship before P0 reports: no
    outcome of that measurement can take ownership away.
    """
    stamped = {
        "run_id": "run-1",
        "written_at": (NOW - timedelta(hours=2)).isoformat(),
        "dispatch_start": NOW.isoformat(),
    }
    exact = evidence_at(NOW, record=stamped)
    moved = evidence_at(NOW, record=stamped, dispatch_start=NOW + timedelta(hours=1))
    absent = evidence_at(NOW, record=stamped, dispatch_start=None)

    assert exact.record_provenance == OWNERSHIP_PROVENANCE_EXACT
    # Disagreeing, and still ours.
    assert moved.record_provenance == OWNERSHIP_PROVENANCE_PARAMETERS
    assert moved.record_matches is True
    # Absent, and still ours.
    assert absent.record_provenance == OWNERSHIP_PROVENANCE_PARAMETERS
    assert absent.record_matches is True


def test_a_disagreeing_readback_is_never_owned() -> None:
    """And the factor that replaced it does refuse. Fail-closed is unchanged."""
    assert evidence_at(NOW, readback_compatible=False).record_matches is False
    assert evidence_at(NOW, readback_compatible=False).failed_factor == "readback_mode"


def test_the_settle_window_needs_the_marker_and_a_running_dispatch() -> None:
    """Both factors, unchanged. A record alone was never evidence and still is not."""
    assert evidence_at(NOW, marker_on=False).record_matches is False
    assert evidence_at(NOW, dispatch_active=False).record_matches is False
    assert evidence_at(NOW, run_id="a-different-run").record_matches is False


def test_a_record_round_trips_through_the_store() -> None:
    """Exactly, including the headroom cap.

    A serialiser that dropped ``max_end_energy_kwh`` would restore a run allowed to
    charge past a ceiling the user chose, which is the sort of loss that shows up as
    a full battery rather than as an error.
    """
    target = charge_target()
    assert parse_target(target_as_published(target)) == target
    assert (
        target_as_published(target)["max_end_energy_kwh"] == target.max_end_energy_kwh
    )


def test_a_run_is_adopted_from_its_record_identically() -> None:
    """The restart case: the run we execute is the run we started."""
    run = admit(charge_target(), ADMITTED)
    record = {
        "run_id": run.run_id,
        "plan_id": run.plan_id,
        "revision": run.revision,
        "target": target_as_published(run.target),
        "admitted_at": run.admitted_at.isoformat(),
        "affirmed_at": run.affirmed_at.isoformat(),
        "stale_after": run.stale_after.isoformat(),
    }

    assert carried_from_record(record) == run


@pytest.mark.parametrize(
    ("label", "record"),
    [
        ("a beta.23-shaped record", {"run_id": "run-1", "plan_id": "p", "revision": 1}),
        ("no record at all", None),
        ("no run id", {"target": {}, "admitted_at": ADMITTED.isoformat()}),
        ("an unparseable target", {"run_id": "run-1", "target": {"intent": "x"}}),
    ],
)
def test_an_insufficient_record_adopts_nothing(label: str, record) -> None:
    """**And adopting nothing must not become resetting something.**

    A record that cannot prove which run is running yields ``None``. The tempting
    next step is to stop the dispatch anyway, and it is exactly the step this
    project has refused since Phase 4: a reset is a physical write, and issuing one
    against a dispatch whose provenance cannot be established is what the
    foreign/unproven rule exists to prevent.
    """
    assert carried_from_record(record) is None, label


async def test_a_restart_with_insufficient_evidence_writes_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, writes: list
) -> None:
    """The invariant, driven through the real coordinator with a live dispatch.

    A beta.23-shaped record, a dispatch running and the marker on. Zero service
    calls: no reset, and **no marker release** either -- releasing it would assert an
    ownership conclusion we do not have.
    """
    from .test_control_modes import set_mode

    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    coordinator.store.execution_record = {
        "run_id": "an-older-run",
        "plan_id": "p1",
        "revision": 1,
        "written_at": ADMITTED.isoformat(),
        "dispatch_start": None,
    }
    coordinator._carried = None
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "on")
    hass.states.async_set(SENSOR_DISPATCH_START, "40500")
    await hass.async_block_till_done()
    writes.clear()

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    report = coordinator.control_report or {}
    ownership = ((report.get("execution") or {}).get("ownership") or {}).get("state")

    assert writes == []
    assert ownership == OWNERSHIP_UNPROVEN
    # The record is left alone as well: reconciling it belongs to the ordinary
    # stale-marker path, once there is nothing running behind it.
    assert coordinator.store.execution_record is not None
    assert coordinator._carried is None


# ===========================================================================
# D. the dead-man: sustained unconditionally, and verified
# ===========================================================================


def test_a_sustain_refreshes_the_deadman_without_rewriting_the_power() -> None:
    """**The two obligations, kept apart.**

    An earlier draft gated the whole re-arm on a material power change, which would
    mean a charge holding steady at 3.0 kW never re-arms, its dead-man is never
    refreshed, and the dispatch expires mid-run while the controller believes it is
    still going. Constant power is the *common* case.
    """
    command = charge_command(3.0)
    sustain = plan_sustain(command)
    entities = [step.entity_id for step in sustain]

    assert CHARGE_FAMILY.duration in entities
    assert CHARGE_FAMILY.activate == entities[-1]
    assert CHARGE_FAMILY.power not in entities
    assert BOOLEAN_EXECUTION_OWNER not in entities


def test_the_sustain_sets_the_duration_from_the_command() -> None:
    """It is the dead-man, so it is the one figure a sustain must always write."""
    command = charge_command(3.0)
    duration = next(
        step
        for step in plan_sustain(command)
        if step.entity_id == CHARGE_FAMILY.duration
    )

    assert duration.value == float(command.duration_minutes)
    assert command.duration_minutes >= 20


def test_an_unchanged_power_is_not_material_and_a_changed_one_is(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The deadband is the one this integration already uses everywhere else."""
    coordinator = setup_integration.runtime_data
    coordinator._last_control_power_kw = 3.0

    assert coordinator._power_moved_materially(charge_command(3.0)) is False
    assert (
        coordinator._power_moved_materially(charge_command(3.0 + CONTROL_MIN_POWER_KW))
        is True
    )
    # Nothing written yet is an arm, not a sustain.
    coordinator._last_control_power_kw = None
    assert coordinator._power_moved_materially(charge_command(3.0)) is True


def test_a_deadman_that_did_not_advance_stops_the_run() -> None:
    """Measured, not assumed, and the failure has one behaviour: stop.

    Whether re-activating an already-active dispatch refreshes the helper timer is a
    property of the control surface rather than of this integration. When the
    measurement says it did not, the run is ending whatever the controller believes,
    so it is ended deliberately.
    """
    decision = decide(
        mode_executes=True,
        mode_off=False,
        targets=[charge_publication(WINDOW_START)],
        now=WINDOW_START + timedelta(minutes=30),
        evidence=evidence_at(WINDOW_START + timedelta(minutes=30)),
        progress=progress_of(1.0),
        deadman_stale=True,
    )

    assert decision.stop_reason == EXECUTION_STOP_TIMER_NOT_REFRESHED
    assert decision.reset_required is True


def test_a_stale_deadman_is_ignored_when_nothing_is_owned() -> None:
    """It can only ever end a run that exists, so Shadow is untouched by it."""
    decision = decide(
        mode_executes=False,
        mode_off=False,
        targets=[charge_publication(WINDOW_START)],
        now=WINDOW_START + timedelta(minutes=30),
        evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
        progress=progress_of(1.0),
        deadman_stale=True,
    )

    assert decision.stop_reason != EXECUTION_STOP_TIMER_NOT_REFRESHED
    assert decision.reset_required is False


def test_the_staleness_test_needs_a_previous_sustain_of_this_run(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A missing reading is "no evidence it advanced", never "it failed".

    Reading silence as failure would stop a healthy run every time the timer entity
    was briefly unavailable, which is the opposite of conservative.
    """
    coordinator = setup_integration.runtime_data

    class Snapshot:
        dispatch_timer_finishes_at = None

    assert coordinator._deadman_is_stale(Snapshot(), "run-1") is False
    coordinator._sustained_run_id = "run-1"
    coordinator._sustained_deadline = NOW
    # Still no reading, so still no conclusion.
    assert coordinator._deadman_is_stale(Snapshot(), "run-1") is False

    class Advanced:
        dispatch_timer_finishes_at = NOW + timedelta(minutes=20)

    class Stuck:
        dispatch_timer_finishes_at = NOW

    assert coordinator._deadman_is_stale(Advanced(), "run-1") is False
    assert coordinator._deadman_is_stale(Stuck(), "run-1") is True
    # And a different run is never compared against this one's deadline.
    assert coordinator._deadman_is_stale(Stuck(), "another-run") is False


def test_no_deactivate_reactivate_fallback_ships() -> None:
    """The failure policy is one behaviour: stop.

    An automatic fallback would mean the first real Live campaign silently
    exercising an unobserved write pattern at the moment the controller had just
    discovered its assumption was wrong. Read structurally so it cannot creep in.
    """
    from custom_components.alpha_ems_manager import coordinator as module

    with open(module.__file__, encoding="utf-8") as handle:
        text = handle.read()

    lowered = text.lower()
    for phrase in ("reactivate", "re_activate", "deactivate_then", "toggle_activation"):
        assert phrase not in lowered, phrase


# ===========================================================================
# E. the charge cutoff and the grid budget, now that they are real money
# ===========================================================================


def test_the_charge_cutoff_is_an_upper_bound_from_the_ceiling() -> None:
    """Never the discharge floor, and no ``+1 %`` on an upper bound."""
    command = build_command(
        make_intent(action=ACTION_CHARGE, energy_ac_kwh=0.5, ceiling_soc_percent=90.0)
    )

    assert command.cutoff_soc_percent == 90
    assert command.power_kw > 0.0


def test_a_charge_with_no_establishable_ceiling_is_refused() -> None:
    """Refused rather than given a substituted bound."""
    command = build_command(
        make_intent(action=ACTION_CHARGE, energy_ac_kwh=0.5, ceiling_soc_percent=None)
    )

    assert command.power_kw == 0.0
    assert command.moves_battery is False
    assert plan_commands(command) == ()


def test_the_reset_returns_the_cutoff_to_its_resting_value() -> None:
    """A run must not inherit the previous run's ceiling or dead-man."""
    steps = {step.entity_id: step.value for step in plan_reset(ACTION_CHARGE)}

    assert steps[CHARGE_FAMILY.cutoff_soc] == float(CONTROL_CUTOFF_MIN_PERCENT)
    assert steps[CHARGE_FAMILY.power] == 0.0


def test_the_grid_budget_stops_a_run_and_is_keyed_on_it() -> None:
    """It becomes real money in beta.24, so it gets its own assertion."""
    decision = decide(
        mode_executes=True,
        mode_off=False,
        targets=[charge_publication(WINDOW_START)],
        now=WINDOW_START + timedelta(minutes=30),
        evidence=evidence_at(WINDOW_START + timedelta(minutes=30)),
        progress=progress_of(0.5),
        grid_charged_kwh=5.0,
        configured_budget_kwh=5.0,
    )

    assert decision.reset_required is True
    assert decision.request_kw == 0.0


def test_the_headroom_stop_is_its_own_reason() -> None:
    """ "Complete" and "stopped for headroom" are not the same sentence.

    They stop and reset identically, and beta.23 reported both as
    ``target_reached`` -- which told a reader the plan was met when in fact the pack
    had run out of room.
    """
    assert EXECUTION_STOP_HEADROOM_REACHED != EXECUTION_STOP_TARGET_REACHED
    # beta.31 sharpens this further: the two endings are not two phrasings of one
    # event kind any more. Running out of room is a *cancellation* and meeting the
    # target is a *success*, so a history view can tell them apart without reading
    # the sentence at all.
    assert activity_module._CANCEL_REASONS[EXECUTION_STOP_HEADROOM_REACHED] == (
        "Headroom Reached"
    )
    assert EXECUTION_STOP_TARGET_REACHED not in activity_module._CANCEL_REASONS


# ===========================================================================
# F. the Activity lifecycle: three lines per plan, and no more
# ===========================================================================
#
# **Rewritten for beta.31**, and the count did not change: three lines, once per
# campaign. What changed is where the first of them comes from and how the
# deduplication is keyed.
#
# beta.24 keyed all three on Stage B's ``run_id`` and took the "planned" line from
# the controller's ``prepared`` state. That was correct for a Live campaign and
# left Stage A's own advice churning beside it -- the two surfaces both spoke, and
# the advice half re-announced the same campaign every fifteen minutes as the
# horizon head advanced. beta.31 has **one** lifecycle: the plan's, keyed on the
# window's end, with Stage B's run attaching to it when the run is admitted.


LIFECYCLE_END = NOW + timedelta(hours=3)
LIFECYCLE_START = NOW + timedelta(minutes=10)


def planned_run(**overrides) -> activity_module.PlannedRun:
    """Return the campaign as Stage A publishes it to Activity."""
    params = {
        "category": ACTIVITY_CATEGORY_SAFETY_BUY,
        "energy_kwh": TARGET_KWH,
        "end_utc": LIFECYCLE_END,
        "window": "13:00-16:30",
    }
    params.update(overrides)
    return activity_module.PlannedRun(
        identity=activity_module.RunIdentity(
            direction=ECONOMIC_DIRECTION_CHARGE, start_utc=LIFECYCLE_START
        ),
        content=activity_module.RunContent(**params),
    )


def view(**overrides):
    """Return an execution view for a charge run."""
    params = {
        "identity": activity_module.RunIdentity(
            direction=ECONOMIC_DIRECTION_CHARGE, start_utc=LIFECYCLE_START
        ),
        "end_utc": LIFECYCLE_END,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "run_id": "run-1",
        "objective_target_kwh": TARGET_KWH,
        "objective_realized_kwh": 0.0,
        "executed": True,
    }
    params.update(overrides)
    return activity_module.ExecutionView(**params)


def entry_for(state, execution, *, runs=None, now=None, shadow=False):
    """Return the lifecycle entry for one refresh, or ``None``."""
    return activity_module.next_activity(
        previous=state,
        runs=(planned_run(),) if runs is None else runs,
        now=NOW if now is None else now,
        execution=execution,
        shadow=shadow,
    )


def plan_id() -> str:
    """Return the campaign's user-visible id."""
    return activity_module.plan_id_for(
        activity_module.PlanIdentity(
            category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=LIFECYCLE_END
        )
    )


def test_the_three_lines_read_as_the_approved_wording() -> None:
    """The user-facing shape, asserted exactly.

    Three facts and a plan id, on one line each. beta.24's lines carried a power
    as well -- "Charge planned - 8.06 kWh - 2.3 kW - 13:00-16:30" -- and a kW in a
    plan announcement is a figure a reader can do nothing with: it is the
    *first interval's* request, and it is revised every minute of the campaign.
    """
    state = None
    messages = []
    for execution in (
        None,
        view(running=True, activation_confirmed=True),
        view(
            running=False,
            objective_realized_kwh=TARGET_KWH,
            stop_reason=EXECUTION_STOP_TARGET_REACHED,
        ),
    ):
        entry = entry_for(state, execution)
        assert entry is not None
        state = entry.state
        messages.append(entry.message)

    identifier = plan_id()
    assert messages == [
        f"Plan ID: {identifier} — Safety Buy Planned — 13:00-16:30 — 8.06 kWh",
        f"Plan ID: {identifier} — Buy Started — Tracking 8.06 kWh",
        f"Finished Plan ID: {identifier} — Success — Target Reached — 8.06 / 8.06 kWh",
    ]


def test_a_planned_line_is_said_once_per_plan() -> None:
    """Twenty refreshes, one line."""
    state = None
    lines = []
    for _ in range(20):
        entry = entry_for(state, None)
        if entry is not None:
            lines.append(entry.message)
            state = entry.state

    assert len(lines) == 1
    assert "Safety Buy Planned" in lines[0]


def test_a_started_line_is_said_once_per_plan() -> None:
    """The activation succeeds once; twenty sustaining refreshes say nothing."""
    state = entry_for(None, None).state
    first = entry_for(state, view(running=True, activation_confirmed=True))
    assert first is not None
    state = first.state

    quiet = [
        entry_for(state, view(running=True, activation_confirmed=False))
        for _ in range(20)
    ]

    assert first.message == f"Plan ID: {plan_id()} — Buy Started — Tracking 8.06 kWh"
    assert quiet == [None] * 20


def test_started_is_never_said_from_an_armed_decision() -> None:
    """**The distinction the brief called exact, and it is.**

    An armed decision has computed a power and sent nothing. In Live, saying
    "started" about it would be a claim about a battery that has not moved.
    """
    state = entry_for(None, None).state
    entry = entry_for(state, view(running=True, activation_confirmed=False))

    assert entry is None


def test_shadow_says_nothing_at_all_rather_than_would_start() -> None:
    """Stronger than beta.24's careful wording, and simpler.

    beta.24 emitted a ``would_start`` kind with "no command sent" appended, so a
    Shadow history looked exactly as busy as a Live one and every line needed the
    disclaimer to stay honest. Shadow now shows the planning lifecycle and stops:
    a line that does not exist cannot read like a live one.
    """
    state = entry_for(None, None, shadow=True).state
    entry = entry_for(
        state,
        view(running=True, executed=False, activation_confirmed=True),
        shadow=True,
    )

    assert entry is None


def test_a_plan_says_planned_started_and_finished_and_nothing_else() -> None:
    """A whole campaign: twenty-two refreshes, three lines."""
    state = None
    messages = []

    def step(execution, runs=None) -> None:
        nonlocal state
        entry = entry_for(state, execution, runs=runs)
        if entry is not None:
            messages.append(entry.message)
            state = entry.state

    for _ in range(5):
        step(None)
    step(view(running=True, activation_confirmed=True))
    # Sixteen sustaining refreshes, the power moving on each. Not one of them is a
    # decision, and Activity is not even told what the power is.
    for delivered in (1.0, 2.0, 3.0, 4.0):
        for _ in range(4):
            step(view(running=True, objective_realized_kwh=delivered))
    step(
        view(
            running=False,
            objective_realized_kwh=TARGET_KWH,
            stop_reason=EXECUTION_STOP_TARGET_REACHED,
        )
    )

    identifier = plan_id()
    assert messages == [
        f"Plan ID: {identifier} — Safety Buy Planned — 13:00-16:30 — 8.06 kWh",
        f"Plan ID: {identifier} — Buy Started — Tracking 8.06 kWh",
        f"Finished Plan ID: {identifier} — Success — Target Reached — 8.06 / 8.06 kWh",
    ]


def test_a_second_campaign_announces_itself() -> None:
    """Deduplication is per plan, not for ever. Two plans, two sets of lines."""
    second_end = LIFECYCLE_END + timedelta(hours=4)
    second = activity_module.PlannedRun(
        identity=activity_module.RunIdentity(
            direction=ECONOMIC_DIRECTION_CHARGE,
            start_utc=second_end - timedelta(hours=1),
        ),
        content=activity_module.RunContent(
            category=ACTIVITY_CATEGORY_SAFETY_BUY,
            energy_kwh=4.0,
            end_utc=second_end,
            window="20:30-21:30",
        ),
    )
    state = None
    messages = []

    def step(execution, runs, now) -> None:
        nonlocal state
        entry = entry_for(state, execution, runs=runs, now=now)
        if entry is not None:
            messages.append(entry.message)
            state = entry.state

    step(None, (planned_run(),), NOW)
    step(view(running=True, activation_confirmed=True), (planned_run(),), NOW)
    step(
        view(running=False, stop_reason=EXECUTION_STOP_STAGE_A_HOLD),
        (planned_run(),),
        NOW,
    )
    step(None, (second,), second_end - timedelta(hours=1))
    step(
        view(
            run_id="run-2",
            end_utc=second_end,
            objective_target_kwh=4.0,
            running=True,
            activation_confirmed=True,
        ),
        (second,),
        second_end - timedelta(hours=1),
    )

    assert len(messages) == 5, messages
    assert "Safety Buy Planned — 20:30-21:30" in messages[3]
    assert "Buy Started — Tracking 4.00 kWh" in messages[4]
    # And the two campaigns carry different ids, so a history reader can separate
    # them without reading the clock.
    second_id = activity_module.plan_id_for(
        activity_module.PlanIdentity(
            category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=second_end
        )
    )
    assert plan_id() != second_id
    assert second_id in messages[3]


def test_no_line_is_produced_by_a_republication_or_a_revision() -> None:
    """Structural: there is no path from any of those to an entry.

    The view Activity is handed carries no plan id, no revision, no publication and
    no budget -- so a change in one of them cannot reach a line even in principle.
    """
    fields = set(activity_module.ExecutionView.__dataclass_fields__)

    assert "plan_id" not in fields
    assert "revision" not in fields
    assert "grid_charged_kwh" not in fields
    assert "publication" not in fields


# ===========================================================================
# G. Shadow is still zero-actuation
# ===========================================================================


async def test_a_shadow_charge_writes_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank, writes: list
) -> None:
    """The claim beta.24 must not weaken: Shadow computes everything and sends none."""
    from custom_components.alpha_ems_manager.const import CONTROL_MODE_SHADOW

    from .forecast_helpers import NORMAL, local, refresh_at
    from .test_stage_b_runtime import prepared

    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_SHADOW)
    for quarter in range(8):
        await refresh_at(
            coordinator, local(NORMAL, 10 + quarter // 4, (quarter % 4) * 15)
        )

    report = coordinator.control_report or {}
    execution = report.get("execution") or {}

    assert writes == []
    assert (execution.get("power") or {}).get("applied_kw") in (0.0, None)
    assert (execution.get("power") or {}).get("executed") is False
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"
    assert coordinator.store.execution_record is None


async def test_upgrading_does_not_enable_live(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A user who never opted in is exactly as safe as they were on beta.23."""
    coordinator = setup_integration.runtime_data

    assert coordinator.config.control_execution_enabled is False
    assert coordinator.control_mode == "off"


# ===========================================================================
# H. mutations -- each defect, restored, must fail
# ===========================================================================


def test_widening_the_capability_to_discharge_is_caught() -> None:
    """The mutation is one word in a frozenset, and it is the whole barrier."""
    from custom_components.alpha_ems_manager import alphaess_adapter as adapter

    steps = plan_commands(build_command(make_intent(energy_ac_kwh=0.5)))
    original = adapter.CONTROL_EXECUTABLE_ACTIONS
    try:
        adapter.CONTROL_EXECUTABLE_ACTIONS = frozenset(
            {ACTION_CHARGE, ACTION_DISCHARGE}
        )
        assert adapter.steps_outside_capability(steps) == ()
    finally:
        adapter.CONTROL_EXECUTABLE_ACTIONS = original

    assert adapter.steps_outside_capability(steps)
    assert ACTION_DISCHARGE not in CONTROL_EXECUTABLE_ACTIONS


def test_gating_the_sustain_on_a_power_change_is_caught() -> None:
    """The §G defect: a constant-power run would never refresh its dead-man.

    Reproduced as the predicate rather than by editing the module: with the
    materiality test as the *only* gate, a run holding steady writes nothing at all,
    and a dead-man that is never rewritten expires.
    """
    command = charge_command(3.0)
    material = False  # power unchanged, which is the common case

    mutated_steps = plan_commands(command) if material else ()
    honest_steps = plan_sustain(command)

    assert mutated_steps == ()
    assert CHARGE_FAMILY.duration in [step.entity_id for step in honest_steps]


def test_deriving_started_from_the_controller_state_is_caught() -> None:
    """The mutation is the beta.23 condition, reproduced exactly."""
    execution = view(running=True, activation_confirmed=False)
    mutated = execution.running and bool(execution.intent)
    honest = execution.activation_confirmed and bool(execution.intent)

    assert mutated is True
    assert honest is False
    # Driven from the state where the plan has already been announced, so the only
    # line the refresh could produce is the start -- and it does not.
    assert entry_for(entry_for(None, None).state, execution) is None


def test_keying_the_lifecycle_on_the_intent_is_caught() -> None:
    """Two campaigns of the same intent must announce themselves twice.

    beta.31 moves the key again, and further: the lifecycle is the *plan's*, so two
    campaigns of the same intent in different windows carry different plan ids
    before Stage B has admitted either of them. The run id still separates them
    once it exists, which is what attaches a dispatch to the plan announced for it.
    """
    first = view()
    second = view(run_id="run-2", end_utc=LIFECYCLE_END + timedelta(hours=4))

    assert first.intent == second.intent
    assert first.run_id != second.run_id
    assert first.end_utc != second.end_utc


def test_clearing_the_record_before_the_reset_lands_is_caught() -> None:
    """Read structurally: the claim is released only once the stop has landed."""
    from custom_components.alpha_ems_manager import coordinator as module

    with open(module.__file__, encoding="utf-8") as handle:
        text = handle.read()

    marker = 'report["state"] = CONTROL_STATE_EXECUTED'
    clear = "if self._pending_is_reset:\n                self._clear_execution_record()"

    assert marker in text
    assert clear in text
    assert text.index(marker) < text.index(clear)


def test_releasing_the_marker_before_deactivation_is_caught() -> None:
    """The reset order, asserted on the real step list."""
    steps = [step.entity_id for step in plan_reset(ACTION_CHARGE)]

    assert steps[0] == CHARGE_FAMILY.activate
    assert steps[-1] == BOOLEAN_EXECUTION_OWNER


def test_activating_before_configuring_is_caught() -> None:
    """The arm order, on both sequences that can start a dispatch."""
    for steps in (plan_commands(charge_command()), plan_sustain(charge_command())):
        entities = [step.entity_id for step in steps]
        assert entities[-1] == CHARGE_FAMILY.activate
        assert entities.index(CHARGE_FAMILY.activate) == len(entities) - 1


def test_a_discharge_reset_is_not_reachable_and_would_be_refused() -> None:
    """beta.24 never arms a discharge, so it never resets one -- and could not."""
    assert steps_outside_capability(plan_reset(ACTION_DISCHARGE))
    assert DISCHARGE_FAMILY.activate in steps_outside_capability(
        plan_reset(ACTION_DISCHARGE)
    )


def test_the_permitted_service_set_did_not_grow() -> None:
    """Three, and no ``timer.cancel``."""
    assert len(PERMITTED_SERVICES) == 4
    assert ("timer", "cancel") not in PERMITTED_SERVICES


# ===========================================================================
# I. a real Live campaign, driven through the coordinator
# ===========================================================================


class LiveSurface:
    """A control surface that actually responds to what is written to it.

    **The other tests in this file assert what would be sent; this one closes the
    loop.** Recording handlers alone cannot prove a Live campaign works, because
    ownership depends on the device *answering*: the marker has to come on, the
    dispatch-start register has to become non-zero, and the helper timer has to
    move. With handlers that only record, ownership would never settle and a
    multi-refresh run could not be tested at all.

    So this stands in for the vendor package. It is deliberately simple -- writes
    land in the state machine, activation starts a dispatch and advances the timer,
    deactivation stops both -- and every fact it invents is one the real surface was
    measured producing during the beta.20 hardware gates.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Register the three permitted services and start the clock at rest."""
        self.hass = hass
        self.calls: list = []
        self.dispatch_seconds = 0.0
        self.timer_finishes_at: datetime | None = None
        #: Every deadline this surface ever armed, in order. Kept because a run that
        #: ends correctly clears the live one, so the *history* is what shows the
        #: dead-man was being refreshed while the run was alive.
        self.deadlines: list[datetime] = []
        self._now = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)

        async def turn_on(call) -> None:
            self._apply(call, "on")

        async def turn_off(call) -> None:
            self._apply(call, "off")

        async def set_value(call) -> None:
            self._apply(call, str(call.data["value"]))

        async def select_option(call) -> None:
            # The mode is a *label*, which the vendor package parses a number out
            # of -- so the harness stores the label rather than a number.
            self._apply(call, call.data["option"])

        hass.services.async_register("input_boolean", "turn_on", turn_on)
        hass.services.async_register("input_boolean", "turn_off", turn_off)
        hass.services.async_register("input_number", "set_value", set_value)
        hass.services.async_register("input_select", "select_option", select_option)

    def at(self, moment: datetime) -> None:
        """Set this surface's clock to the instant the coordinator is about to use.

        **Driven from the coordinator's own clock, not an independent one**, and that
        was a real harness bug rather than a nicety. ``dispatch_start`` is a
        seconds-since-midnight register, and the ownership layer rebuilds an instant
        from it using the refresh's ``now``. A surface ticking its own clock produced
        a register a few hours out, ownership never settled, and the failure looked
        exactly like a production fault in the claim window.
        """
        self._now = moment

    def _apply(self, call, value: str) -> None:
        self.calls.append(call)
        entity_id = call.data["entity_id"]
        self.hass.states.async_set(entity_id, value)
        if entity_id == DISPATCH_DURATION and self.dispatch_seconds:
            # **Writing the duration while the dispatch is on re-arms the
            # dead-man**, which is the vendor behaviour the alternation exists to
            # trigger: the automation fires on the helper changing state and then
            # cancels and restarts the timer. Same value, no state change, no
            # re-arm -- which is exactly what the alternation prevents.
            self._rearm(float(value))
            return
        if entity_id != DISPATCH_ENABLE:
            return
        if value == "on":
            # Enabling starts a dispatch and arms the dead-man. Both are readbacks
            # the ownership rule depends on.
            # The same reconstruction the ownership layer performs, from the same
            # instant: seconds since the refresh day's midnight.
            midnight = self._now.replace(hour=0, minute=0, second=0, microsecond=0)
            self.dispatch_seconds = (self._now - midnight).total_seconds()
            self._rearm(float(self.hass.states.get(DISPATCH_DURATION).state))
        else:
            self.dispatch_seconds = 0.0
            self.timer_finishes_at = None
            self.hass.states.async_set(DISPATCH_TIMER, "idle", {})
        self.hass.states.async_set(SENSOR_DISPATCH_START, str(self.dispatch_seconds))

    def _rearm(self, minutes: float) -> None:
        """Set the Dispatch dead-man, as writing the duration helper does."""
        self.timer_finishes_at = self._now + timedelta(minutes=minutes)
        self.deadlines.append(self.timer_finishes_at)
        self.hass.states.async_set(
            DISPATCH_TIMER,
            "active",
            {"finishes_at": self.timer_finishes_at.isoformat()},
        )

    def steps_of(self, entity_id: str) -> list:
        """Return every write made to one entity, in order."""
        return [call for call in self.calls if call.data["entity_id"] == entity_id]


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def charge_now_price(index: int, moment) -> float:
    """Return a wholesale day with a deep morning trough and a dear evening.

    **Why this fixture had to change in beta.31, and it is the point of the whole
    release.** These suites drive a Live charge at 10:00 and then assert things
    about executing it. Until beta.31 the charge appeared for a reason nobody had
    asked for: the whole-horizon autonomy reserve sat above stored energy, the
    objective compared ``(violation, cost)`` lexicographically, and so a purchase
    was *compulsory* whatever the price. The default sawtooth here -- six cents of
    wholesale spread -- never had to justify anything.

    Reachability makes no purchase compulsory when the pack can hold its floor, so
    a fixture that wants a charge now has to say **why**: cheap now, dear later.
    That is a strictly better fixture. It exercises the same Stage-B machinery
    through the path production will actually take, and if the economics ever stop
    forming a charge here, that is a real finding rather than a broken mock.

    A plausible Dutch shape: cheap overnight, cheap again around midday when the
    sun is on the system, dear through the evening peak. Two troughs because these
    suites refresh at two different times -- some at 00:00-01:15, some at
    10:00-11:00 -- and both need a reason to buy.

    Two details are load bearing, and both were found by watching the optimiser
    correctly decline to do what the fixture wanted:

    * **Each trough rises** rather than being flat. Across a flat trough every
      quarter is an equally good place to buy, so the search picks one arbitrarily
      and may defer the charge past the moment the fixture refreshes at.
    * **The overnight trough is strictly the cheaper of the two.** With both
      starting at the same price, a plan made at 00:15 can see the 09:00 trough and
      is right to wait for it -- which is good economics and a useless fixture.

    Indices are quarter-hours from local midnight.
    """
    del moment
    if index < 20:  # 00:00-05:00, the deepest trough
        return 0.005 + 0.001 * index
    if 36 <= index < 52:  # 09:00-13:00, a shallower one
        return 0.06 + 0.002 * (index - 36)
    if 68 <= index < 92:  # 17:00-23:00, the evening peak
        return 0.34
    return 0.12


def sell_now_price(index: int, moment) -> float:
    """Return a day where the profitable move now is to **sell**, not to buy.

    The mirror of :func:`charge_now_price`, and it exists for the suites that are
    about the *advisory* half of the economic surface. Only a charge is executable
    in this release, so those suites need runs whose action is deliberately not --
    an export or a discharge -- and a buy-shaped fixture would hand them a charge
    line that correctly carries no advisory disclaimer.

    Dear 10:00-13:00 so selling now pays, cheap 14:00-19:00 so refilling later is
    cheap, ordinary otherwise. Indices are quarter-hours from local midnight.
    """
    del moment
    if 40 <= index < 52:
        return 0.40
    if 56 <= index < 76:
        return 0.02
    return 0.12


async def live_coordinator(hass: HomeAssistant, config_data: dict):
    """Return a loaded coordinator with command sending enabled and Live selected."""
    from custom_components.alpha_ems_manager.const import (
        CONF_CONTROL_EXECUTION_ENABLED,
        CONFIG_ENTRY_VERSION,
        DOMAIN,
    )

    from .test_control_modes import set_mode

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=config_data,
        options={CONF_CONTROL_EXECUTION_ENABLED: True},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    return entry.runtime_data


async def drive_live_charge(
    hass: HomeAssistant,
    config_data: dict,
    frank,
    live_surface: LiveSurface,
    *,
    quarters: int,
):
    """Drive a Live charge campaign for ``quarters`` refreshes and return the trace."""
    from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
    from .frank_capture import synthetic_day
    from .test_economic_published import allow_trading

    coordinator = await live_coordinator(hass, config_data)
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)

    trace = []
    for quarter in range(quarters):
        moment = local(NORMAL, 10 + quarter // 4, (quarter % 4) * 15)
        live_surface.at(moment)
        await refresh_at(coordinator, moment)
        await hass.async_block_till_done()
        report = coordinator.control_report or {}
        execution = report.get("execution") or {}
        carried = (execution.get("carried") or {}).get("run") or {}
        trace.append(
            {
                "state": execution.get("state"),
                "run_id": carried.get("run_id"),
                "ownership": (execution.get("ownership") or {}).get("state"),
                "sequence": (execution.get("write_boundary") or {}).get("sequence"),
                "reset_action": (execution.get("write_boundary") or {}).get(
                    "reset_action"
                ),
                "stop_reason": (execution.get("result") or {}).get("stop_reason"),
                "reset_required": (execution.get("result") or {}).get("reset_required"),
                "refusal": (report.get("authorization") or {}).get("refusal"),
                "planned": report.get("commands_planned"),
                "steps": len(
                    ((execution.get("write_boundary") or {}).get("steps")) or []
                ),
                "authorized": (report.get("authorization") or {}).get("authorized"),
                "calls": len(live_surface.calls),
            }
        )
    return coordinator, trace


async def test_a_live_charge_arms_once_and_is_sustained_thereafter(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**The Live positive path, end to end, and the constant-power proof with it.**

    Eight refreshes of one campaign. What this asserts is the shape the whole design
    rests on: the run is armed once, ownership becomes provable from the readback,
    and every refresh after that **re-arms the dead-man** -- whether or not the
    requested power moved.

    The last part is the one an earlier draft of the plan got wrong. Gating the
    re-arm on a material power change would mean a charge holding steady never
    re-arms, its dead-man is never refreshed, and the dispatch expires mid-run while
    this controller believes it is still going.
    """
    _coordinator, trace = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=5
    )

    executing = [row for row in trace if row["authorized"]]
    assert executing, trace

    # Five refreshes, chosen so the campaign is still under way throughout: this
    # test is about arming and sustaining, and each way a run can *stop* gets its
    # own test that forces its own condition rather than depending on where a
    # synthetic day happens to end one.
    moving = [row for row in executing if row["sequence"] != "reset"]
    assert moving == executing, trace
    assert len({row["run_id"] for row in moving}) == 1, trace

    # Ownership became provable from the device's own readback.
    assert any(row["ownership"] == OWNERSHIP_OWNED for row in executing), trace

    assert moving[0]["sequence"] == "arm"

    # **The dead-man was rewritten on every executing refresh**, which is the
    # guarantee, and it holds whichever sequence ran -- arming writes the duration
    # too, and a sustain writes nothing else that matters.
    assert len(live_surface.steps_of(DISPATCH_DURATION)) == len(executing)
    # **The enable is written once, not once per refresh**, and that is a beta.25
    # improvement rather than a weaker assertion. Writing the duration re-arms the
    # vendor timer on its own, so a sustain needs no enable toggle -- and not
    # toggling it is what keeps the dispatch continuously live instead of
    # momentarily off fifteen times an hour.
    assert len(live_surface.steps_of(DISPATCH_ENABLE)) == 1
    # The dead-man moved forward every time it was re-armed, and it is still live:
    # the run is under way throughout this window, so a cleared timer here would
    # mean the charge had silently stopped.
    assert len(live_surface.deadlines) == len(moving)
    assert all(a < b for a, b in pairwise(live_surface.deadlines))
    assert live_surface.timer_finishes_at is not None

    # No redundant power write: every one carried a value different from the last.
    values = [call.data["value"] for call in live_surface.steps_of(DISPATCH_POWER)]
    assert values
    assert all(a != b for a, b in pairwise(values)), values

    # The marker was only ever turned **on**. Each re-arm re-asserts it, which is a
    # no-op on an already-on boolean; what must never happen mid-run is a release,
    # because until it is off the dispatch is still owned and still stoppable.
    marker_writes = live_surface.steps_of(BOOLEAN_EXECUTION_OWNER)
    assert marker_writes
    # Turned on by each arm, and turned off exactly once -- by the reset, last.
    assert [call.service for call in marker_writes].count("turn_off") <= 1
    assert marker_writes[0].service == "turn_on"

    # The record carried the action a reset needs, and was completed from the
    # readback rather than guessed.
    actions = [row for row in trace if row["reset_action"]]
    assert actions, trace
    assert {row["reset_action"] for row in actions} == {ACTION_CHARGE}


async def test_a_live_campaign_says_three_things(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Eight refreshes, and the Activity feed is not a fifteen-minute log.

    A start line is emitted only because an activation write actually succeeded, and
    the sustaining refreshes after it say nothing at all.
    """
    from homeassistant.const import EVENT_LOGBOOK_ENTRY

    logbook: list = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: logbook.append(event.data))

    await drive_live_charge(hass, config_data, frank, live_surface, quarters=8)

    lifecycle = [entry["message"] for entry in logbook]

    assert lifecycle
    # One start at most, and in Live it can only have come from a confirmed
    # activation -- Shadow emits no start line at all.
    starts = [m for m in lifecycle if " Started — " in m]
    assert len(starts) == 1, lifecycle
    assert "Shadow" not in starts[0]
    # Nothing repeated, and far fewer lines than refreshes.
    assert len(lifecycle) == len(set(lifecycle)), lifecycle
    assert len(lifecycle) < 8, lifecycle


async def test_a_live_charge_never_writes_a_discharge_helper(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Across a whole Live campaign, not one write leaves the charge family."""
    await drive_live_charge(hass, config_data, frank, live_surface, quarters=8)

    # **The Live surface is Dispatch and the marker, and nothing else.** The
    # invariant is unchanged from beta.24 -- no discharge helper is ever written --
    # but the family it is asserted against is the one that now executes.
    permitted = set(DISPATCH_ENTITIES) | {BOOLEAN_EXECUTION_OWNER}
    touched = {call.data["entity_id"] for call in live_surface.calls}

    assert touched
    assert touched <= permitted, touched - permitted
    assert not touched & set(DISCHARGE_FAMILY.entities)
    # And the Force Charging family is not commanded either: it is read for
    # conflict detection and nothing more.
    assert not touched & set(CHARGE_FAMILY.entities)


async def test_a_run_whose_power_holds_still_is_sustained_not_re_armed(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The constant-power case, made deterministic instead of hoped for.**

    On a synthetic day the rolling power moves every quarter, so the campaign above
    legitimately re-arms. Here the materiality predicate is pinned to "unchanged" --
    the condition a real multi-hour charge at steady power presents -- and what is
    asserted is the *wiring*: that the coordinator then chooses the sustain sequence,
    still writes the dead-man, and does **not** write the power helper.

    The predicate itself is tested on its own against the deadband; this is the
    branch it selects. Splitting them is deliberate: a single test that had to
    produce constant power *and* check the consequence would be asserting an
    accident.
    """
    coordinator, trace = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=2
    )
    armed = [row for row in trace if row["authorized"]]
    assert armed, trace
    assert armed[-1]["sequence"] == "arm"

    from .forecast_helpers import NORMAL, local, refresh_at

    # **No patch is needed to hold the power still any more, and that is the
    # point.** beta.24 gated the sustain on ``_power_moved_materially``, so the
    # test had to force that predicate. beta.25 decides materiality with the
    # deadband in ``dispatch.decide``, from live measurements that do not move in
    # this harness -- so a steady setpoint is the natural outcome rather than a
    # forced one, and the assertion below is about real behaviour.
    before = len(live_surface.steps_of(DISPATCH_POWER))
    duration_before = len(live_surface.steps_of(DISPATCH_DURATION))
    deadline_before = live_surface.timer_finishes_at

    for quarter in (2, 3):
        moment = local(NORMAL, 10, quarter * 15)
        live_surface.at(moment)
        await refresh_at(coordinator, moment)
        await hass.async_block_till_done()

    report = coordinator.control_report or {}
    boundary = (report.get("execution") or {}).get("write_boundary") or {}

    assert boundary.get("sequence") == "sustain"
    # The dead-man was rewritten on both sustaining refreshes, and the timer moved.
    assert len(live_surface.steps_of(DISPATCH_DURATION)) == duration_before + 2
    assert live_surface.timer_finishes_at is not None
    assert deadline_before is not None
    assert live_surface.timer_finishes_at > deadline_before
    # **At most one power write per refresh, and only a material one.**
    #
    # The old assertion here was "the power helper was left exactly as it was",
    # which is no longer the right claim: Stage A publishes a fresh
    # ``desired_grid_kw`` on every quarter boundary, so a correction at a boundary
    # is the controller doing its job rather than churn. What must hold is that no
    # write is redundant and none is duplicated -- the deadband decision, asserted
    # here on the real path and exhaustively in the dispatch arithmetic tests.
    written = [call.data["value"] for call in live_surface.steps_of(DISPATCH_POWER)]
    assert len(written) - before <= 2, written
    assert all(a != b for a, b in pairwise(written)), written
    # One run throughout: a sustain is a continuation, not a new campaign.
    run_id = ((report.get("execution") or {}).get("carried") or {}).get("run") or {}
    assert run_id.get("run_id") == armed[-1]["run_id"]


async def test_a_sustain_is_never_refused_by_the_cooldown(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The exemption without which the unconditional dead-man refresh is unreachable.

    The cooldown is a quarter of an hour and so is the refresh interval, so a re-arm
    sits exactly on the boundary. A cooldown that refused a sustain would expire the
    run it was protecting -- so a sustaining refresh is treated as the continuation
    it is rather than as a start.
    """
    coordinator, _ = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=2
    )

    from .forecast_helpers import NORMAL, local, refresh_at

    monkeypatch.setattr(
        type(coordinator), "_power_moved_materially", lambda self, command: False
    )
    # A write moments ago, which is what a fifteen-minute cadence produces.
    coordinator._last_control_write = local(NORMAL, 10, 30)

    moment = local(NORMAL, 10, 30)
    live_surface.at(moment)
    await refresh_at(coordinator, moment)
    await hass.async_block_till_done()

    report = coordinator.control_report or {}
    authorization = report.get("authorization") or {}

    assert authorization.get("refusal") != "cooldown"
    assert authorization.get("authorized") is True


# ===========================================================================
# J. the three gaps the mutation run found
# ===========================================================================


def test_the_headroom_branch_reports_the_headroom_reason() -> None:
    """**Found by mutation: nothing asserted which reason the branch actually uses.**

    Section E asserted that the two constants differ and that the phrase exists,
    which is true whichever reason the branch reports -- so restoring
    ``target_reached`` there survived the whole suite. This drives the branch: a pack
    already at the plan's stored-energy ceiling, so the cap reduces the request to
    nothing.

    The distinction is worth a test because the two sentences say opposite things
    about the same run. "Complete" means the plan was met; "stopped for headroom"
    means the pack ran out of room with energy still to buy.
    """
    decision = decide(
        mode_executes=True,
        mode_off=False,
        targets=[charge_publication(WINDOW_START)],
        now=WINDOW_START + timedelta(minutes=30),
        evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
        progress=progress_of(0.5),
        # The plan lands the pack at 18.0 kWh and it is already there.
        current_energy_kwh=18.0,
        remaining_expected_pv_kwh=0.0,
        charge_efficiency=0.948683,
    )

    assert decision.demand is not None
    assert decision.demand.finished is True
    assert decision.stop_reason == EXECUTION_STOP_HEADROOM_REACHED
    assert decision.stop_reason != EXECUTION_STOP_TARGET_REACHED
    state = entry_for(None, None).state
    state = entry_for(state, view(running=True, activation_confirmed=True)).state
    ended = entry_for(
        state,
        view(
            running=False, objective_realized_kwh=6.2, stop_reason=decision.stop_reason
        ),
    )
    assert ended is not None
    assert ended.kind == ECONOMIC_EVENT_CANCELLED
    assert ended.message == (
        f"Canceled Plan ID: {plan_id()} — Headroom Reached — 6.20 / 8.06 kWh"
    )


def test_write_refusal_rejects_a_raw_dispatch_write_on_its_own() -> None:
    """**Found by mutation: the outer interlock was covering for the inner one.**

    Removing the raw-surface check from ``write_refusal`` survived, because
    ``steps_outside_capability`` catches a raw write too. Defence in depth is only
    defence if each layer is verified separately, so this asserts the inner one
    without the outer one standing behind it.

    The raw surface matters more than most: its power field is **signed** and its
    convention is the opposite of the helpers', so a value that leaks across would
    charge when it meant to discharge.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        CommandStep,
        write_refusal,
    )
    from custom_components.alpha_ems_manager.const import (
        CONTROL_REFUSE_RAW_DISPATCH_WRITE,
    )

    command = charge_command()
    for entity in (
        SENSOR_DISPATCH_START,
        SENSOR_DISPATCH_MODE,
        SENSOR_DISPATCH_ACTIVE_POWER,
    ):
        steps = (
            *plan_commands(command),
            CommandStep("input_number", "set_value", entity, 1.0),
        )
        assert write_refusal(command, steps) == CONTROL_REFUSE_RAW_DISPATCH_WRITE, (
            entity
        )

    # And the honest list is accepted, so the check above is not refusing everything.
    assert write_refusal(command, plan_commands(command)) is None


def test_write_refusal_rejects_a_negative_magnitude_on_its_own() -> None:
    """The other inner check the outer interlock would have covered for.

    The helper takes an **unsigned** battery rate -- measured: +1.0 kW charges -- so a
    negative value there is either a sign confusion or a raw-surface value that has
    leaked in. Both are refused whole.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        CommandStep,
        write_refusal,
    )
    from custom_components.alpha_ems_manager.const import (
        CONTROL_REFUSE_NEGATIVE_MAGNITUDE,
    )

    command = charge_command()
    steps = (
        CommandStep("input_number", "set_value", CHARGE_FAMILY.power, -2.3),
        CommandStep("input_boolean", "turn_on", CHARGE_FAMILY.activate),
    )

    assert write_refusal(command, steps) == CONTROL_REFUSE_NEGATIVE_MAGNITUDE


def test_a_supersession_of_the_same_intent_still_announces_the_new_run() -> None:
    """**Found by mutation: intent-keying survived because a stop had reset it.**

    ``test_a_second_campaign_announces_itself`` walks through a stop between the two
    runs, and the stop clears the intent memory -- so keying on the intent instead of
    the run id made no difference there and the mutation lived.

    This is the case that separates them: one charge run replaced by another with
    **no intervening stop line**, which is what a supersession looks like. Keyed on
    the run id the second run announces itself; keyed on the intent it would be
    silently swallowed, and a user would watch a different campaign under the last
    campaign's headline.
    """
    second_end = LIFECYCLE_END + timedelta(hours=4)
    second = activity_module.PlannedRun(
        identity=activity_module.RunIdentity(
            direction=ECONOMIC_DIRECTION_CHARGE,
            start_utc=second_end - timedelta(hours=1),
        ),
        content=activity_module.RunContent(
            category=ACTIVITY_CATEGORY_SAFETY_BUY,
            energy_kwh=4.0,
            end_utc=second_end,
            window="20:30-21:30",
        ),
    )
    state = entry_for(None, None).state
    first = entry_for(state, view(running=True, activation_confirmed=True))
    assert first is not None
    assert first.state.open[0].run_id == "run-1"

    # The second campaign replaces the first with no intervening stop: the plan
    # simply holds a different run next refresh.
    later = second_end - timedelta(hours=1)
    cancelled = entry_for(first.state, None, runs=(second,), now=later)
    assert cancelled is not None
    assert cancelled.kind == ECONOMIC_EVENT_CANCELLED

    announced = entry_for(cancelled.state, None, runs=(second,), now=later)
    assert announced is not None
    assert "Safety Buy Planned — 20:30-21:30" in announced.message

    started = entry_for(
        announced.state,
        view(
            run_id="run-2",
            end_utc=second_end,
            objective_target_kwh=4.0,
            running=True,
            activation_confirmed=True,
        ),
        runs=(second,),
        now=later,
    )
    assert started is not None
    assert " Buy Started — " in started.message
    assert started.state.open[0].run_id == "run-2"


def test_the_planned_line_is_also_keyed_on_the_plan_rather_than_the_intent() -> None:
    """The same distinction, on the other end of the lifecycle.

    Two campaigns of the same intent in different windows are two plans, and each
    is announced once. Under beta.30's intent keying the second was swallowed and a
    user watched a different campaign under the last campaign's headline.
    """
    second_end = LIFECYCLE_END + timedelta(hours=4)
    first_id = plan_id()
    second_id = activity_module.plan_id_for(
        activity_module.PlanIdentity(
            category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=second_end
        )
    )

    assert first_id != second_id


# ===========================================================================
# K. the seven stop sequences, each forcing its own condition
# ===========================================================================
#
# **Every one of these was measured refusing before the amendment**, with the same
# symptom each time: the controller decided to stop, the reset list was built after
# the authorisation that would have permitted it, and the start path refused a
# command that had no intent. So each sequence here forces one stop condition and
# asserts the physical consequence, rather than depending on where a synthetic day
# happens to end a run.

#: **The Dispatch stop, in order.** Enable off first, so an interrupted stop
#: leaves the dispatch off rather than half-cleaned; the resting values next,
#: because a dispatch left armed at zero still holds a duration and a cutoff a
#: later run would inherit; the photovoltaic switch back to its fail-safe on; and
#: the marker last, because until it is off the dispatch is still owned.
RESET_ORDER = (
    DISPATCH_ENABLE,
    DISPATCH_POWER,
    DISPATCH_DURATION,
    DISPATCH_CUTOFF_SOC,
    DISPATCH_PV_SWITCH,
    BOOLEAN_EXECUTION_OWNER,
)


async def owned_live_charge(hass, config_data, frank, live_surface, *, quarters=3):
    """Return a coordinator owning a physically running charge."""
    coordinator, trace = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=quarters
    )
    assert any(row["ownership"] == OWNERSHIP_OWNED for row in trace), trace
    assert hass.states.get(DISPATCH_ENABLE).state == "on"
    assert coordinator.store.execution_record is not None
    live_surface.calls.clear()
    return coordinator


async def step_once(hass, coordinator, live_surface, *, hour=10, minute=45):
    """Drive one more refresh at a given local time and return the control report.

    **The control layer's own exception guard is lifted for the duration.** In
    production a fault there costs the two control entities and nothing else, which
    is right -- but in a test it turns a crash into an empty report, and an empty
    report is indistinguishable from a refusal. These tests are about the difference
    between those two, so the guard has to come off.
    """
    from .forecast_helpers import NORMAL, local, refresh_at

    moment = local(NORMAL, hour, minute)
    live_surface.at(moment)
    kind = type(coordinator)
    guarded = kind._build_control_report_safely
    kind._build_control_report_safely = kind._build_control_report
    try:
        await refresh_at(coordinator, moment)
        await hass.async_block_till_done()
    finally:
        kind._build_control_report_safely = guarded
    return coordinator.control_report or {}


def assert_full_charge_reset(hass, live_surface, report, *, reason: str) -> None:
    """Assert the complete six-step charge reset was authorised and sent, in order."""
    execution = report.get("execution") or {}
    boundary = execution.get("write_boundary") or {}
    authorization = report.get("authorization") or {}

    assert boundary.get("source") == "stage_b_reset", boundary
    assert boundary.get("action") == ACTION_CHARGE, boundary
    assert boundary.get("stop_reason") == reason, boundary
    assert authorization.get("authorized") is True, authorization

    sent = [call.data["entity_id"] for call in live_surface.calls]
    assert sent == list(RESET_ORDER), sent
    # Deactivation first so an interrupted reset leaves the dispatch off, and the
    # marker last so ownership outlives the cleanup that needs it.
    assert sent[0] == DISPATCH_ENABLE
    assert sent[-1] == BOOLEAN_EXECUTION_OWNER
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"


async def test_target_reached_stops_the_charge(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Sequence A.** The happy ending, and it could not be sent before."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    # Measured progress says the target is met.
    monkeypatch.setattr(
        type(coordinator),
        "_execution_progress",
        lambda self, run_id, plan: progress_of(999.0),
    )
    report = await step_once(hass, coordinator, live_surface)

    assert_full_charge_reset(
        hass, live_surface, report, reason=EXECUTION_STOP_TARGET_REACHED
    )
    # The record is released only once the stop has landed -- and it has.
    assert coordinator.store.execution_record is None


async def test_live_to_shadow_stops_the_charge(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Sequence B, the primary emergency abort.**

    Measured before the amendment: zero service calls and Force Charging still on,
    which made the documented abort procedure a fiction. The user selecting Shadow
    *is* the stop request, so refusing it for not being in Live was circular.
    """
    from .test_control_modes import set_mode

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await set_mode(hass, CONTROL_MODE_SHADOW)
    # **The stop happens on the mode-change refresh itself, since beta.30.**
    # Selecting Shadow *is* the stop request, as this test's own name says, and
    # ownership is now provable on that refresh -- so the reset completes there
    # rather than one refresh later. Asserting the report from the refresh that
    # performed it is the stronger statement: it pins that no dispatch of ours
    # survives the mode change even for a single cycle.
    report = coordinator.control_report or {}

    assert report["mode"] == CONTROL_MODE_SHADOW
    assert_full_charge_reset(
        hass, live_surface, report, reason=EXECUTION_STOP_SWITCHED_TO_SHADOW
    )
    assert coordinator.store.execution_record is None

    # And Shadow is Shadow again: nothing further is written.
    live_surface.calls.clear()
    for minute in (0, 15):
        await step_once(hass, coordinator, live_surface, hour=11, minute=minute)
    assert live_surface.calls == []


async def test_live_to_off_stops_the_charge_then_goes_quiet(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Sequence C.** Off used to return before it could even see the dispatch.

    A user selecting Off while their battery is being charged by this integration
    means stop. Off now cleans up after itself once, and is silent afterwards.
    """
    from .test_control_modes import set_mode

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await set_mode(hass, CONTROL_MODE_OFF)
    # **The stop happens on the mode-change refresh itself, since beta.30.**
    # Selecting Off while a charge of ours is running means stop, and ownership is
    # now provable on that refresh -- so the cleanup completes there rather than one
    # refresh later. Asserting the report from the refresh that performed it pins
    # that no dispatch of ours survives the mode change even for a single cycle.
    report = coordinator.control_report or {}

    # ``state`` is relabelled by the send site once a write lands, so the boundary
    # is what says which operation ran.
    boundary = report.get("write_boundary") or {}
    assert boundary.get("source") == "off_reset", boundary
    assert boundary.get("action") == ACTION_CHARGE, boundary
    assert (report.get("authorization") or {}).get("authorized") is True

    sent = [call.data["entity_id"] for call in live_surface.calls]
    assert sent == list(RESET_ORDER), sent
    assert hass.states.get(CHARGE_FAMILY.activate).state == "off"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"
    assert coordinator.store.execution_record is None

    # Silent from here. Off does not become another execution mode.
    live_surface.calls.clear()
    for minute in (0, 15):
        await step_once(hass, coordinator, live_surface, hour=11, minute=minute)
    assert live_surface.calls == []


async def test_stage_a_withdrawal_stops_the_charge(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Sequence D.** A reset with no current charge intent anywhere.

    This is the beta.22 incident's own shape, and the case that shows why a reset
    cannot be authorised through the start path: there is nothing to start.

    **"Anywhere" includes the quarter, since beta.29.** An open ``CarriedQuarter``
    is itself an intent source -- deliberately, because a parent run ending must not
    stop a quarter that has already opened -- so the scenario this test is about
    needs the quarter closed as well as the publications withdrawn. That
    continuation is a different property and has its own test in
    ``test_beta29_quarter_authority_lifecycle``; conflating the two here would leave
    neither pinned.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    # Stage A publishes nothing executable from here on.
    monkeypatch.setattr(
        type(coordinator), "_execution_targets", lambda self, **kwargs: ()
    )
    # And no quarter is open, so there is genuinely no intent anywhere.
    # **The schedule too, since beta.30.** The executing quarter is derived at
    # the top of every tick and refresh, so clearing the derived value alone
    # would be undone immediately -- which is exactly the property that makes a
    # skipped boundary impossible.
    coordinator._plan = None
    coordinator._quarter = None
    coordinator._reset_quarter_progress(None)
    report = await step_once(hass, coordinator, live_surface)

    assert (report.get("intent")) is None
    assert_full_charge_reset(
        hass, live_surface, report, reason=EXECUTION_STOP_STAGE_A_HOLD
    )
    assert coordinator.store.execution_record is None


async def test_an_unsafe_verdict_while_owned_stops_the_charge(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Sequence E, and the sharpest of them.**

    "Safety says do not start this" and "safety prevents us stopping what is already
    running" look alike and are opposites. The second was what the code did: an
    unsafe verdict refused the reset, so a charge that became unsafe kept charging.

    Now an unsafe verdict *while we own an active dispatch* is itself a stop
    condition, and the reset authorisation never reads the verdict -- so an unsafe
    world cannot block the response to itself.
    """
    from custom_components.alpha_ems_manager import coordinator as module
    from custom_components.alpha_ems_manager.safety import SafetyVerdict

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    monkeypatch.setattr(
        module,
        "evaluate",
        lambda intent, context: SafetyVerdict(False, "battery_power_stale", ()),
    )
    report = await step_once(hass, coordinator, live_surface)

    assert_full_charge_reset(hass, live_surface, report, reason=EXECUTION_STOP_SAFETY)
    assert coordinator.store.execution_record is None


async def test_a_deadman_that_did_not_advance_stops_the_charge(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Sequence F.** The measured unknown, and its one behaviour: stop."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    monkeypatch.setattr(
        type(coordinator), "_deadman_is_stale", lambda self, snapshot, run_id: True
    )
    report = await step_once(hass, coordinator, live_surface)

    assert_full_charge_reset(
        hass, live_surface, report, reason=EXECUTION_STOP_TIMER_NOT_REFRESHED
    )
    # No deactivate-and-reactivate anywhere: the run ends, it is not cycled.
    enables = [
        call for call in live_surface.calls if call.data["entity_id"] == DISPATCH_ENABLE
    ]
    assert [call.service for call in enables] == ["turn_off"]


async def test_a_failed_reset_keeps_its_evidence_and_retries(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Sequence G.** A service failure mid-reset must cost the reset, not the run.

    The record is what makes a second attempt possible, so a failed reset that
    cleared it would strand the dispatch as permanently unattributable -- the F16
    fault, reached from the other direction.
    """
    from custom_components.alpha_ems_manager import coordinator as module

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    async def failing(hass_arg, steps):
        raise RuntimeError("the helper refused the write")

    monkeypatch.setattr(module, "async_execute", failing)
    monkeypatch.setattr(
        type(coordinator),
        "_execution_progress",
        lambda self, run_id, plan: progress_of(999.0),
    )
    report = await step_once(hass, coordinator, live_surface)

    # The reset was authorised and attempted, and it failed.
    assert (report.get("authorization") or {}).get("authorized") is True
    assert live_surface.calls == []
    # The evidence survives, so the next refresh can try again.
    assert coordinator.store.execution_record is not None
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "on"

    # And it does: with the executor restored, the same stop lands.
    monkeypatch.undo()
    monkeypatch.setattr(
        type(coordinator),
        "_execution_progress",
        lambda self, run_id, plan: progress_of(999.0),
    )
    report = await step_once(hass, coordinator, live_surface, hour=11, minute=0)

    assert_full_charge_reset(
        hass, live_surface, report, reason=EXECUTION_STOP_TARGET_REACHED
    )
    assert coordinator.store.execution_record is None


# ===========================================================================
# L. the rest of the amendment's contract
# ===========================================================================


def test_the_two_authorisations_answer_different_questions() -> None:
    """**The architectural rule, asserted structurally.**

    A start needs a verdict, a mode, an opt-in and a cooldown. A reset needs proof
    of ownership and nothing else. Reading their signatures is the cheapest way to
    show that neither can quietly acquire the other's conditions.
    """
    import inspect

    from custom_components.alpha_ems_manager.safety import (
        authorize_marker_release,
        authorize_reset,
        authorize_start,
    )

    start = set(inspect.signature(authorize_start).parameters)
    reset = set(inspect.signature(authorize_reset).parameters)
    release = set(inspect.signature(authorize_marker_release).parameters)

    # The start path is handed the world; the stop path is handed a proof.
    assert {"verdict", "context", "starts_or_increases"} <= start
    assert not {"verdict", "context", "starts_or_increases"} & reset
    assert not {"verdict", "context", "starts_or_increases"} & release
    assert "ownership" in reset
    assert "ownership" not in start


def test_a_reset_is_refused_without_proof_of_ownership() -> None:
    """Foreign and unproven stay untouchable, which is the whole entitlement."""
    from custom_components.alpha_ems_manager.const import (
        OWNERSHIP_FOREIGN,
        OWNERSHIP_NONE,
    )
    from custom_components.alpha_ems_manager.safety import authorize_reset

    for ownership in (OWNERSHIP_FOREIGN, OWNERSHIP_UNPROVEN, OWNERSHIP_NONE):
        decision = authorize_reset(
            ownership=ownership,
            stopping_action=ACTION_CHARGE,
            stop_reason=EXECUTION_STOP_TARGET_REACHED,
            steps_planned=6,
        )
        assert decision.authorized is False, ownership
        assert decision.refusal == "reset_not_owned", ownership


def test_a_reset_fails_closed_without_an_action() -> None:
    """A missing action is never defaulted to a charge.

    Guessing what to stop is how a stop becomes a start in the other direction, and
    the guess would be unfalsifiable: there is nothing to check it against.
    """
    from custom_components.alpha_ems_manager.safety import authorize_reset

    decision = authorize_reset(
        ownership=OWNERSHIP_OWNED,
        stopping_action=None,
        stop_reason=EXECUTION_STOP_TARGET_REACHED,
        steps_planned=6,
    )

    assert decision.authorized is False
    assert decision.refusal == "reset_action_unknown"


def test_a_discharge_reset_is_refused_at_both_boundaries() -> None:
    """beta.24 can never own a Live discharge, so it can never reset one."""
    from custom_components.alpha_ems_manager.safety import authorize_reset

    decision = authorize_reset(
        ownership=OWNERSHIP_OWNED,
        stopping_action=ACTION_DISCHARGE,
        stop_reason=EXECUTION_STOP_TARGET_REACHED,
        steps_planned=6,
    )

    assert decision.authorized is False
    assert decision.refusal == REFUSE_LIVE_ACTION_NOT_PERMITTED
    # And the final entity interlock would refuse the steps anyway.
    assert steps_outside_capability(plan_reset(ACTION_DISCHARGE))


def test_a_marker_release_is_refused_while_a_dispatch_is_running() -> None:
    """The one thing the release path must never do."""
    from custom_components.alpha_ems_manager.safety import authorize_marker_release

    running = authorize_marker_release(marker_is_stale=False, steps_planned=1)
    stale = authorize_marker_release(marker_is_stale=True, steps_planned=1)

    assert running.authorized is False
    assert running.refusal == "marker_still_dispatching"
    assert stale.authorized is True


async def test_the_record_carries_the_action_a_reset_needs(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Written at arm time, so the stop path reads a fact rather than inferring one."""
    coordinator, _ = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=3
    )
    record = coordinator.store.execution_record

    assert record is not None
    assert record["action"] == ACTION_CHARGE
    assert record["intent"] == EXECUTION_INTENT_GRID_CHARGE
    assert coordinator._owned_run_action() == ACTION_CHARGE


def test_a_record_without_an_action_falls_back_to_its_intent() -> None:
    """And to nothing at all when the intent is unmapped.

    The fallback exists for a record written by an unreleased build. It derives from
    the intent through the same total map rather than guessing, and an intent Alpha
    EMS cannot own yields ``None`` -- which fails the reset closed.
    """
    assert action_for_intent(EXECUTION_INTENT_GRID_CHARGE) == ACTION_CHARGE
    assert action_for_intent("serve_load") is None
    # **Changed deliberately in beta.27**, and the reason is the stop path:
    # ``net_export`` is executable from beta.27 on, so a stop has to be able to name
    # the direction it is stopping.
    #
    # This maps an intent to a battery *direction* and nothing else. It must never
    # be used to pick an actuator surface -- doing so is what would have routed an
    # export onto the Force Discharging helper family, because that family is where
    # ``ACTION_DISCHARGE`` used to lead. The surface is chosen from
    # ``CONTROL_LIVE_DISPATCH_INTENTS``, keyed on the intent.
    assert action_for_intent("net_export") == ACTION_DISCHARGE
    assert action_for_intent(None) is None


async def test_a_stale_marker_is_released_in_shadow(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    live_surface: LiveSurface,
) -> None:
    """A marker with nothing behind it must not latch on because the mode changed.

    Before the amendment this went through the start path too, so a marker left on
    by a crash could only ever be cleared in Live -- and a user watching Shadow was
    exactly the person who would find one.
    """
    from .test_control_modes import set_mode

    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "on")
    hass.states.async_set(SENSOR_DISPATCH_START, "0")
    await hass.async_block_till_done()
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface)
    boundary = (report.get("execution") or {}).get("write_boundary") or {}

    assert boundary.get("source") == "stale_marker_release", boundary
    assert (report.get("authorization") or {}).get("authorized") is True
    assert [call.data["entity_id"] for call in live_surface.calls] == [
        BOOLEAN_EXECUTION_OWNER
    ]
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"


async def test_a_stale_marker_is_released_in_off(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    live_surface: LiveSurface,
) -> None:
    """The same, in Off, which used to return before it could look."""
    from .test_control_modes import set_mode

    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_OFF)
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "on")
    hass.states.async_set(SENSOR_DISPATCH_START, "0")
    await hass.async_block_till_done()
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface)
    boundary = report.get("write_boundary") or {}

    assert boundary.get("source") == "off_marker_release", boundary
    assert [call.data["entity_id"] for call in live_surface.calls] == [
        BOOLEAN_EXECUTION_OWNER
    ]
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"


async def test_off_writes_nothing_when_it_owns_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    live_surface: LiveSurface,
) -> None:
    """Off is silent in the common case, which is every refresh but the first.

    The amendment gave Off one responsibility. It must not have acquired a second.
    """
    from .test_control_modes import set_mode

    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_OFF)
    live_surface.calls.clear()

    for minute in (0, 15, 30):
        report = await step_once(hass, coordinator, live_surface, minute=minute)
        assert report["state"] == CONTROL_STATE_OFF
        assert (report.get("write_boundary") or {}).get("source") is None

    assert live_surface.calls == []


def test_an_unproven_restart_still_writes_nothing_at_all() -> None:
    """The invariant the amendment must not have loosened.

    ``authorize_reset`` requires ``owned``, and the restart rule requires the record
    to be adoptable. Neither is satisfied by an unprovable dispatch, so the two
    agree: nothing is written and the device dead-man ends it.
    """
    from custom_components.alpha_ems_manager.safety import authorize_reset

    decision = authorize_reset(
        ownership=OWNERSHIP_UNPROVEN,
        stopping_action=ACTION_CHARGE,
        stop_reason="whatever",
        steps_planned=6,
    )

    assert decision.authorized is False
    assert carried_from_record({"run_id": "r", "plan_id": "p", "revision": 1}) is None
