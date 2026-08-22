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

import ast
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
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_SHADOW,
    CONTROL_POWER_STEP_KW,
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


# ===========================================================================
# 11. the beta.15 safe-discharge clamp
# ===========================================================================
#
# Sixteen mutations on one small function, because it sits between the decision
# layer and the inverter and every one of these would either send a command that
# exports or silently lose the refusal that stops one.
#
# The harness is a local reimplementation rather than a monkeypatch wherever the
# mutation is arithmetic: reimplementing shows the broken rule in full, which is
# what makes the test readable a year from now.


CLAMP_REQUESTED_KW = 1.1
CLAMP_CAPACITY_KW = 0.99
CLAMP_MARGIN_PERCENT = 10.0


def clamp_pieces(
    *,
    capacity_kw: float = CLAMP_CAPACITY_KW,
    requested_kw: float = CLAMP_REQUESTED_KW,
    margin_percent: float = CLAMP_MARGIN_PERCENT,
    **context_overrides,
):
    """Return ``(requested, context, safe_kw)`` for one clamp scenario."""
    from .test_control_pipeline import make_context, make_intent

    intent = make_intent(energy_ac_kwh=requested_kw * INTERVAL_HOURS)
    requested = build_command(intent)
    readings = {
        "grid_import_w": capacity_kw * 1000.0,
        "grid_export_w": 0.0,
        "battery_power_w": 0.0,
        "house_load_w": max(50.0, capacity_kw * 1000.0),
        "export_margin_percent": margin_percent,
        **context_overrides,
    }
    context = make_context(device_power_kw=requested.power_kw, **readings)
    return intent, requested, context, safety.safe_discharge_power_kw(context)


def clamped_gate(intent, command, context):
    """Return the verdict the gate reaches on an already-clamped command."""
    import dataclasses

    return safety.evaluate(
        intent, dataclasses.replace(context, device_power_kw=command.power_kw)
    )


def test_removing_the_safety_margin_is_caught() -> None:
    """Mutation: clamp to the raw absorbing capacity.

    The margin exists because house load can change after the meter sample, and
    dropping it is the single most tempting way to make more commands eligible.
    It moves the live case from 0.8 kW to 0.9 kW -- above the 0.891 kW bound the
    margin creates, which is exactly the band the race can eat.
    """
    _, requested, _, safe_kw = clamp_pieces()

    honest = alphaess_device.limit_command(requested, safe_kw)
    broken = alphaess_device.limit_command(requested, CLAMP_CAPACITY_KW)

    assert honest.power_kw == pytest.approx(0.8)
    assert broken.power_kw == pytest.approx(0.9)
    assert broken.power_kw > safe_kw


def test_applying_the_margin_to_the_command_instead_of_the_capacity_is_caught() -> None:
    """Mutation: reduce the *request* by the margin and compare to the capacity.

    A plausible reading of "apply a ten percent margin", and it leaves the
    command sitting at the capacity itself with no margin at all. 1.1 kW less ten
    percent is 0.99 kW -- precisely the measured capacity, and the whole point of
    the margin was not to be there.
    """
    _, requested, _, safe_kw = clamp_pieces()

    honest = alphaess_device.limit_command(requested, safe_kw)
    margined_command = CLAMP_REQUESTED_KW * (1.0 - CLAMP_MARGIN_PERCENT / 100.0)
    broken = alphaess_device.limit_command(requested, margined_command)

    assert margined_command == pytest.approx(CLAMP_CAPACITY_KW)
    assert honest.power_kw == pytest.approx(0.8)
    assert broken.power_kw == pytest.approx(0.9)
    assert broken.power_kw > safe_kw


def test_rounding_the_safe_command_upward_is_caught() -> None:
    """Mutation: ``ceil`` to the helper step instead of ``floor``.

    Rounding up past the bound is the one direction that turns a safety limit
    into a safety hazard: 0.891 kW becomes 0.9 kW, which is above it.
    """
    import math

    _, requested, _, safe_kw = clamp_pieces()

    honest = alphaess_device.limit_command(requested, safe_kw)
    ceiled = math.ceil(safe_kw / CONTROL_POWER_STEP_KW - 1e-9) * CONTROL_POWER_STEP_KW

    assert honest.power_kw <= safe_kw
    assert ceiled > safe_kw
    assert honest.power_kw < ceiled


def test_rounding_the_safe_command_to_nearest_is_caught() -> None:
    """Mutation: ``round`` to the nearest helper step.

    Unsafe half the time, and 0.891 kW is in the unsafe half: it rounds to
    0.9 kW. There is no direction argument that makes nearest-rounding correct in
    a bound whose whole purpose is not to be exceeded.
    """
    _, requested, _, safe_kw = clamp_pieces()

    honest = alphaess_device.limit_command(requested, safe_kw)
    nearest = round(safe_kw / CONTROL_POWER_STEP_KW) * CONTROL_POWER_STEP_KW

    assert honest.power_kw == pytest.approx(0.8)
    assert nearest == pytest.approx(0.9)
    assert nearest > safe_kw


def test_skipping_the_clamp_entirely_is_caught() -> None:
    """Mutation: never call ``limit_command``.

    The pre-beta.15 behaviour, and it must now be visible as a *lost capability*
    rather than as a silent equivalence: the request is refused whole where a
    0.8 kW command was available.
    """
    intent, requested, context, safe_kw = clamp_pieces()

    clamped = alphaess_device.limit_command(requested, safe_kw)
    assert clamped_gate(intent, clamped, context).safe is True

    # Skipping it: the gate sees the unreduced request.
    assert clamped_gate(intent, requested, context).inhibit_reason == (
        INHIBIT_WOULD_EXPORT
    )


def test_clamping_above_the_requested_power_is_caught() -> None:
    """Mutation: ``max`` where ``min`` belongs, so a generous bound raises the command.

    The invariant this breaks is the one the whole safety layer rests on: it may
    only subtract. With 4 kW of absorption a 1.1 kW request must stay 1.1 kW, not
    become 3.6 kW.
    """
    _, requested, _, safe_kw = clamp_pieces(capacity_kw=4.0)

    honest = alphaess_device.limit_command(requested, safe_kw)

    assert safe_kw == pytest.approx(3.6)
    assert honest is requested
    assert honest.power_kw == pytest.approx(CLAMP_REQUESTED_KW)
    assert honest.power_kw < safe_kw


def test_raising_a_sub_minimum_command_to_the_device_minimum_is_caught() -> None:
    """Mutation: emit ``CONTROL_MIN_POWER_KW`` when nothing smaller is representable.

    It reads like graceful degradation and it sends a command above the bound.
    With 0.15 kW of capacity the safe power is 0.135 kW, and the device minimum is
    0.2 kW -- so the "graceful" answer exports.
    """
    intent, requested, context, safe_kw = clamp_pieces(capacity_kw=0.15)

    honest = alphaess_device.limit_command(requested, safe_kw)

    assert safe_kw == pytest.approx(0.135)
    assert safe_kw < CONTROL_MIN_POWER_KW
    # Refused, not floored up to the minimum.
    assert honest is requested
    assert clamped_gate(intent, honest, context).inhibit_reason == INHIBIT_WOULD_EXPORT


def test_keeping_the_old_commanded_energy_after_lowering_the_power_is_caught() -> None:
    """Mutation: reduce the power and leave ``commanded_energy_ac_kwh`` alone.

    The command would then claim to deliver 0.275 kWh at 0.8 kW over a quarter
    hour, which is 0.2 kWh of energy and 0.075 kWh of fiction. Every downstream
    read-back comparison would be measured against a number nothing produces.
    """
    import dataclasses

    _, requested, _, safe_kw = clamp_pieces()

    honest = alphaess_device.limit_command(requested, safe_kw)
    stale = dataclasses.replace(honest, commanded_energy_ac_kwh=0.275)

    assert honest.commanded_energy_ac_kwh == pytest.approx(0.2)
    assert honest.commanded_energy_ac_kwh == pytest.approx(
        honest.power_kw * honest.interval_hours
    )
    assert stale.commanded_energy_ac_kwh != pytest.approx(
        stale.power_kw * stale.interval_hours
    )
    assert stale.undelivered_energy_ac_kwh < honest.undelivered_energy_ac_kwh


def test_letting_the_commanded_energy_exceed_the_allowance_is_caught() -> None:
    """Mutation: recompute energy from the *requested* power after clamping.

    The bound that must never break, whatever else does.
    """
    _, requested, _, safe_kw = clamp_pieces()

    honest = alphaess_device.limit_command(requested, safe_kw)

    assert honest.commanded_energy_ac_kwh <= honest.allowed_energy_ac_kwh
    assert honest.undelivered_energy_ac_kwh > 0.0
    # And the requested figure really was larger, so this is not vacuous.
    assert requested.commanded_energy_ac_kwh > honest.commanded_energy_ac_kwh


def test_extending_the_duration_to_compensate_is_caught() -> None:
    """Mutation: stretch the command so the reduced power delivers the same energy.

    It looks like preserving the decision and it is this module inventing a
    schedule: a 27-minute command outlives its planning interval, so the next
    refresh cannot supersede it cleanly. The duration is a dead-man margin, not a
    delivery window.
    """
    _, requested, _, safe_kw = clamp_pieces()

    honest = alphaess_device.limit_command(requested, safe_kw)
    stretched = requested.duration_minutes * (requested.power_kw / honest.power_kw)

    assert honest.duration_minutes == requested.duration_minutes
    assert stretched > requested.duration_minutes


def test_clamping_with_a_missing_grid_reading_is_caught() -> None:
    """Mutation: treat an absent meter as unlimited absorption.

    A missing reading is not evidence of capacity. The authoritative formula
    reads ``None`` as zero, so the bound collapses and the request is refused --
    and the gate's own ``grid_unusable`` condition fires first anyway, which is
    the belt to that braces.
    """
    intent, requested, context, safe_kw = clamp_pieces(grid_import_w=None)

    honest = alphaess_device.limit_command(requested, safe_kw)

    assert safe_kw == pytest.approx(0.0)
    assert honest is requested
    assert clamped_gate(intent, honest, context).inhibit_reason == (
        INHIBIT_GRID_UNUSABLE
    )
    # And an "unlimited" bound would have sent the whole request.
    assert alphaess_device.limit_command(requested, float("inf")) is requested


def test_disabling_would_export_entirely_is_caught() -> None:
    """Mutation: delete the condition now that commands are clamped.

    The clamp does not replace the gate. When nothing representable survives the
    clamp deliberately hands the *unreduced* request on, so removing the
    condition would send exactly the command it was refusing.
    """
    intent, requested, context, safe_kw = clamp_pieces(capacity_kw=0.05)

    honest = alphaess_device.limit_command(requested, safe_kw)
    verdict = clamped_gate(intent, honest, context)

    assert honest is requested
    assert verdict.safe is False
    assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT
    assert INHIBIT_WOULD_EXPORT in dict(verdict.checks)
    assert dict(verdict.checks)[INHIBIT_WOULD_EXPORT] is False


def test_bounding_the_clamp_by_the_forecast_house_load_is_caught() -> None:
    """Mutation: clamp to the house-load sensor instead of the meter.

    The beta.8 mistake, resurrected in a new place. On a site with production the
    house-load figure says nothing about export: 2 kW of load under 3.1 kW of sun
    is already exporting a kilowatt, and a clamp to 1.8 kW would add to it.
    """
    from .test_control_pipeline import make_context, make_intent

    intent = make_intent(energy_ac_kwh=CLAMP_REQUESTED_KW * INTERVAL_HOURS)
    requested = build_command(intent)
    # Sunny midday: 2 kW of load, 3.1 kW of PV, exporting ~1.1 kW.
    context = make_context(
        house_load_w=2000.0,
        grid_import_w=0.0,
        grid_export_w=1100.0,
        battery_power_w=0.0,
        device_power_kw=requested.power_kw,
        export_margin_percent=CLAMP_MARGIN_PERCENT,
    )

    honest_bound = safety.safe_discharge_power_kw(context)
    load_bound = 2.0 * (1.0 - CLAMP_MARGIN_PERCENT / 100.0)

    assert honest_bound == pytest.approx(0.0)
    assert load_bound == pytest.approx(1.8)
    assert alphaess_device.limit_command(requested, honest_bound) is requested
    assert clamped_gate(intent, requested, context).inhibit_reason == (
        INHIBIT_WOULD_EXPORT
    )


def test_bounding_the_clamp_by_a_planned_residual_is_caught() -> None:
    """Mutation: clamp to the Phase-8 plan's own predicted grid export headroom.

    A forecast is not a measurement. The safety bound has to come from the
    instrument that defines export at the instant the command would be sent, and
    the clamp reads nothing from the optimizer -- asserted from the source, so a
    later refactor cannot thread one in.
    """
    import inspect

    source = inspect.getsource(alphaess_device.limit_command)

    for forbidden in ("economic", "forecast", "plan", "pv_kwh", "baseline"):
        assert forbidden not in source, forbidden

    tree = ast.parse(inspect.getsource(alphaess_device))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0] if node.level else node.module)
    assert "economic" not in imported
    assert "plan" not in imported
    assert "simulation" not in imported


def test_treating_the_desired_export_action_as_executable_is_caught() -> None:
    """Mutation: map the Phase-8 ``export`` action onto the clamped discharge path.

    Two opposite intentions. The clamp exists so grid export does *not* occur, so
    it can never be the vehicle for an action that wants it. There is no helper
    family for an export at all, which is what makes this structural.
    """
    from custom_components.alpha_ems_manager.const import (
        ECONOMIC_ACTION_CURTAIL,
        ECONOMIC_ACTION_EXPORT,
    )

    assert ECONOMIC_ACTION_EXPORT not in alphaess_device.FAMILIES
    assert ECONOMIC_ACTION_CURTAIL not in alphaess_device.FAMILIES
    assert set(alphaess_device.FAMILIES) == {ACTION_DISCHARGE, "charge"}

    # And a command built for an unmapped action moves nothing, clamp or no clamp.
    from .test_control_pipeline import make_intent

    intent = make_intent(action=ECONOMIC_ACTION_EXPORT, energy_ac_kwh=1.0)
    command = build_command(intent)

    assert command.power_kw == 0.0
    assert command.moves_battery is False
    assert alphaess_device.limit_command(command, 5.0) is command


def test_letting_the_clamp_reach_back_into_the_planner_is_caught() -> None:
    """Mutation: have the clamp adjust the intent, or the plan, it came from.

    The layers must stay one-way. ``limit_command`` takes a command and a number
    and returns a command: it has no reference to an intent, a plan or a decision,
    so it cannot write to one.
    """
    import inspect

    signature = inspect.signature(alphaess_device.limit_command)

    assert list(signature.parameters) == ["command", "max_power_kw"]

    _, requested, _, safe_kw = clamp_pieces()
    before = requested.as_dict()
    alphaess_device.limit_command(requested, safe_kw)

    # The input command is untouched: the clamp returns a new one.
    assert requested.as_dict() == before


def test_making_execution_available_is_caught() -> None:
    """Mutation: flip the release barrier now that clamped commands are eligible.

    beta.15 makes ``eligible`` reachable far more often, which makes this the most
    consequential constant in the repository. Two independent refusals stand
    behind it, and both are asserted.
    """
    assert safety.CONTROL_EXECUTION_AVAILABLE is False

    intent, requested, context, safe_kw = clamp_pieces()
    command = alphaess_device.limit_command(requested, safe_kw)
    verdict = clamped_gate(intent, command, context)

    assert verdict.safe is True
    for mode in (CONTROL_MODE_SHADOW, CONTROL_MODE_ACTIVE):
        import dataclasses

        decision = safety.authorize(
            verdict,
            dataclasses.replace(
                context,
                mode=mode,
                device_power_kw=command.power_kw,
                execution_enabled=True,
            ),
            commands_planned=6,
            starts_or_increases=False,
        )
        assert decision.authorized is False


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
    assert alphaess_device.limit_command.__module__.endswith("alphaess_device")
    assert safety.safe_discharge_power_kw.__module__.endswith("safety")
    # The clamp still reduces the live case to the same figure it always did.
    _, requested, _, safe_kw = clamp_pieces()
    assert safe_kw == pytest.approx(0.891)
    assert alphaess_device.limit_command(requested, safe_kw).power_kw == pytest.approx(
        0.8
    )
