"""Stage B structural boundaries: the guarantees that must outlive this release.

The prose in ``execution.py`` says the controller cannot do economics. Prose decays.
These tests are what actually holds, and they are written to fail if someone later
reaches for a price from the controller in good faith -- which is how the second
economic optimizer would arrive if it ever did.

Modelled on ``test_realized_economics.py``'s isolation tests, which keep realised
*money* out of every decision path. Stage B needs the same shape with one deliberate
difference, spelled out below: it may read realised **physics**.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from custom_components.alpha_ems_manager import execution as execution_module
from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    PERMITTED_SERVICES,
    plan_commands,
    plan_release_marker,
    plan_reset,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
)

from .live_capability import assert_charge_only_capability

SOURCE = pathlib.Path(execution_module.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def code_words() -> set[str]:
    """Return every word appearing in *executable* code, lowercased.

    Docstrings and comments excluded, deliberately. This module's documentation
    names the things it must never do -- "decide what counts as a valuable export
    opportunity" is a prohibition, not a use -- and a check that could not tell the
    two apart would forbid explaining the rule it exists to enforce.

    Identifiers, attribute names, and string literals that are not docstrings.
    """
    docstrings = {
        node.body[0].value
        for node in ast.walk(TREE)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    words: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Name):
            words.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            words.add(node.attr.lower())
        elif isinstance(node, ast.arg):
            words.add(node.arg.lower())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            words.add(node.name.lower())
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
        ):
            words.update(node.value.lower().split())
    return words


CODE_WORDS = code_words()


# ===========================================================================
# A. the controller cannot do economics
# ===========================================================================


def test_the_controller_imports_no_price_or_economic_module() -> None:
    """**The load-bearing guarantee of the whole Stage A/B split.**

    Stage B is handed every economic quantity as data. It has no route to a price
    series, a forecast of one, or the optimizer that reasons about them -- so the
    question "what is this energy worth?" is not one it can even ask.
    """
    forbidden = {
        "price_forecast",
        "economic",
        "realized",
        "policy",
        "pv_forecast",
        "reserve",
    }
    imported: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)

    assert not (imported & forbidden), sorted(imported & forbidden)
    # The constants, the interval definition, the device-neutral intent shape, and
    # the standard library. ``battery`` and ``control`` were added in beta.20 when
    # Stage B became the command source: it has to express an interval and a
    # ``ControlIntent``, and both of those modules are themselves pure and contain
    # no economics. The forbidden set above is the load-bearing half of this
    # assertion, and it has not moved.
    assert imported <= {
        "const",
        "battery",
        "control",
        "dataclasses",
        "datetime",
        # Digests only, for the run identity. The same shape the publication id
        # already uses -- a stable name for one accepted run, not a source of
        # judgement about it.
        "hashlib",
        "typing",
        "__future__",
    }, sorted(imported)


def test_the_controller_names_no_price_or_value_concept() -> None:
    """Not in an identifier, not in a string, not in a comment.

    Strings and comments included on purpose. A price reached by any route is a
    price, and a helpfully named local variable is the first step of building one.
    """
    forbidden = {
        "eur",
        "price",
        "prices",
        "tariff",
        "profit",
        "revenue",
        "cheap",
        "cheapest",
        "expensive",
        "arbitrage",
        "valuable",
        "worth",
        # Not bare "value": it is the generic name for "a number" in this module's
        # parsing helpers, and forbidding it would forbid ordinary Python. The
        # economic forms are what matter.
        "value_eur",
        "expected_value",
        "net_value",
    }
    present = sorted(
        word
        for word in CODE_WORDS
        if word in forbidden or any(word.startswith(f"{f}_") for f in forbidden)
    )

    assert present == [], present


def test_the_controller_has_no_branch_that_raises_a_target() -> None:
    """It may raise the *rate*. It may never raise the *amount*.

    Asserted on behaviour rather than on text: a target handed in must come out
    unchanged in every field, whatever the controller concludes about it.
    """
    from datetime import UTC, datetime, timedelta

    from custom_components.alpha_ems_manager.execution import (
        demand_for,
        measure_progress,
        parse_target,
    )

    opens = datetime(2026, 8, 24, 10, 45, tzinfo=UTC)
    raw = {
        "plan_id": "p",
        "revision": 1,
        "intent": "grid_charge",
        "purpose": "charge",
        "window_start": opens.isoformat(),
        "window_end": (opens + timedelta(hours=4)).isoformat(),
        "issued_at": opens.isoformat(),
        "stale_after": (opens + timedelta(hours=9)).isoformat(),
        "battery_target_kwh": 10.0,
        "average_power_kw": 2.5,
        "first_power_kw": 2.5,
        "reserve_floor_kwh": 4.0,
        "max_end_energy_kwh": 18.0,
        "expected_pv_to_battery_kwh": 5.0,
    }
    target = parse_target(raw)
    assert target is not None

    # Wildly behind schedule, which is the case that raises power.
    demand = demand_for(
        target,
        now=opens + timedelta(hours=3, minutes=45),
        progress=measure_progress(accumulated_kwh=0.0, soc_delta_kwh=None),
        current_energy_kwh=5.0,
        remaining_expected_pv_kwh=0.0,
    )

    assert demand.rolling_kw > target.average_power_kw  # the rate rose
    assert target.battery_target_kwh == 10.0  # the amount did not
    assert demand.remaining_kwh <= target.battery_target_kwh


def test_the_headroom_cap_can_only_lower_the_request() -> None:
    """Swept, because a one-directional rule is worth proving rather than reading."""
    from datetime import UTC, datetime, timedelta

    from custom_components.alpha_ems_manager.execution import (
        demand_for,
        measure_progress,
        parse_target,
    )

    opens = datetime(2026, 8, 24, 10, 45, tzinfo=UTC)
    for stored in (2.0, 6.0, 10.0, 14.0, 17.9, 18.0, 25.0):
        for pv_left in (0.0, 1.0, 5.0, 12.0):
            raw = {
                "plan_id": "p",
                "revision": 1,
                "intent": "grid_charge",
                "purpose": "charge",
                "window_start": opens.isoformat(),
                "window_end": (opens + timedelta(hours=4)).isoformat(),
                "issued_at": opens.isoformat(),
                "stale_after": (opens + timedelta(hours=9)).isoformat(),
                "battery_target_kwh": 10.0,
                "average_power_kw": 2.5,
                "first_power_kw": 2.5,
                "reserve_floor_kwh": 4.0,
                "max_end_energy_kwh": 18.0,
            }
            target = parse_target(raw)
            assert target is not None
            demand = demand_for(
                target,
                now=opens + timedelta(hours=1),
                progress=measure_progress(accumulated_kwh=1.0, soc_delta_kwh=None),
                current_energy_kwh=stored,
                remaining_expected_pv_kwh=pv_left,
            )
            assert demand.required_kw <= demand.rolling_kw + 1e-9, (stored, pv_left)
            assert demand.required_kw >= 0.0, (stored, pv_left)


def test_the_controller_reads_realised_physics_but_not_realised_money() -> None:
    """The one deliberate difference from the realised-economics isolation.

    Stage B has to know how much energy actually arrived, or it cannot track a
    target at all -- so realised *physics* is allowed and is the whole point of
    ``measure_progress``. Realised *money* stays forbidden: what the energy cost is
    not a fact a controller needs, and a controller that knew it would eventually be
    asked to act on it.
    """
    names = {
        node.name
        for node in ast.walk(TREE)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }

    # Physics: present, and the whole reason the module exists.
    assert "measure_progress" in names
    assert "Progress" in names
    # Money: no realised-money identifier, and no route to one. The import test
    # above already proves ``realized`` is unreachable; this pins the naming, so a
    # field called ``realized_cost_eur`` could not appear without failing.
    money = {"realized_cost_eur", "realized_revenue_eur", "cost_basis", "trade_profit"}
    assert not (CODE_WORDS & money), sorted(CODE_WORDS & money)


# ===========================================================================
# B. one command path, one send site
# ===========================================================================


def test_the_controller_names_no_service_and_no_entity() -> None:
    """It decides a power. Turning that into writes is the device layer's job."""
    assert "async_call" not in SOURCE
    assert "input_number" not in SOURCE
    assert "input_boolean" not in SOURCE
    assert "hass" not in SOURCE


def test_the_controller_imports_no_home_assistant() -> None:
    """Pure, so the whole policy is exercisable against plain values."""
    assert "homeassistant" not in SOURCE


def test_the_mode_is_read_in_exactly_one_place_in_the_pipeline() -> None:
    """**How Shadow/Live parity is guaranteed: structurally, not by testing twice.**

    Everything upstream of authorization is mode-blind, so the command a Shadow
    refresh computes is the command a Live refresh computes. If a second module
    started reading the mode, the two could diverge and no behavioural test would
    necessarily catch it.
    """
    from custom_components.alpha_ems_manager import safety

    # The gate compares the mode. That comparison is what decides execution, and
    # there must be exactly one of it.
    source = inspect.getsource(safety)
    assert source.count("context.mode !=") == 1

    # ``evaluate`` -- everything upstream of the gate -- must not read the mode at
    # all, which is what makes the command mode-blind and therefore identical in
    # shadow and live.
    tree = ast.parse(source)
    evaluate = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate"
    )
    mode_reads = [
        inner
        for inner in ast.walk(evaluate)
        if isinstance(inner, ast.Attribute) and inner.attr == "mode"
    ]
    assert mode_reads == []

    # And the controller does not know the mode's name either.
    assert "CONTROL_MODE" not in SOURCE


def test_the_controller_never_decides_whether_to_execute() -> None:
    """It computes a request. Authorization is somebody else's decision."""
    assert "CONTROL_EXECUTION_AVAILABLE" not in SOURCE
    assert "authorize" not in SOURCE
    assert "execution_enabled" not in SOURCE


def test_only_a_grid_charge_is_executable_in_this_release() -> None:
    """beta.19 built the controller; beta.24 lets one direction out of it.

    Stage B is the only path that can execute, and only for a charge. Every other
    direction it can describe stays outside the executable set.
    """
    assert_charge_only_capability()


def test_the_permitted_service_set_did_not_grow() -> None:
    """The owner marker costs nothing: ``turn_on``/``turn_off`` were already in."""
    assert len(PERMITTED_SERVICES) == 4
    assert ("input_boolean", "turn_on") in PERMITTED_SERVICES
    assert ("input_boolean", "turn_off") in PERMITTED_SERVICES


# ===========================================================================
# C. arming, resetting, and the marker
# ===========================================================================


def test_the_marker_is_the_first_step_of_arming() -> None:
    """So an interrupted sequence leaves a stale marker, not an unowned dispatch.

    The direction of the failure is the point. A marker on with nothing running is
    recognisable and clearable. A dispatch running with no marker reads as
    somebody else's and could never be stopped.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        DeviceCommand,
    )

    command = DeviceCommand(
        action=ACTION_CHARGE,
        power_kw=2.0,
        cutoff_soc_percent=95,
        duration_minutes=20,
        device_hold_flag=False,
        energy_limit_bound=False,
        allowed_energy_ac_kwh=0.5,
        commanded_energy_ac_kwh=0.5,
    )
    steps = plan_commands(command)

    assert steps[0].entity_id == BOOLEAN_EXECUTION_OWNER
    assert steps[0].service == "turn_on"
    # And activation is still last, which was already the contract.
    assert steps[-1].entity_id == CHARGE_FAMILY.activate
    assert steps[-1].service == "turn_on"


def test_the_reset_deactivates_first_and_releases_the_marker_last() -> None:
    """The mirror of arming, and for the mirrored reason.

    Deactivating first means an interrupted reset leaves the dispatch *off* with
    some parameters still populated -- inert, and cleaned up next time. The other
    order would clear the parameters of a dispatch that was still running.

    The marker goes last because until it is off the dispatch is still owned, and
    releasing ownership mid-reset would leave Alpha EMS unable to finish.
    """
    steps = plan_reset(ACTION_DISCHARGE)

    assert steps[0].entity_id == DISCHARGE_FAMILY.activate
    assert steps[0].service == "turn_off"
    assert steps[-1].entity_id == BOOLEAN_EXECUTION_OWNER
    assert steps[-1].service == "turn_off"


def test_the_reset_leaves_nothing_a_later_run_could_inherit() -> None:
    """**Setting power to zero is not a stop.**

    A dispatch left armed at zero still holds a duration, a cutoff and a timer, and
    the next run would inherit them -- so a short run following a long one would
    silently acquire the long one's dead-man.
    """
    steps = plan_reset(ACTION_CHARGE)
    touched = {step.entity_id for step in steps}

    assert CHARGE_FAMILY.power in touched
    assert CHARGE_FAMILY.duration in touched
    assert CHARGE_FAMILY.cutoff_soc in touched
    assert CHARGE_FAMILY.hold in touched
    assert CHARGE_FAMILY.activate in touched
    # Power really is returned to zero rather than merely rewritten.
    power_steps = [s for s in steps if s.entity_id == CHARGE_FAMILY.power]
    assert [s.value for s in power_steps] == [0.0]


def test_every_reset_step_is_a_permitted_service() -> None:
    """No fourth service was added to express a stop."""
    for action in (ACTION_CHARGE, ACTION_DISCHARGE):
        for step in plan_reset(action):
            assert (step.domain, step.service) in PERMITTED_SERVICES
    for step in plan_release_marker():
        assert (step.domain, step.service) in PERMITTED_SERVICES


def test_the_reset_touches_only_one_family() -> None:
    """A charge reset never disturbs the discharge helpers, or the reverse."""
    charge = {s.entity_id for s in plan_reset(ACTION_CHARGE)}
    discharge = {s.entity_id for s in plan_reset(ACTION_DISCHARGE)}

    assert not (charge & set(DISCHARGE_FAMILY.entities))
    assert not (discharge & set(CHARGE_FAMILY.entities))
    # Both release the marker, which belongs to neither family.
    assert BOOLEAN_EXECUTION_OWNER in charge & discharge


def test_releasing_a_stale_marker_touches_nothing_else() -> None:
    """Clearing a marker with nothing behind it is not an ownership claim."""
    steps = plan_release_marker()

    assert len(steps) == 1
    assert steps[0].entity_id == BOOLEAN_EXECUTION_OWNER
    assert steps[0].service == "turn_off"


def test_the_marker_is_not_an_alphaess_helper() -> None:
    """It records what the vendor surface cannot, so it lives outside it."""
    assert "alphaess" not in BOOLEAN_EXECUTION_OWNER
    assert BOOLEAN_EXECUTION_OWNER.startswith("input_boolean.")
    families = set(CHARGE_FAMILY.entities) | set(DISCHARGE_FAMILY.entities)
    assert BOOLEAN_EXECUTION_OWNER not in families


def test_ownership_is_still_not_derived_from_parameters() -> None:
    """The inference the control surface makes untrustworthy stays forbidden.

    beta.19 did not relax this -- it routed around it. Ownership rests on a marker
    and a persisted record, neither of which is a helper value the vendor package
    also writes.
    """
    from custom_components.alpha_ems_manager.alphaess_device import OWNERSHIP_PROVABLE

    assert OWNERSHIP_PROVABLE is False
    for word in ("power_kw ==", "cutoff ==", "duration =="):
        assert word not in SOURCE


def test_the_ownership_verdict_is_supplied_to_the_gate_not_derived_by_it() -> None:
    """``safety`` must not learn how to decide ownership.

    **Named against the evidence rather than against the vocabulary**, and beta.24
    forced that distinction. The check used to forbid any word containing "marker"
    or "record" anywhere in the module, which was a fine proxy while nothing here
    mentioned either. The stop path has an operation whose subject genuinely *is*
    the owner marker -- releasing one that has nothing behind it -- and a test that
    banned the word would have been satisfied by calling it something else, which is
    obfuscation rather than safety.

    So the invariant is stated directly: this module may be *told* a verdict, and it
    may not reach the evidence a verdict is made from. The denied names are the
    evidence's own fields and the two functions that decide, so no rename can
    satisfy this list -- only actually reading the evidence would break it.
    """
    from custom_components.alpha_ems_manager import safety

    source = inspect.getsource(safety)
    words = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            words.add(node.id)
        elif isinstance(node, ast.Attribute):
            words.add(node.attr)
        elif isinstance(node, ast.arg):
            words.add(node.arg)

    # The verdicts are read: an ownership state, and whether a marker is stale.
    assert "dispatch_owned" in words

    # The evidence is not. These are ``OwnershipEvidence``'s own fields and
    # properties, plus the two functions that turn them into a verdict -- so this
    # module cannot begin deciding ownership for itself.
    #
    # ``dispatch_active`` is deliberately absent from the list even though the
    # evidence carries one: ``ControlContext`` has carried its own since Phase 4,
    # and the gate has always read it to refuse a command over a running dispatch.
    # Denying the *name* would fail an invariant that predates this file.
    for denied in (
        "OwnershipEvidence",
        "ownership_of",
        "stale_marker",
        "marker_on",
        "dispatch_start",
        "record",
        "record_present",
        "record_matches",
        "record_provenance",
        "record_names_this_run",
    ):
        assert denied not in words, denied


def test_a_foreign_dispatch_still_inhibits() -> None:
    """The relaxation is additive. Unsupplied ownership means beta.18 behaviour."""
    from custom_components.alpha_ems_manager.safety import ControlContext

    context = ControlContext(mode="shadow", execution_enabled=False)

    assert context.dispatch_owned is False


# ===========================================================================
# D. two defects found during implementation, pinned so they cannot return
# ===========================================================================


def test_the_targets_are_built_before_the_control_report() -> None:
    """**Otherwise Stage B acts on last quarter's plan.**

    Found by walking twelve refreshes and noticing the controller was working to a
    window that had not started yet. The controller reads
    ``self.execution_targets``, so building them after the control report leaves it
    a refresh behind -- with a ``plan_id`` and ``revision`` describing a plan it is
    not executing. Nothing failed loudly; the numbers were simply stale.
    """
    from custom_components.alpha_ems_manager import coordinator as coordinator_module

    source = pathlib.Path(coordinator_module.__file__).read_text(encoding="utf-8")
    targets_at = source.index("self.execution_targets = self._execution_targets(")
    report_at = source.index("control = self._build_control_report_safely(")

    assert targets_at < report_at


def test_a_run_starting_at_the_next_boundary_is_actionable_now() -> None:
    """**Otherwise the controller never selects anything at all.**

    The economic horizon begins at the *next* interval boundary, so at any refresh
    the earliest run opens fifteen minutes later. Strict containment -- the obvious
    test -- selected nothing, ever, and the controller sat idle beside a perfectly
    good target through a whole simulated day.

    A dispatch armed now runs through the coming interval, so imminent is the
    correct reading.
    """
    from datetime import UTC, datetime, timedelta

    from custom_components.alpha_ems_manager.execution import (
        ACTIONABLE_LEAD_MINUTES,
        actionable_target,
        parse_target,
    )

    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    opens = now + timedelta(minutes=15)
    raw = {
        "plan_id": "p",
        "revision": 1,
        "intent": "grid_charge",
        "purpose": "charge",
        "window_start": opens.isoformat(),
        "window_end": (opens + timedelta(hours=1)).isoformat(),
        "issued_at": now.isoformat(),
        "stale_after": (now + timedelta(minutes=30)).isoformat(),
        "battery_target_kwh": 5.0,
        "average_power_kw": 5.0,
        "first_power_kw": 5.0,
        "reserve_floor_kwh": 4.0,
    }
    target = parse_target(raw)
    assert target is not None

    # Strict containment says no, and that reading is what caused the defect.
    assert target.covers(now) is False
    # Imminence says yes, which is the physically correct answer.
    assert target.actionable_at(now) is True
    assert actionable_target([raw], now) is not None

    # And it is one planning interval, not an open-ended lookahead: a run hours
    # away is still not actionable.
    assert ACTIONABLE_LEAD_MINUTES == 15.0
    distant = dict(raw)
    distant["window_start"] = (now + timedelta(hours=3)).isoformat()
    distant["window_end"] = (now + timedelta(hours=4)).isoformat()
    assert actionable_target([distant], now) is None


# ===========================================================================
# E. the two gates that must close before Live. Recorded, not solved.
# ===========================================================================


def test_no_command_can_exist_before_the_window_opens() -> None:
    """**A4. The inverse of the gate this test used to record.**

    In beta.19 this asserted the defect: fifteen minutes before a window opened the
    controller reached ``armed`` with a live request, and since arming an AlphaESS
    dispatch starts it immediately, opening the barrier would have delivered energy
    early. Hardware confirmed that on the real installation.

    It now asserts the fix. Selection still looks one interval ahead -- it must, or
    nothing is ever selected, because the economic horizon begins at the next
    boundary -- but activation is strictly inside the window, and the two are
    separate questions asked by separate methods.

    Checked at four offsets, and on the *command* rather than only the state,
    because a state name is a label and a command list is what reaches an inverter.
    """
    from datetime import UTC, datetime, timedelta

    from custom_components.alpha_ems_manager.const import (
        EXECUTION_STATE_ARMED,
        EXECUTION_STATE_PREPARED,
    )
    from custom_components.alpha_ems_manager.execution import (
        OwnershipEvidence,
        control_intent_for,
        decide,
        measure_progress,
        parse_target,
    )

    opens = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
    raw = {
        "plan_id": "p",
        "revision": 1,
        "intent": "grid_charge",
        "purpose": "charge",
        "window_start": opens.isoformat(),
        "window_end": (opens + timedelta(hours=2)).isoformat(),
        "issued_at": (opens - timedelta(minutes=20)).isoformat(),
        "stale_after": (opens + timedelta(hours=9)).isoformat(),
        "battery_target_kwh": 8.0,
        "average_power_kw": 4.0,
        "first_power_kw": 4.0,
        "reserve_floor_kwh": 4.0,
    }
    target = parse_target(raw)
    assert target is not None

    def at(moment):
        decision = decide(
            mode_executes=True,
            mode_off=False,
            targets=[raw],
            now=moment,
            evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
            progress=measure_progress(accumulated_kwh=0.0, soc_delta_kwh=None),
            current_energy_kwh=8.0,
        )
        intent = control_intent_for(
            decision,
            floor_soc_percent=20.0,
            ceiling_soc_percent=100.0,
            horizon_minutes=20,
            target_day=opens.date(),
            start_index=56,
            built_at=moment,
        )
        return decision, intent

    # Fifteen minutes early: selected, computed, and refusing to act.
    early, early_intent = at(opens - timedelta(minutes=15))
    assert early.target is not None, "selection must still look one interval ahead"
    assert early.state == EXECUTION_STATE_PREPARED
    assert early.request_kw == 0.0
    assert early.wants_command is False
    assert early_intent is None, "no command may exist before the window opens"

    # One second early: still nothing.
    barely, barely_intent = at(opens - timedelta(seconds=1))
    assert barely.state == EXECUTION_STATE_PREPARED
    assert barely_intent is None

    # Exactly at the boundary: now it may act.
    on_time, on_time_intent = at(opens)
    assert on_time.state == EXECUTION_STATE_ARMED
    assert on_time.request_kw > 0.0
    assert on_time_intent is not None
    assert on_time_intent.action == "charge"

    # A few seconds late is acceptable and must still act -- a refresh lands just
    # after the boundary, and starting a little late is the correct trade.
    late, late_intent = at(opens + timedelta(seconds=20))
    assert late.state == EXECUTION_STATE_ARMED
    assert late_intent is not None


def test_the_reset_cannot_clear_the_vendor_timer_and_that_is_a_live_gate() -> None:
    """The other gate, recorded the same way.

    The reset returns every field it is permitted to touch to a resting value, but
    the family's ``timer`` entity is not among them: ``timer.cancel`` is not in the
    closed set of three services, and adding it would widen that set.

    So the sequence relies on the vendor package clearing its own timer when
    ``activate`` goes off -- which is true or false of the real installation and
    cannot be settled from here.
    """
    steps = plan_reset(ACTION_CHARGE)
    touched = {step.entity_id for step in steps}

    # The timer is not touched, and cannot be.
    assert CHARGE_FAMILY.timer not in touched
    assert not any(step.domain == "timer" for step in steps)
    assert "timer" not in {service for _, service in PERMITTED_SERVICES}
    # Everything the reset *can* reach, it does.
    assert CHARGE_FAMILY.activate in touched
    assert CHARGE_FAMILY.power in touched
    assert CHARGE_FAMILY.duration in touched
    assert CHARGE_FAMILY.cutoff_soc in touched
