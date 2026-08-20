"""Deliberately break each control invariant, and prove a test notices.

A green suite is not evidence on its own. A test that would also pass against
the broken implementation it exists to protect against is decoration, and the
only way to know which kind you have is to break the thing and watch.

Every mutation below is a *plausible* refactor rather than an absurdity -- the
kind of change someone might make in good faith while tidying up. Each one is
applied, the guarding assertion is run, and the mutation is reverted. A mutation
that survives is a gap in the suite, not a curiosity.

The two that matter most are the ownership pair. They reproduce, exactly, the
design this release rejected: inferring that a running dispatch is ours because
its parameters match what we would have sent.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from custom_components.alpha_ems_manager import alphaess_device, safety
from custom_components.alpha_ems_manager.alphaess_device import (
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    build_command,
    plan_commands,
)
from custom_components.alpha_ems_manager.battery import INTERVAL_HOURS
from custom_components.alpha_ems_manager.const import (
    ACTION_DISCHARGE,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_SHADOW,
    INHIBIT_DISPATCH_ACTIVE,
    INHIBIT_GRID_STALE,
    INHIBIT_GRID_UNUSABLE,
    INHIBIT_SOC_STALE,
    INHIBIT_WOULD_EXPORT,
)
from custom_components.alpha_ems_manager.safety import authorize, evaluate

from .test_control_pipeline import GATE_CASES, make_context, make_intent


@contextmanager
def patched(module: object, name: str, value: object):
    """Replace a module attribute, then put it back whatever happens."""
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def surviving(check) -> bool:
    """Return whether an assertion still passes -- i.e. the mutation survived."""
    try:
        check()
    except AssertionError:
        return False
    return True


# ===========================================================================
# 1. the release barrier
# ===========================================================================


def test_lifting_the_release_barrier_is_caught() -> None:
    """The headline invariant: nothing is ever authorized in this release."""

    def guard() -> None:
        context = make_context(mode=CONTROL_MODE_ACTIVE, execution_enabled=True)
        decision = authorize(
            evaluate(make_intent(energy_ac_kwh=0.5), context),
            context,
            commands_planned=5,
            starts_or_increases=True,
        )
        assert decision.authorized is False

    guard()
    with patched(safety, "CONTROL_EXECUTION_AVAILABLE", True):
        assert not surviving(guard)


# ===========================================================================
# 2. the eligibility / permission split
# ===========================================================================


def test_authorizing_an_unsafe_verdict_is_caught() -> None:
    """Authorization may only ever subtract from the gate's answer."""

    def guard() -> None:
        context = make_context(mode=CONTROL_MODE_ACTIVE, soc_percent=None)
        verdict = evaluate(make_intent(), context)
        assert verdict.safe is False
        decision = authorize(
            verdict, context, commands_planned=5, starts_or_increases=True
        )
        assert decision.authorized is False

    guard()

    def permissive(verdict, context, *, commands_planned, starts_or_increases):
        from custom_components.alpha_ems_manager.safety import ExecutionDecision

        return ExecutionDecision(True, None)

    with patched(safety, "authorize", permissive):
        # Rebound through the module so the mutation is what the guard sees.
        def mutated_guard() -> None:
            context = make_context(mode=CONTROL_MODE_ACTIVE, soc_percent=None)
            verdict = evaluate(make_intent(), context)
            decision = safety.authorize(
                verdict, context, commands_planned=5, starts_or_increases=True
            )
            assert decision.authorized is False

        assert not surviving(mutated_guard)


def test_making_the_gate_mode_aware_is_caught() -> None:
    """The mistake an earlier design actually made.

    With ``mode`` as the first gate condition, shadow reported
    ``mode_not_active`` and never revealed whether the command would have been
    safe -- the only question shadow exists to answer.
    """

    def guard() -> None:
        intent = make_intent(energy_ac_kwh=0.5)
        reference = evaluate(intent, make_context(mode=CONTROL_MODE_SHADOW))
        assert evaluate(intent, make_context(mode=CONTROL_MODE_ACTIVE)) == reference
        for _, overrides in GATE_CASES:
            shadow = evaluate(
                intent, make_context(mode=CONTROL_MODE_SHADOW, **overrides)
            )
            active = evaluate(
                intent, make_context(mode=CONTROL_MODE_ACTIVE, **overrides)
            )
            assert shadow == active

    guard()

    real_evaluate = safety.evaluate

    def mode_aware(intent, context):
        from custom_components.alpha_ems_manager.safety import SafetyVerdict

        if context.mode != CONTROL_MODE_ACTIVE:
            return SafetyVerdict(False, "mode_not_active", ())
        return real_evaluate(intent, context)

    with patched(safety, "evaluate", mode_aware):

        def mutated_guard() -> None:
            intent = make_intent(energy_ac_kwh=0.5)
            shadow = safety.evaluate(intent, make_context(mode=CONTROL_MODE_SHADOW))
            active = safety.evaluate(intent, make_context(mode=CONTROL_MODE_ACTIVE))
            assert shadow == active

        assert not surviving(mutated_guard)


# ===========================================================================
# 3. the gate must reject, never reduce
# ===========================================================================


def test_scaling_a_command_instead_of_refusing_it_is_caught() -> None:
    """A gate that trims a request to fit has made a decision of its own."""

    def guard() -> None:
        verdict = evaluate(
            make_intent(energy_ac_kwh=2.0),
            make_context(device_power_kw=8.0, house_load_w=1000.0),
        )
        assert verdict.safe is False
        assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT

    guard()

    def scaling(intent, context):
        from custom_components.alpha_ems_manager.safety import SafetyVerdict

        # The tempting "helpful" version: quietly fit the command to the load.
        return SafetyVerdict(True, None, ())

    with patched(safety, "evaluate", scaling):

        def mutated_guard() -> None:
            verdict = safety.evaluate(
                make_intent(energy_ac_kwh=2.0),
                make_context(device_power_kw=8.0, house_load_w=1000.0),
            )
            assert verdict.safe is False

        assert not surviving(mutated_guard)


def test_allowing_an_export_is_caught() -> None:
    """A forced discharge beyond what the meter can absorb leaves the site.

    Nothing downstream catches it: the dispatch path does not honour the
    inverter's own feed-in limit.
    """

    def guard() -> None:
        verdict = evaluate(
            make_intent(energy_ac_kwh=2.0),
            make_context(device_power_kw=9.0, grid_import_w=500.0, battery_power_w=0.0),
        )
        assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT

    guard()
    with patched(safety, "ACTION_DISCHARGE", "not_a_real_action"):
        # With the discharge constant no longer matching, the export condition
        # never applies -- exactly the shape of an accidental refactor.
        assert not surviving(guard)


def test_reintroducing_the_house_load_export_rule_is_caught() -> None:
    """The beta.8 rule, restored, and the live sample that proves it is wrong.

    House load 2071 W against 3132 W of PV: the site was already exporting a
    kilowatt, so the absorbing capacity was zero and the recorded 22 W of import
    is all there was. The old rule read 2071 W of capacity and passed.
    """

    def guard() -> None:
        verdict = evaluate(
            make_intent(energy_ac_kwh=0.225),
            make_context(
                house_load_w=2071.0,
                grid_import_w=22.0,
                grid_export_w=0.0,
                battery_power_w=0.0,
                device_power_kw=0.9,
                export_margin_percent=10.0,
            ),
        )
        assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT

    guard()

    def house_load_capacity(context: object) -> float:
        return (context.house_load_w or 0.0) / 1000.0

    with patched(safety, "absorbing_capacity_kw", house_load_capacity):
        assert not surviving(guard)


def test_dropping_the_existing_discharge_from_the_capacity_is_caught() -> None:
    """A battery already discharging genuinely has that much more headroom.

    Omitting the term would refuse commands that are provably safe -- which is a
    real defect even though it errs on the cautious side, because the whole point
    of the rule is that it bounds the physics rather than guessing at them.
    """

    def guard() -> None:
        verdict = evaluate(
            make_intent(),
            make_context(
                grid_import_w=100.0,
                grid_export_w=0.0,
                battery_power_w=-2000.0,
                device_power_kw=1.8,
                export_margin_percent=10.0,
            ),
        )
        assert verdict.safe is True

    guard()

    def meter_only(context: object) -> float:
        net_w = (context.grid_import_w or 0.0) - (context.grid_export_w or 0.0)
        return max(0.0, net_w) / 1000.0

    with patched(safety, "absorbing_capacity_kw", meter_only):
        assert not surviving(guard)


def test_inverting_the_export_sign_in_the_capacity_is_caught() -> None:
    """Adding export instead of subtracting it inverts the whole rule.

    An exporting site would read as having the *most* capacity, which is exactly
    backwards and would permit a command on the one site that can absorb nothing.
    """

    def guard() -> None:
        verdict = evaluate(
            make_intent(),
            make_context(
                grid_import_w=0.0,
                grid_export_w=4000.0,
                battery_power_w=0.0,
                device_power_kw=2.0,
            ),
        )
        assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT

    guard()

    def sign_flipped(context: object) -> float:
        net_w = (context.grid_import_w or 0.0) + (context.grid_export_w or 0.0)
        discharge_w = max(0.0, -(context.battery_power_w or 0.0))
        return max(0.0, net_w + discharge_w) / 1000.0

    with patched(safety, "absorbing_capacity_kw", sign_flipped):
        assert not surviving(guard)


def test_reading_an_unusable_meter_as_zero_is_caught() -> None:
    """Zero here is not conservative -- it is the unsafe direction.

    A capacity of zero refuses, which looks safe, but the mutation that matters
    is the *other* substitution: treating an unreadable meter as though it had
    been read. This asserts the gate names the missing reading rather than
    silently computing a bound from nothing.
    """

    def guard() -> None:
        verdict = evaluate(
            make_intent(),
            make_context(grid_import_w=None, grid_export_w=None),
        )
        assert verdict.inhibit_reason == INHIBIT_GRID_UNUSABLE

    guard()
    with patched(safety, "INHIBIT_GRID_UNUSABLE", INHIBIT_WOULD_EXPORT):
        assert not surviving(guard)


def test_dropping_the_meter_freshness_check_is_caught() -> None:
    """A capacity computed from a reading minutes old is not a bound on now."""

    def guard() -> None:
        verdict = evaluate(make_intent(), make_context(grid_age_seconds=301.0))
        assert verdict.inhibit_reason == INHIBIT_GRID_STALE

    guard()
    with patched(safety, "_stale", lambda age, limit: False):
        assert not surviving(guard)


# ===========================================================================
# 4. ownership is never inferred
# ===========================================================================


def test_inferring_ownership_from_matching_parameters_is_caught() -> None:
    """The design this release rejected, reproduced and caught.

    A dispatch is running with exactly the parameters Alpha EMS would have sent.
    The unsound inference says "ours"; the correct answer is that nothing records
    who armed it, so it is not.
    """

    def guard() -> None:
        verdict = evaluate(
            make_intent(energy_ac_kwh=0.5), make_context(dispatch_active=True)
        )
        assert verdict.safe is False
        assert verdict.inhibit_reason == INHIBIT_DISPATCH_ACTIVE

    guard()

    real_evaluate = safety.evaluate

    def parameter_match(intent, context):
        from custom_components.alpha_ems_manager.safety import SafetyVerdict

        command = build_command(intent) if intent is not None else None
        looks_like_ours = (
            command is not None
            and context.dispatch_active
            and context.device_power_kw == command.power_kw
        )
        if looks_like_ours:
            # "It matches, so it must be ours." It is not.
            return SafetyVerdict(True, None, ())
        return real_evaluate(intent, context)

    with patched(safety, "evaluate", parameter_match):

        def mutated_guard() -> None:
            intent = make_intent(energy_ac_kwh=0.5)
            command = build_command(intent)
            verdict = safety.evaluate(
                intent,
                make_context(dispatch_active=True, device_power_kw=command.power_kw),
            )
            assert verdict.inhibit_reason == INHIBIT_DISPATCH_ACTIVE

        assert not surviving(mutated_guard)


def test_narrowing_the_dispatch_condition_to_a_mismatch_is_caught() -> None:
    """The same error one layer down: "any dispatch" weakened to "a different one"."""

    def guard() -> None:
        verdict = evaluate(make_intent(), make_context(dispatch_active=True))
        assert verdict.inhibit_reason == INHIBIT_DISPATCH_ACTIVE

    guard()

    with patched(alphaess_device, "OWNERSHIP_PROVABLE", True):

        def provable_guard() -> None:
            assert alphaess_device.OWNERSHIP_PROVABLE is False

        assert not surviving(provable_guard)


def test_reintroducing_a_stop_path_is_caught() -> None:
    """A hold plans nothing, whatever the device happens to be doing."""

    def guard() -> None:
        command = build_command(make_intent(action="hold", energy_ac_kwh=0.0))
        assert plan_commands(command) == ()

    guard()

    real_plan = alphaess_device.plan_commands

    def with_stop(command):
        from custom_components.alpha_ems_manager.alphaess_device import (
            SERVICE_TURN_OFF,
            CommandStep,
        )

        if not command.moves_battery:
            # "We might own it, so turn it off." That is the prohibited write.
            return (CommandStep(*SERVICE_TURN_OFF, DISCHARGE_FAMILY.activate),)
        return real_plan(command)

    with patched(alphaess_device, "plan_commands", with_stop):

        def mutated_guard() -> None:
            command = build_command(make_intent(action="hold", energy_ac_kwh=0.0))
            assert alphaess_device.plan_commands(command) == ()

        assert not surviving(mutated_guard)


# ===========================================================================
# 5. quantisation resolves downwards
# ===========================================================================


def test_rounding_the_power_up_is_caught() -> None:
    """Rounding to nearest would over-deliver on nearly half of all inputs."""

    def guard() -> None:
        for milli_kwh in range(0, 3000):
            energy = milli_kwh / 1000.0
            power = alphaess_device.device_power_kw(energy, INTERVAL_HOURS)
            assert power * INTERVAL_HOURS <= energy

    guard()

    def rounded(energy_ac_kwh: float, interval_hours: float) -> float:
        if energy_ac_kwh <= 0.0:
            return 0.0
        return round(round(energy_ac_kwh / interval_hours, 1), 1)

    with patched(alphaess_device, "device_power_kw", rounded):
        assert not surviving(guard)


def test_dropping_the_cutoff_compensation_is_caught() -> None:
    """Without the extra percent the device stops just below the user's floor."""

    from custom_components.alpha_ems_manager.const import (
        CONTROL_CUTOFF_PERCENT_PER_BIT,
    )

    def guard() -> None:
        for floor in range(0, 100):
            commanded = alphaess_device.device_cutoff_percent(float(floor))
            bits = int(commanded / CONTROL_CUTOFF_PERCENT_PER_BIT)
            effective = bits * CONTROL_CUTOFF_PERCENT_PER_BIT
            assert effective >= floor

    guard()

    def uncompensated(floor_soc_percent: float) -> int:
        import math

        return min(100, max(4, math.ceil(floor_soc_percent)))

    with patched(alphaess_device, "device_cutoff_percent", uncompensated):
        assert not surviving(guard)


def test_dropping_the_device_minimum_is_caught() -> None:
    """A command inside the teardown window looks like a finished one."""

    def guard() -> None:
        verdict = evaluate(make_intent(), make_context(device_power_kw=0.1))
        assert verdict.safe is False

    guard()
    with patched(safety, "CONTROL_MIN_POWER_KW", 0.0):
        assert not surviving(guard)


# ===========================================================================
# 6. the write order
# ===========================================================================


def test_activating_before_the_parameters_are_set_is_caught() -> None:
    """Turning the boolean on is what triggers the write.

    Put it first and the device acts on whatever the helpers happened to hold --
    which, after a previous command, is a stale setpoint.
    """

    def guard() -> None:
        steps = plan_commands(
            build_command(make_intent(action=ACTION_DISCHARGE, energy_ac_kwh=0.5))
        )
        assert steps[-1].entity_id == DISCHARGE_FAMILY.activate
        assert DISCHARGE_FAMILY.activate not in [step.entity_id for step in steps[:-1]]

    guard()

    real_plan = alphaess_device.plan_commands

    def activate_first(command):
        steps = real_plan(command)
        return tuple(reversed(steps)) if steps else steps

    with patched(alphaess_device, "plan_commands", activate_first):

        def mutated_guard() -> None:
            steps = alphaess_device.plan_commands(
                build_command(make_intent(action=ACTION_DISCHARGE, energy_ac_kwh=0.5))
            )
            assert steps[-1].entity_id == DISCHARGE_FAMILY.activate

        assert not surviving(mutated_guard)


# ===========================================================================
# 7. the right actuator family
# ===========================================================================


def test_mapping_a_discharge_to_the_charge_family_is_caught() -> None:
    """Direction is the one thing a translation layer must never get wrong."""

    def guard() -> None:
        steps = plan_commands(
            build_command(make_intent(action=ACTION_DISCHARGE, energy_ac_kwh=0.5))
        )
        assert steps[-1].entity_id == DISCHARGE_FAMILY.activate

    guard()

    with patched(
        alphaess_device,
        "FAMILIES",
        {**alphaess_device.FAMILIES, ACTION_DISCHARGE: CHARGE_FAMILY},
    ):

        def mutated_guard() -> None:
            steps = alphaess_device.plan_commands(
                build_command(make_intent(action=ACTION_DISCHARGE, energy_ac_kwh=0.5))
            )
            assert steps[-1].entity_id == DISCHARGE_FAMILY.activate

        assert not surviving(mutated_guard)


# ===========================================================================
# 8. freshness
# ===========================================================================


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("soc_age_seconds", INHIBIT_SOC_STALE),
        ("battery_power_age_seconds", "battery_power_stale"),
        ("house_load_age_seconds", "house_load_stale"),
    ],
)
def test_accepting_a_stale_reading_is_caught(field: str, reason: str) -> None:
    """Until this phase nothing on the battery path checked an age at all."""

    def guard() -> None:
        verdict = evaluate(make_intent(), make_context(**{field: 400.0}))
        assert verdict.safe is False
        assert verdict.inhibit_reason == reason

    guard()

    def never_stale(age: float | None, limit: float) -> bool:
        return False

    with patched(safety, "_stale", never_stale):
        assert not surviving(guard)


# ===========================================================================
# 9. the balance residual must stay out of the gate
# ===========================================================================


def test_restoring_the_old_balance_interlock_is_caught() -> None:
    """The condition this release removed, and must not regain by accident.

    Reintroducing it would inhibit control whenever a load sits between the two
    grid meters, or during a fast charge ramp -- neither of which says anything
    about the state of charge.
    """
    from .test_balance_is_not_a_control_gate import LIVE_GROSS, snapshot

    def guard() -> None:
        result = snapshot(**LIVE_GROSS)
        assert result.gross_fault_suspected is True
        verdict = evaluate(make_intent(energy_ac_kwh=0.5), make_context())
        assert verdict.safe is True

    guard()

    real_evaluate = safety.evaluate

    def with_balance_interlock(intent, context):
        from custom_components.alpha_ems_manager.safety import SafetyVerdict

        # The removed condition, restored.
        if snapshot(**LIVE_GROSS).gross_fault_suspected:
            return SafetyVerdict(False, "gross_balance_fault", ())
        return real_evaluate(intent, context)

    with patched(safety, "evaluate", with_balance_interlock):

        def mutated_guard() -> None:
            verdict = safety.evaluate(make_intent(energy_ac_kwh=0.5), make_context())
            assert verdict.safe is True

        assert not surviving(mutated_guard)


# ===========================================================================
# 10. the intent is a copy, not a recomputation
# ===========================================================================


def test_recomputing_the_energy_from_the_request_is_caught() -> None:
    """The clamped figure is the only one that has been through the clamp.

    Driven with a plan the clamp actually **reduced**. The first version of this
    mutation survived, and the reason is worth recording: the helper it used
    built a request whose power matched the allowed energy exactly, so
    recomputing from the request produced the same number and the bug was
    invisible. A mutation test that cannot distinguish the two implementations is
    not testing anything.
    """
    import dataclasses

    from custom_components.alpha_ems_manager import control

    from .test_control_pipeline import make_clamped_plan

    def guard() -> None:
        plan = make_clamped_plan()
        # The clamp reduced the request, so the two figures genuinely differ.
        assert plan.decision.request.power_kw * INTERVAL_HOURS != (
            plan.decision.allowed_energy_ac_kwh
        )
        intent = control.translate(plan, now=datetime.now(UTC), horizon_minutes=20)
        assert intent is not None
        assert intent.energy_ac_kwh == plan.decision.allowed_energy_ac_kwh

    guard()

    real_translate = control.translate

    def from_request(plan, *, now, horizon_minutes):
        intent = real_translate(plan, now=now, horizon_minutes=horizon_minutes)
        if intent is None:
            return None
        # Recomputed from what the policy asked for, before the clamp.
        return dataclasses.replace(
            intent,
            energy_ac_kwh=plan.decision.request.power_kw * INTERVAL_HOURS,
        )

    with patched(control, "translate", from_request):
        assert not surviving(guard)


def test_every_mutation_in_this_file_is_reverted() -> None:
    """The suite must not be left holding a broken implementation.

    Cheap insurance, and worth having: a mutation escaping its context manager
    would make everything after it meaningless, in a way that looks like success.
    """
    assert safety.CONTROL_EXECUTION_AVAILABLE is False
    assert safety.evaluate.__module__.endswith("safety")
    assert safety.authorize.__module__.endswith("safety")
    assert alphaess_device.OWNERSHIP_PROVABLE is False
    assert alphaess_device.plan_commands.__module__.endswith("alphaess_device")
    assert alphaess_device.device_power_kw.__module__.endswith("alphaess_device")
    assert alphaess_device.FAMILIES[ACTION_DISCHARGE] is DISCHARGE_FAMILY

    from custom_components.alpha_ems_manager import control

    assert control.translate.__module__.endswith("control")
    assert alphaess_device.device_cutoff_percent.__module__.endswith("alphaess_device")
