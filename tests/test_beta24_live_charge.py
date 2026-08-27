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
    CONTROL_CUTOFF_MIN_PERCENT,
    CONTROL_EXECUTABLE_ACTIONS,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
    CONTROL_STATE_OFF,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_STOP_HEADROOM_REACHED,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_TIMER_NOT_REFRESHED,
    OWNERSHIP_OWNED,
    OWNERSHIP_PROVENANCE_EXACT,
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


def test_a_stale_claim_is_not_owned() -> None:
    """Outside the window a record proves nothing, and that is the whole bound."""
    old = (NOW - timedelta(seconds=OWNERSHIP_CLAIM_WINDOW_SECONDS + 60)).isoformat()
    evidence = evidence_at(
        NOW, record={"run_id": "run-1", "written_at": old, "dispatch_start": None}
    )

    assert evidence.record_provenance is None
    assert evidence.record_matches is False


def test_a_completed_record_is_matched_exactly() -> None:
    """After stamping the comparison is exact, so a moved register stops matching."""
    stamped = {
        "run_id": "run-1",
        "written_at": (NOW - timedelta(hours=2)).isoformat(),
        "dispatch_start": NOW.isoformat(),
    }
    exact = evidence_at(NOW, record=stamped)
    moved = evidence_at(NOW, record=stamped, dispatch_start=NOW + timedelta(hours=1))

    assert exact.record_provenance == OWNERSHIP_PROVENANCE_EXACT
    assert moved.record_provenance is None


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
    assert activity_module._STOP_PHRASES[EXECUTION_STOP_HEADROOM_REACHED] == "headroom"


# ===========================================================================
# F. the Activity lifecycle: three lines per run, and no more
# ===========================================================================


def view(**overrides):
    """Return an execution view for a charge run."""
    params = {
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "run_id": "run-1",
        "target_kwh": TARGET_KWH,
        "delivered_kwh": 0.0,
        "initial_power_kw": 2.3,
        "window": "13:00-16:30",
        "executed": True,
    }
    params.update(overrides)
    return activity_module.ExecutionView(**params)


def entry_for(state, execution):
    """Return the lifecycle entry for one refresh, or ``None``."""
    return activity_module._execution_entry(state, execution, now=NOW)


def test_the_three_lines_read_as_the_approved_wording() -> None:
    """The user-facing shape, asserted exactly."""
    assert activity_module._prepared_message(view()) == (
        "Charge planned - 8.06 kWh - 2.3 kW - 13:00-16:30"
    )
    assert activity_module._started_message(view()) == (
        "Grid charge started - 8.06 kWh - 2.3 kW"
    )
    assert (
        activity_module._stopped_message(
            view(delivered_kwh=8.06, stop_reason=EXECUTION_STOP_TARGET_REACHED)
        )
        == "Charge complete - 8.06 kWh"
    )


def test_a_planned_line_is_said_once_per_run() -> None:
    """Twenty prepared refreshes, one line."""
    state = activity_module.ActivityState()
    lines = []
    for _ in range(20):
        entry = entry_for(state, view(prepared=True))
        if entry is not None:
            lines.append(entry.message)
            state = entry.state

    assert len(lines) == 1
    assert lines[0].startswith("Charge planned")


def test_a_started_line_is_said_once_per_run() -> None:
    """The activation succeeds once; twenty sustaining refreshes say nothing."""
    state = activity_module.ActivityState()
    first = entry_for(state, view(running=True, activation_confirmed=True))
    assert first is not None
    state = first.state

    quiet = [
        entry_for(state, view(running=True, activation_confirmed=False))
        for _ in range(20)
    ]

    assert first.message == "Grid charge started - 8.06 kWh - 2.3 kW"
    assert quiet == [None] * 20


def test_started_is_never_said_from_an_armed_decision() -> None:
    """**The distinction the brief called exact, and it is.**

    An armed decision has computed a power and sent nothing. In Live, saying
    "started" about it would be a claim about a battery that has not moved.
    """
    state = activity_module.ActivityState()
    entry = entry_for(state, view(running=True, activation_confirmed=False))

    assert entry is None


def test_shadow_says_would_start_and_never_the_live_wording() -> None:
    """Whatever the barrier says. A shadow line must not read like a live one."""
    state = activity_module.ActivityState()
    entry = entry_for(state, view(running=True, executed=False))

    assert entry is not None
    assert entry.kind == "would_start"
    assert "no command sent" in entry.message
    assert not entry.message.startswith("Grid charge started")


def test_a_run_says_planned_started_and_ended_and_nothing_else() -> None:
    """A whole campaign: twenty-two refreshes, three lines."""
    state = activity_module.ActivityState()
    messages = []

    def step(execution) -> None:
        nonlocal state
        entry = entry_for(state, execution)
        if entry is not None:
            messages.append(entry.message)
            state = entry.state

    step(view(prepared=True))
    for _ in range(4):
        step(view(prepared=True))
    step(view(running=True, activation_confirmed=True))
    for power in (2.3, 2.7, 3.1, 2.9):
        for _ in range(4):
            step(view(running=True, initial_power_kw=power, delivered_kwh=1.0))
    step(
        view(
            running=False, delivered_kwh=8.06, stop_reason=EXECUTION_STOP_TARGET_REACHED
        )
    )

    assert messages == [
        "Charge planned - 8.06 kWh - 2.3 kW - 13:00-16:30",
        "Grid charge started - 8.06 kWh - 2.3 kW",
        "Charge complete - 8.06 kWh",
    ]


def test_a_second_campaign_announces_itself() -> None:
    """Deduplication is per run, not for ever. Two runs, two sets of lines."""
    state = activity_module.ActivityState()
    messages = []

    def step(execution) -> None:
        nonlocal state
        entry = entry_for(state, execution)
        if entry is not None:
            messages.append(entry.message)
            state = entry.state

    step(view(prepared=True))
    step(view(running=True, activation_confirmed=True))
    step(view(running=False, stop_reason=EXECUTION_STOP_STAGE_A_HOLD))
    step(view(run_id="run-2", prepared=True))
    step(view(run_id="run-2", running=True, activation_confirmed=True))

    assert len(messages) == 5
    assert messages[3].startswith("Charge planned")
    assert messages[4].startswith("Grid charge started")


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
    assert entry_for(activity_module.ActivityState(), execution) is None


def test_keying_the_lifecycle_on_the_intent_is_caught() -> None:
    """Two campaigns of the same intent must announce themselves twice."""
    first = view()
    second = view(run_id="run-2")

    assert first.intent == second.intent
    assert first.run_id != second.run_id


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
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
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

    lifecycle = [
        entry["message"]
        for entry in logbook
        if entry["message"].startswith(("Charge ", "Grid charge "))
    ]

    assert lifecycle, [entry["message"] for entry in logbook]
    # One start at most, and it is the Live wording -- so it can only have come from
    # a confirmed activation.
    starts = [m for m in lifecycle if m.startswith("Grid charge started")]
    assert len(starts) == 1, lifecycle
    assert "no command sent" not in starts[0]
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
    assert (
        activity_module._stopped_message(
            view(delivered_kwh=6.2, stop_reason=decision.stop_reason)
        )
        == "Charge stopped - headroom - 6.20 / 8.06 kWh"
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
    state = activity_module.ActivityState()

    first = entry_for(state, view(running=True, activation_confirmed=True))
    assert first is not None
    assert first.state.execution.started_run == "run-1"

    second = entry_for(
        first.state, view(run_id="run-2", running=True, activation_confirmed=True)
    )

    assert second is not None
    assert second.message.startswith("Grid charge started")
    assert second.state.execution.started_run == "run-2"


def test_the_planned_line_is_also_keyed_on_the_run_rather_than_the_intent() -> None:
    """The same distinction, on the other end of the lifecycle."""
    state = activity_module.ActivityState()

    first = entry_for(state, view(prepared=True))
    assert first is not None
    assert first.state.execution.planned_run == "run-1"

    second = entry_for(first.state, view(run_id="run-2", prepared=True))

    assert second is not None
    assert second.state.execution.planned_run == "run-2"


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
    report = await step_once(hass, coordinator, live_surface)

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
    report = await step_once(hass, coordinator, live_surface)

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
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    # Stage A publishes nothing executable from here on.
    monkeypatch.setattr(
        type(coordinator), "_execution_targets", lambda self, **kwargs: ()
    )
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
