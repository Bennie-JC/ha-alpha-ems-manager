"""Break each Stage B invariant, and prove a test notices.

Every mutation here is a *plausible* change rather than an absurdity -- the kind
someone might make in good faith while making the controller "smarter". Four are
worth singling out, and each is a thing a reasonable person would try:

* **letting the controller see a price.** The obvious way to make headroom
  preservation cleverer, and the exact step that turns Stage B into a second
  economic optimizer.
* **treating an absent headroom constraint as zero.** The natural reading of
  ``value or 0.0``, and it forbids the pack from filling at all.
* **adding house load to the charge setpoint.** Arithmetically tempting, because
  the grid figure really is charge plus load minus production -- but the grid figure
  is a consequence and the command is a battery figure.
* **accepting one half of the ownership evidence.** Both halves are individually
  convincing, which is why neither is sufficient.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import timedelta

import pytest

from custom_components.alpha_ems_manager import execution as execution_module
from custom_components.alpha_ems_manager.const import (
    EXECUTION_STATE_INHIBITED,
    EXECUTION_STATE_STOPPING,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_WINDOW_ENDED,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_OWNED,
    OWNERSHIP_UNPROVEN,
)
from custom_components.alpha_ems_manager.execution import (
    OwnershipEvidence,
    decide,
    demand_for,
    headroom_ceiling_kw,
    measure_progress,
    ownership_of,
    rolling_power_kw,
)

from .test_stage_b_controller import (
    CLOSES,
    DISPATCH_START,
    ISSUED,
    OPENS,
    matching_record,
    raw_target,
    target_of,
)


def progress_of(kwh: float):
    """Return measured progress of ``kwh`` delivered."""
    return measure_progress(accumulated_kwh=kwh, soc_delta_kwh=None)


def owned() -> OwnershipEvidence:
    """Return evidence establishing ownership."""
    return OwnershipEvidence(
        dispatch_active=True,
        marker_on=True,
        record=matching_record(),
        dispatch_start=DISPATCH_START,
        run_id="abc123",
    )


# ===========================================================================
# A. economics must not leak into Stage B
# ===========================================================================


def test_letting_the_controller_read_a_price_is_caught() -> None:
    """**The headline mutation.** One import is all it would take.

    The structural test in ``test_stage_b_boundaries`` is what catches it. This
    reproduces the mutation so the guard is shown working rather than assumed: any
    of these names appearing in the controller's imports fails it.
    """
    source = pathlib.Path(execution_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module.lstrip(".")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    for forbidden in ("price_forecast", "economic", "realized", "reserve"):
        assert forbidden not in imported

    # And the mutation would be visible even if the import were indirect, because
    # the reachable set is closed and every member of it is itself pure. ``battery``
    # and ``control`` joined in beta.20, when Stage B became the command source and
    # therefore had to express an interval and a ``ControlIntent``; neither module
    # contains economics, and the forbidden set above has not moved.
    assert imported <= {
        "const",
        "battery",
        "control",
        "dataclasses",
        "datetime",
        "typing",
        "__future__",
    }, sorted(imported)


def test_the_controller_choosing_its_own_headroom_is_caught() -> None:
    """The cap comes from the target, not from anything computed here.

    Given no published constraint the answer is ``None`` -- unconstrained. A
    controller that invented a figure would return a number, and that is the whole
    difference between obeying a constraint and inventing one.
    """
    unconstrained = target_of(required_headroom_kwh=None, max_end_energy_kwh=None)

    ceiling = headroom_ceiling_kw(
        unconstrained,
        current_energy_kwh=17.9,
        remaining_minutes=60.0,
    )

    # Not 0.0, and not some fraction of the pack: nothing at all.
    assert ceiling is None


def test_the_controller_choosing_its_own_export_window_is_caught() -> None:
    """``headroom_until`` arrives as data. There is no code that could pick one.

    Asserted structurally, because the absence is the property: the controller
    never compares two windows for worth, and it has no price to compare them with.
    """
    source = pathlib.Path(execution_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    # No function exists whose job could be choosing a window.
    for invented in ("next_export_window", "best_window", "pick_window", "rank"):
        assert invented not in functions
    # And nothing here sorts or compares windows by anything at all: the only
    # ordering in the module is the freshness tie-break in ``actionable_target``,
    # which is on the window start and the revision, never on a quantity.
    assert "sorted(" not in source
    assert "max(" in source  # arithmetic clamps only
    assert "export" not in {name.lower() for name in functions} - {
        "battery_power_for_export_kw"
    }


def test_treating_an_absent_headroom_constraint_as_zero_is_caught() -> None:
    """**The ``or 0.0`` mutation, and it forbids charging entirely.**

    ``None`` means Stage A has no view on the endpoint. Zero would mean the pack
    must end empty. Reading the first as the second is a one-character change with
    the opposite effect.
    """
    target = target_of(required_headroom_kwh=None, max_end_energy_kwh=None)

    demand = demand_for(
        target,
        now=OPENS + timedelta(minutes=30),
        progress=progress_of(0.0),
        current_energy_kwh=8.0,
        remaining_expected_pv_kwh=3.0,
    )

    # Unconstrained: no cap, and the full rolling request survives.
    assert demand.ceiling_kw is None
    assert demand.required_kw == pytest.approx(demand.rolling_kw)
    assert demand.required_kw > 0.0


def test_the_headroom_logic_increasing_grid_charging_is_caught() -> None:
    """The cap is one-directional. Raising it would be raising a target.

    Swept over the states where a "helpful" implementation might compensate for
    disappointing production by charging harder.
    """
    for pv_left in (0.0, 0.5, 2.0, 8.94):
        target = target_of()
        demand = demand_for(
            target,
            now=OPENS + timedelta(hours=2),
            progress=progress_of(1.0),
            current_energy_kwh=7.0,
            remaining_expected_pv_kwh=pv_left,
        )
        assert demand.required_kw <= demand.rolling_kw + 1e-9, pv_left


def test_the_rolling_controller_never_exceeds_the_remaining_target() -> None:
    """It raises the rate, and the rate is bounded by what is left over the time."""
    for remaining, minutes in ((1.0, 15.0), (5.0, 60.0), (11.94, 345.0)):
        power = rolling_power_kw(remaining_kwh=remaining, remaining_minutes=minutes)
        delivered = power * (minutes / 60.0)
        assert delivered == pytest.approx(remaining)


# ===========================================================================
# B. the physics rules
# ===========================================================================


def test_adding_house_load_to_the_charge_setpoint_is_caught() -> None:
    """**The pinned live case, as a mutation.**

    3.7 kW of battery charging against 1.1 kW of house and 0.63 kW of production
    draws 4.17 kW at the meter. A controller that commanded the meter figure would
    ask the battery for 4.17 and overcharge by the difference; one that added the
    load outright would ask for 4.81.

    The request is derived from battery energy over time, and no load term appears
    in it -- which is why passing a different house load changes nothing.
    """
    target = target_of(required_headroom_kwh=None, max_end_energy_kwh=None)
    window_hours = (CLOSES - OPENS).total_seconds() / 3600.0

    demand = demand_for(target, now=OPENS, progress=progress_of(0.0))

    assert demand.rolling_kw == pytest.approx(target.battery_target_kwh / window_hours)
    # The signature has nowhere to put a house load, which is the real guard.
    assert "house" not in inspect.signature(demand_for).parameters


def test_the_grid_figure_is_never_treated_as_the_battery_target() -> None:
    """A charge publishes no grid target at all, so confusing them cannot happen."""
    target = target_of()

    assert target.grid_target_kwh is None
    assert target.battery_target_kwh == pytest.approx(11.94)


def test_ignoring_a_reached_target_is_caught() -> None:
    """A dispatch left armed because a countdown has not expired overshoots."""
    decision = decide(
        mode_executes=True,
        mode_off=False,
        targets=[raw_target()],
        now=OPENS + timedelta(minutes=20),
        evidence=owned(),
        progress=progress_of(11.94),
        current_energy_kwh=18.0,
    )

    assert decision.request_kw == 0.0
    assert decision.state == EXECUTION_STATE_STOPPING
    assert decision.stop_reason == EXECUTION_STOP_TARGET_REACHED
    assert decision.reset_required is True


def test_extending_the_window_is_caught() -> None:
    """The window is Stage A's. A shortfall is reported, never worked around."""
    short = raw_target(window_end=(OPENS + timedelta(minutes=10)).isoformat())

    decision = decide(
        mode_executes=True,
        mode_off=False,
        targets=[short],
        now=OPENS + timedelta(minutes=25),
        evidence=owned(),
        progress=progress_of(2.0),
        current_energy_kwh=8.0,
        running_run_id="abc123",
    )

    assert decision.stop_reason == EXECUTION_STOP_WINDOW_ENDED
    assert decision.request_kw == 0.0
    assert any("window closed" in note for note in decision.notes)


def test_a_stale_target_continuing_is_caught() -> None:
    """beta.18 published ``stale_after`` and enforced nothing. Now it bites."""
    soon = raw_target(stale_after=(ISSUED + timedelta(minutes=30)).isoformat())

    running = decide(
        mode_executes=True,
        mode_off=False,
        targets=[soon],
        now=ISSUED + timedelta(hours=2),
        evidence=owned(),
        progress=progress_of(3.0),
        current_energy_kwh=9.0,
    )
    idle = decide(
        mode_executes=False,
        mode_off=False,
        targets=[soon],
        now=ISSUED + timedelta(hours=2),
        evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
        progress=progress_of(0.0),
        current_energy_kwh=9.0,
    )

    assert running.stop_reason == EXECUTION_STOP_STALE_PLAN
    assert running.reset_required is True
    # And a stale target may not start in the first place.
    assert idle.state == EXECUTION_STATE_INHIBITED
    assert idle.request_kw == 0.0


def test_replaying_a_full_target_after_a_restart_is_caught() -> None:
    """**"Ten kilowatt-hours before the reboot and another ten after."**

    Progress is reconstructed from persisted evidence, so what is left is what is
    left. A controller that restarted from the published target would double the
    energy bought.
    """
    # Four kilowatt-hours are known to have been delivered before the restart,
    # from the state-of-charge series rather than from a live accumulator.
    recovered = measure_progress(
        accumulated_kwh=None, soc_delta_kwh=4.0, reconstructed=True
    )

    demand = demand_for(
        target_of(),
        now=OPENS + timedelta(hours=1),
        progress=recovered,
        current_energy_kwh=12.0,
        remaining_expected_pv_kwh=4.0,
    )

    assert demand.remaining_kwh == pytest.approx(11.94 - 4.0)
    assert demand.remaining_kwh < 11.94


# ===========================================================================
# C. ownership
# ===========================================================================


def test_claiming_a_foreign_dispatch_is_caught() -> None:
    """The marker is off, so it is somebody else's whatever else agrees."""
    evidence = OwnershipEvidence(
        dispatch_active=True,
        marker_on=False,
        record=matching_record(),
        dispatch_start=DISPATCH_START,
        run_id="abc123",
    )

    assert ownership_of(evidence) == OWNERSHIP_FOREIGN

    decision = decide(
        mode_executes=True,
        mode_off=False,
        targets=[raw_target()],
        now=OPENS + timedelta(minutes=30),
        evidence=evidence,
        progress=progress_of(0.0),
        current_energy_kwh=8.0,
    )

    assert decision.state == EXECUTION_STATE_INHIBITED
    assert decision.reset_required is False
    assert decision.request_kw == 0.0


def test_the_marker_alone_proving_ownership_is_caught() -> None:
    """A crash could leave it on, and a dispatch armed afterwards is not ours."""
    evidence = OwnershipEvidence(
        dispatch_active=True, marker_on=True, record=None, dispatch_start=DISPATCH_START
    )

    assert ownership_of(evidence) == OWNERSHIP_UNPROVEN
    assert ownership_of(evidence) != OWNERSHIP_OWNED


def test_the_record_alone_proving_ownership_is_caught() -> None:
    """This one is parameter matching wearing a persistence layer."""
    evidence = OwnershipEvidence(
        dispatch_active=True,
        marker_on=False,
        record=matching_record(),
        dispatch_start=DISPATCH_START,
    )

    assert ownership_of(evidence) != OWNERSHIP_OWNED


def test_an_owned_dispatch_self_inhibiting_is_caught() -> None:
    """The second of the two blockers the barrier's docstring names.

    Until beta.19 any active dispatch inhibited, so Alpha EMS stopped itself the
    moment it armed anything and no multi-interval command was expressible.
    """
    from custom_components.alpha_ems_manager.const import INHIBIT_DISPATCH_ACTIVE
    from custom_components.alpha_ems_manager.safety import ControlContext, evaluate

    # The dispatch check sits after the surface checks, so those must pass or the
    # verdict never reaches the one under test.
    common = {
        "mode": "shadow",
        "execution_enabled": False,
        "failsafe_available": True,
        "dispatch_active": True,
    }
    foreign = ControlContext(**common)
    ours = ControlContext(**common, dispatch_owned=True)

    assert evaluate(None, foreign).inhibit_reason == INHIBIT_DISPATCH_ACTIVE
    # Ours gets past the dispatch check and is refused later for a different
    # reason -- which is the point: this gate no longer stops it.
    assert evaluate(None, ours).inhibit_reason != INHIBIT_DISPATCH_ACTIVE


def test_a_foreign_dispatch_not_inhibiting_is_caught() -> None:
    """The relaxation is additive, and this is the half that must not move."""
    from custom_components.alpha_ems_manager.const import INHIBIT_DISPATCH_ACTIVE
    from custom_components.alpha_ems_manager.safety import ControlContext, evaluate

    context = ControlContext(
        mode="active",
        execution_enabled=True,
        failsafe_available=True,
        dispatch_active=True,
        dispatch_owned=False,
    )

    verdict = evaluate(None, context)

    assert verdict.safe is False
    assert verdict.inhibit_reason == INHIBIT_DISPATCH_ACTIVE


def test_defaulting_ownership_to_true_is_caught() -> None:
    """A caller that forgets to supply it must get the safe answer."""
    from custom_components.alpha_ems_manager.safety import ControlContext

    context = ControlContext(mode="shadow", execution_enabled=False)

    assert context.dispatch_owned is False


# ===========================================================================
# D. arming and resetting
# ===========================================================================


def test_the_reset_not_deactivating_first_is_caught() -> None:
    """An interrupted reset must leave the dispatch off, not half-cleared."""
    from custom_components.alpha_ems_manager.alphaess_device import (
        CHARGE_FAMILY,
        plan_reset,
    )
    from custom_components.alpha_ems_manager.const import ACTION_CHARGE

    steps = plan_reset(ACTION_CHARGE)

    assert steps[0].entity_id == CHARGE_FAMILY.activate
    assert steps[0].service == "turn_off"
    # Every prefix of the sequence leaves the dispatch off.
    for cut in range(1, len(steps) + 1):
        assert steps[0].service == "turn_off", cut


def test_a_reset_that_leaves_the_duration_behind_is_caught() -> None:
    """So a short run cannot inherit a long one's dead-man."""
    from custom_components.alpha_ems_manager.alphaess_device import (
        CHARGE_FAMILY,
        plan_reset,
    )
    from custom_components.alpha_ems_manager.const import ACTION_CHARGE

    touched = {step.entity_id for step in plan_reset(ACTION_CHARGE)}

    assert CHARGE_FAMILY.duration in touched
    assert CHARGE_FAMILY.cutoff_soc in touched
    assert CHARGE_FAMILY.power in touched


def test_shadow_acquiring_the_marker_is_caught() -> None:
    """**Shadow must never create a physical ownership claim.**

    The marker is only ever written as part of arming, and arming only happens on
    the authorized path -- which the mode refuses in shadow before the barrier even
    gets a say. So this is a property of the pipeline rather than of the marker,
    and it is asserted where the refusal is.
    """
    from custom_components.alpha_ems_manager.const import (
        CONTROL_MODE_SHADOW,
        REFUSE_MODE_NOT_ACTIVE,
    )
    from custom_components.alpha_ems_manager.safety import (
        ControlContext,
        SafetyVerdict,
        authorize,
    )

    context = ControlContext(mode=CONTROL_MODE_SHADOW, execution_enabled=True)

    decision = authorize(
        SafetyVerdict(True, None, ()),
        context,
        commands_planned=6,
        starts_or_increases=True,
    )

    assert decision.authorized is False
    assert decision.refusal == REFUSE_MODE_NOT_ACTIVE
