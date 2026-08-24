"""Clamping a discharge to what the house can absorb, instead of refusing it whole.

Before beta.15, a discharge command that exceeded the measured absorbing capacity
was refused **wholesale**: 1.1 kW against 0.99 kW of capacity produced
``inhibited`` / ``would_export`` and no discharge at all. Safe, and too coarse --
with low household load it made discharge-to-house effectively unusable for long
stretches, which the Phase-8 ``make_headroom`` recommendation exposed in practice.

Since beta.15 the command is clamped down to the largest representable
non-exporting power. It is still refused, with the same reason and the same
figures, when nothing representable survives.

Three properties carry the whole file, and each has its own section:

**The safety bound has exactly one definition.** :func:`safe_discharge_power_kw`
is what the command is clamped to *and* what the gate checks, so the number a
command was reduced to and the number it is judged against cannot drift apart.

**The clamp can only subtract.** The final power is at most the requested power
and at most the safe bound, both asserted over a swept grid rather than at chosen
points. Quantisation is downward; the margin is applied to the capacity before
any rounding, never to the command.

**The failure path is untouched.** Every case that inhibited before still
inhibits, with ``would_export`` and with the original unreduced figures -- because
when the clamp cannot produce a representable command it returns the command
unchanged and lets the existing gate refuse it.

Every case is synthetic except :data:`LIVE_CASE`, which is shaped after the
beta.14 diagnostics download that motivated the change.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.alphaess_device import (
    DeviceCommand,
    build_command,
    device_power_kw,
    limit_command,
)
from custom_components.alpha_ems_manager.battery import INTERVAL_HOURS
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_HOLD,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_SHADOW,
    CONTROL_POWER_STEP_KW,
    CONTROL_STATE_ELIGIBLE,
    CONTROL_STATE_INHIBITED,
    CONTROL_STATE_OFF,
    INHIBIT_GRID_STALE,
    INHIBIT_GRID_UNUSABLE,
    INHIBIT_WOULD_EXPORT,
    REFUSE_MODE_NOT_ACTIVE,
)
from custom_components.alpha_ems_manager.safety import (
    ControlContext,
    absorbing_capacity_kw,
    authorize,
    evaluate,
    safe_discharge_power_kw,
)

from .test_control_pipeline import make_context, make_intent

#: The live case that motivated beta.15, from the beta.14 diagnostics of
#: 2026-08-21: a 1.1 kW discharge recommended against 0.99 kW of measured
#: absorbing capacity, with the default 10 % margin.
#:
#: The expected command is **derived** from the real device contract rather than
#: written down, so a change to the helper step cannot leave this asserting a
#: figure the device could not accept.
LIVE_REQUESTED_KW = 1.1
LIVE_CAPACITY_KW = 0.99
LIVE_MARGIN_PERCENT = 10.0
LIVE_SAFE_RAW_KW = LIVE_CAPACITY_KW * (1.0 - LIVE_MARGIN_PERCENT / 100.0)


def quantised_down(power_kw: float) -> float:
    """Return ``power_kw`` floored to a helper step, via the real contract."""
    return device_power_kw(power_kw * INTERVAL_HOURS, INTERVAL_HOURS)


def clamped(
    *,
    requested_kw: float = LIVE_REQUESTED_KW,
    capacity_kw: float = LIVE_CAPACITY_KW,
    margin_percent: float = LIVE_MARGIN_PERCENT,
    discharge_now_kw: float = 0.0,
    **context_overrides,
) -> tuple[DeviceCommand, DeviceCommand, ControlContext, float]:
    """Drive the real pipeline once, and return every stage of it.

    Returns ``(requested, command, context, safe_kw)`` where ``context`` carries
    the *final* command's power -- which is what the gate evaluates.

    The absorbing capacity is produced by feeding the meter, so the authoritative
    formula computes it. Nothing here reimplements it: the assertion below that
    it comes out equal to ``capacity_kw`` is what proves that.
    """
    intent = make_intent(energy_ac_kwh=requested_kw * INTERVAL_HOURS)
    requested = build_command(intent)

    import_w = max(0.0, capacity_kw - discharge_now_kw) * 1000.0
    readings = {
        "grid_import_w": import_w,
        "grid_export_w": 0.0,
        "battery_power_w": -discharge_now_kw * 1000.0,
        "house_load_w": max(50.0, capacity_kw * 1000.0),
        "export_margin_percent": margin_percent,
        **context_overrides,
    }

    probe = make_context(device_power_kw=requested.power_kw, **readings)
    safe_kw = safe_discharge_power_kw(probe)
    command = limit_command(requested, safe_kw)
    context = make_context(device_power_kw=command.power_kw, **readings)
    return requested, command, context, safe_kw


# --- A. the bound has one definition ----------------------------------------


def test_the_bound_is_the_capacity_with_the_margin_taken_off_it() -> None:
    """Capacity first, margin second. Stated once, in one function."""
    _, _, context, safe_kw = clamped()

    assert absorbing_capacity_kw(context) == pytest.approx(LIVE_CAPACITY_KW)
    assert safe_kw == pytest.approx(LIVE_SAFE_RAW_KW)
    assert safe_kw == pytest.approx(0.891)


def test_the_bound_does_not_depend_on_the_command_it_is_taken_around() -> None:
    """The load-bearing property that makes the two-context pipeline sound.

    The bound reads the meter, the battery power and the margin -- never
    ``device_power_kw``. If it ever did, taking the bound from a context built
    around the *requested* command and then evaluating the *limited* one would be
    circular, and the clamp could talk itself into a higher power.
    """
    readings = {
        "grid_import_w": 990.0,
        "grid_export_w": 0.0,
        "battery_power_w": 0.0,
        "export_margin_percent": 10.0,
    }
    bounds = {
        safe_discharge_power_kw(make_context(device_power_kw=power, **readings))
        for power in (0.0, 0.2, 0.8, 1.1, 5.0, 20.0)
    }

    assert len(bounds) == 1


def test_the_gate_checks_the_same_bound_the_clamp_used() -> None:
    """One function, two callers. They cannot disagree.

    Asserted by construction rather than by comparing two numbers: the gate is
    made to pass at exactly the bound and fail one step above it.
    """
    readings = {
        "grid_import_w": 1000.0,
        "grid_export_w": 0.0,
        "battery_power_w": 0.0,
        "export_margin_percent": 10.0,
    }
    bound = safe_discharge_power_kw(make_context(**readings))
    intent = make_intent(energy_ac_kwh=1.0)

    at_bound = make_context(device_power_kw=bound, **readings)
    above = make_context(device_power_kw=bound + 1e-6, **readings)

    assert bound == pytest.approx(0.9)
    assert evaluate(intent, at_bound).safe is True
    assert evaluate(intent, above).inhibit_reason == INHIBIT_WOULD_EXPORT


def test_a_margin_of_zero_leaves_the_capacity_as_the_bound() -> None:
    """And proves the margin is doing work at ten percent, not decoration."""
    _, with_margin, _, safe_with = clamped(margin_percent=10.0)
    _, without, _, safe_without = clamped(margin_percent=0.0)

    assert safe_without == pytest.approx(LIVE_CAPACITY_KW)
    assert safe_with < safe_without
    assert with_margin.power_kw < without.power_kw


def test_a_margin_of_a_hundred_percent_forbids_every_discharge() -> None:
    """No sensible user sets this; a bound that inverted at 100 % would be a bug."""
    _, command, context, safe_kw = clamped(margin_percent=100.0)

    assert safe_kw == pytest.approx(0.0)
    assert command.safety_limited is False
    assert evaluate(make_intent(), context).inhibit_reason == INHIBIT_WOULD_EXPORT


# --- B. the live case -------------------------------------------------------


def test_the_live_case_is_clamped_to_the_largest_representable_step() -> None:
    """1.1 kW requested against 0.99 kW of capacity becomes 0.8 kW, not nothing.

    The exact figure is derived from the device contract, so this asserts the
    *rule* and reports the number rather than hard-coding a step size. At the
    real 0.1 kW step it is 0.8 kW, because 0.9 would exceed the 0.891 bound.
    """
    requested, command, context, safe_kw = clamped()
    expected = quantised_down(safe_kw)

    assert requested.power_kw == pytest.approx(LIVE_REQUESTED_KW)
    assert command.power_kw == pytest.approx(expected)
    assert command.power_kw == pytest.approx(0.8)
    assert command.safety_limited is True
    assert command.power_kw <= safe_kw
    # The step above the answer would have breached the bound, which is what
    # makes this the *largest* representable safe command.
    assert command.power_kw + CONTROL_POWER_STEP_KW > safe_kw

    verdict = evaluate(
        make_intent(energy_ac_kwh=LIVE_REQUESTED_KW * INTERVAL_HOURS), context
    )
    assert verdict.safe is True
    assert verdict.inhibit_reason is None


def test_the_live_case_is_eligible_in_shadow_and_refused_only_by_the_mode() -> None:
    """The whole point: eligible, safely reduced, and still not sent.

    ``mode_not_active`` rather than ``would_export`` is the observable difference
    beta.15 makes to this exact download.
    """
    from custom_components.alpha_ems_manager.const import (
        CONTROL_MODE_SHADOW,
        REFUSE_MODE_NOT_ACTIVE,
    )

    _, command, context, _ = clamped()
    verdict = evaluate(
        make_intent(energy_ac_kwh=LIVE_REQUESTED_KW * INTERVAL_HOURS), context
    )
    decision = authorize(verdict, context, commands_planned=6, starts_or_increases=True)

    assert context.mode == CONTROL_MODE_SHADOW
    assert verdict.safe is True
    assert decision.authorized is False
    assert decision.refusal == REFUSE_MODE_NOT_ACTIVE
    assert decision.unsafe_reason is None
    assert command.power_kw == pytest.approx(0.8)


def test_slightly_less_absorption_leaves_nothing_representable() -> None:
    """The companion case, and the boundary is exact.

    A safe bound below the device minimum cannot be commanded at all, so the
    request reaches the gate unreduced and is refused with the original reason
    and the original figure. ``0.222 kW`` of capacity yields ``0.1998 kW`` safe,
    which floors to one step -- below the two-step minimum.
    """
    requested, command, context, safe_kw = clamped(capacity_kw=0.222)

    assert safe_kw == pytest.approx(0.1998)
    assert safe_kw < CONTROL_MIN_POWER_KW
    assert command is requested
    assert command.safety_limited is False
    assert command.power_kw == pytest.approx(LIVE_REQUESTED_KW)

    verdict = evaluate(
        make_intent(energy_ac_kwh=LIVE_REQUESTED_KW * INTERVAL_HOURS), context
    )
    assert verdict.safe is False
    assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT


def test_the_smallest_absorption_that_still_permits_a_command() -> None:
    """One step either side of the minimum, so the threshold is pinned exactly."""
    threshold_capacity = CONTROL_MIN_POWER_KW / 0.9

    _, just_enough, _, safe_enough = clamped(capacity_kw=threshold_capacity)
    _, not_enough, _, safe_short = clamped(capacity_kw=threshold_capacity * 0.99)

    assert safe_enough == pytest.approx(CONTROL_MIN_POWER_KW)
    assert just_enough.power_kw == pytest.approx(CONTROL_MIN_POWER_KW)
    assert just_enough.safety_limited is True

    assert safe_short < CONTROL_MIN_POWER_KW
    assert not_enough.safety_limited is False


# --- C. the clamp can only subtract -----------------------------------------


@pytest.mark.parametrize("capacity_kw", [0.0, 0.1, 0.25, 0.5, 0.99, 1.5, 3.0, 12.0])
@pytest.mark.parametrize("requested_kw", [0.2, 0.5, 1.1, 2.0, 5.0])
def test_the_final_power_never_exceeds_the_request_or_the_bound(
    capacity_kw: float, requested_kw: float
) -> None:
    """The two invariants, swept rather than sampled.

    ``final <= requested`` is unconditional. ``final <= safe`` holds *whenever
    the gate passes* -- when it does not, the command is deliberately the
    unreduced request and the gate is what stops it.
    """
    requested, command, context, safe_kw = clamped(
        requested_kw=requested_kw, capacity_kw=capacity_kw
    )
    verdict = evaluate(
        make_intent(energy_ac_kwh=requested_kw * INTERVAL_HOURS), context
    )

    assert command.power_kw <= requested.power_kw + 1e-12
    if verdict.safe:
        assert command.power_kw <= safe_kw + 1e-12
    else:
        assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT
        assert command is requested


def test_a_request_below_the_bound_is_left_completely_alone() -> None:
    """Not merely equal -- the identical object, so nothing was rebuilt."""
    requested, command, _, safe_kw = clamped(capacity_kw=4.0)

    assert safe_kw > LIVE_REQUESTED_KW
    assert command is requested
    assert command.safety_limited is False
    assert command.undelivered_energy_ac_kwh == pytest.approx(0.0)


def test_a_request_exactly_at_the_bound_is_left_alone() -> None:
    """The boundary case, and it must not be reduced by a rounding artefact."""
    capacity_kw = LIVE_REQUESTED_KW / 0.9
    requested, command, _, safe_kw = clamped(capacity_kw=capacity_kw)

    assert safe_kw == pytest.approx(LIVE_REQUESTED_KW)
    assert command is requested
    assert command.power_kw == pytest.approx(LIVE_REQUESTED_KW)


def test_the_final_power_is_always_a_whole_number_of_helper_steps() -> None:
    """Downward quantisation through the real contract, swept."""
    for capacity_kw in (0.25, 0.33, 0.47, 0.62, 0.78, 0.91, 1.07, 1.44, 2.31):
        _, command, _, _ = clamped(capacity_kw=capacity_kw)
        steps = command.power_kw / CONTROL_POWER_STEP_KW
        assert steps == pytest.approx(round(steps), abs=1e-9), command.power_kw


def test_the_margin_is_applied_before_the_quantisation_never_after() -> None:
    """Ordering, demonstrated by a case where the two orders differ.

    With 1.0 kW of capacity the correct order gives 0.9 kW. Quantising first and
    then applying the margin would give ``1.0 * 0.9 = 0.9`` too -- so the case
    has to be chosen where they diverge. At 0.99 kW: correct order 0.8 kW;
    quantise-then-margin would give ``0.9 * 0.9 = 0.81`` and, floored, 0.8 as
    well. The order that actually breaks is *margin ignored*, which is the
    mutation, and *rounding the capacity up*, which the sweep above forbids.

    What this asserts is the observable consequence: the final command clears the
    margin, and the step above it does not.
    """
    for capacity_kw in (0.99, 1.0, 1.11, 1.23, 2.0):
        _, command, _, safe_kw = clamped(capacity_kw=capacity_kw)
        if not command.safety_limited:
            continue
        assert command.power_kw <= safe_kw
        assert command.power_kw + CONTROL_POWER_STEP_KW > safe_kw
        # And strictly below the unmargined capacity, which is what the margin buys.
        assert command.power_kw < capacity_kw


# --- D. only a non-exporting discharge is clamped ---------------------------


def test_a_charge_is_never_export_limited() -> None:
    """A charge cannot export, so an export bound has nothing to say about it."""
    intent = make_intent(action=ACTION_CHARGE, energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)

    assert limit_command(requested, 0.0) is requested
    assert limit_command(requested, 0.2) is requested


def test_a_hold_is_never_limited() -> None:
    """Nothing to reduce, and a hold must not acquire a safety story."""
    intent = make_intent(action=ACTION_HOLD, energy_ac_kwh=0.0)
    requested = build_command(intent)

    assert limit_command(requested, 0.0) is requested
    assert requested.power_kw == 0.0
    assert requested.safety_limited is False


def test_an_unprovable_bound_is_not_a_licence_to_send_the_request() -> None:
    """A non-finite ceiling leaves the command for the gate, never widens it."""
    intent = make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)

    for ceiling in (float("nan"), float("inf")):
        assert limit_command(requested, ceiling) is requested


def test_a_negative_bound_never_produces_a_negative_command() -> None:
    """Floored at zero, and then refused for being under the minimum."""
    intent = make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)

    command = limit_command(requested, -5.0)

    assert command is requested
    assert command.power_kw > 0.0


# --- E. the energy bookkeeping ----------------------------------------------


def test_the_commanded_energy_is_recomputed_from_the_reduced_power() -> None:
    """The bookkeeping must not keep energy calculated for a power nobody sends."""
    requested, command, _, _ = clamped()

    assert command.power_kw == pytest.approx(0.8)
    assert command.interval_hours == pytest.approx(INTERVAL_HOURS)
    assert command.commanded_energy_ac_kwh == pytest.approx(0.8 * INTERVAL_HOURS)
    assert command.commanded_energy_ac_kwh == pytest.approx(0.2)
    # And it really did move: the request would have commanded 0.275.
    assert requested.commanded_energy_ac_kwh == pytest.approx(0.275)


def test_the_allowed_energy_is_never_touched_by_the_clamp() -> None:
    """It is the clamp's own output from Phase 3 and this layer may not move it."""
    requested, command, _, _ = clamped()

    assert command.allowed_energy_ac_kwh == requested.allowed_energy_ac_kwh
    assert command.allowed_energy_ac_kwh == pytest.approx(0.275)


@pytest.mark.parametrize("capacity_kw", [0.25, 0.5, 0.99, 1.5, 3.0])
@pytest.mark.parametrize("requested_kw", [0.5, 1.1, 2.0])
def test_the_commanded_energy_never_exceeds_the_allowed_energy(
    capacity_kw: float, requested_kw: float
) -> None:
    """The invariant the whole energy contract rests on, swept."""
    _, command, _, _ = clamped(requested_kw=requested_kw, capacity_kw=capacity_kw)

    assert command.commanded_energy_ac_kwh <= command.allowed_energy_ac_kwh + 1e-12
    assert command.undelivered_energy_ac_kwh >= 0.0


def test_the_undelivered_energy_reflects_the_clamp() -> None:
    """What the reduction gave up, exactly, and it is not hidden."""
    _, command, _, _ = clamped()

    assert command.undelivered_energy_ac_kwh == pytest.approx(0.275 - 0.2)
    assert command.undelivered_energy_ac_kwh == pytest.approx(0.075)


def test_the_duration_is_never_extended_to_compensate() -> None:
    """A reduced power over the same window, not the same energy over longer.

    Extending the duration would be this layer inventing a schedule. The next
    refresh issues its own command, which is the architecture's answer.
    """
    requested, command, _, _ = clamped()

    assert command.duration_minutes == requested.duration_minutes


def test_the_cutoff_is_never_changed_by_the_clamp() -> None:
    """The floor backstop bounds battery energy and says nothing about export."""
    requested, command, _, _ = clamped()

    assert command.cutoff_soc_percent == requested.cutoff_soc_percent


def test_the_action_and_the_energy_limit_flag_survive_the_clamp() -> None:
    """A reduction, never a substitution."""
    requested, command, _, _ = clamped()

    assert command.action == requested.action == ACTION_DISCHARGE
    assert command.energy_limit_bound == requested.energy_limit_bound
    assert command.device_hold_flag is False


# --- F. every existing refusal survives -------------------------------------


def test_no_capacity_at_all_still_inhibits() -> None:
    """Zero absorption, and the reason is unchanged."""
    requested, command, context, safe_kw = clamped(capacity_kw=0.0)

    assert safe_kw == pytest.approx(0.0)
    assert command is requested
    assert evaluate(make_intent(), context).inhibit_reason == INHIBIT_WOULD_EXPORT


def test_an_already_exporting_site_still_inhibits() -> None:
    """The formula floors at zero, so exporting leaves nothing to clamp to."""
    intent = make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)
    readings = {
        "grid_import_w": 0.0,
        "grid_export_w": 500.0,
        "battery_power_w": 0.0,
        "export_margin_percent": 10.0,
    }
    safe_kw = safe_discharge_power_kw(make_context(**readings))
    command = limit_command(requested, safe_kw)
    context = make_context(device_power_kw=command.power_kw, **readings)

    assert safe_kw == pytest.approx(0.0)
    assert command is requested
    assert evaluate(intent, context).inhibit_reason == INHIBIT_WOULD_EXPORT


def test_exporting_while_discharging_can_still_leave_real_capacity() -> None:
    """Not a special case -- the authoritative formula already handles it.

    A site exporting 200 W *because* the battery is putting out 900 W still has
    700 W of genuine absorption, and the clamp may use it. Inventing a rule that
    refused any exporting site would have thrown that away.
    """
    intent = make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)
    readings = {
        "grid_import_w": 0.0,
        "grid_export_w": 200.0,
        "battery_power_w": -900.0,
        "export_margin_percent": 10.0,
    }
    safe_kw = safe_discharge_power_kw(make_context(**readings))
    command = limit_command(requested, safe_kw)
    context = make_context(device_power_kw=command.power_kw, **readings)

    assert safe_kw == pytest.approx(0.63)
    assert command.power_kw == pytest.approx(0.6)
    assert evaluate(intent, context).safe is True


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"grid_import_w": None}, INHIBIT_GRID_UNUSABLE),
        ({"grid_export_w": None}, INHIBIT_GRID_UNUSABLE),
        ({"grid_age_seconds": 9999.0}, INHIBIT_GRID_STALE),
    ],
)
def test_a_missing_or_stale_meter_still_refuses_before_any_clamp(
    override: dict, reason: str
) -> None:
    """The clamp cannot rescue a command whose evidence is not there.

    These conditions are ordered *before* ``would_export`` and are unchanged, so
    a clamp computed from an absent meter never gets the chance to be believed --
    a missing reading yields a zero capacity and therefore no reduction anyway.
    """
    intent = make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)
    readings = {"grid_import_w": 990.0, "grid_export_w": 0.0, **override}
    safe_kw = safe_discharge_power_kw(make_context(**readings))
    command = limit_command(requested, safe_kw)
    context = make_context(device_power_kw=command.power_kw, **readings)

    assert evaluate(intent, context).inhibit_reason == reason


def test_a_higher_priority_refusal_still_wins_over_a_clamped_command() -> None:
    """Being safely reduced does not make a command eligible for other reasons."""
    from custom_components.alpha_ems_manager.const import (
        INHIBIT_AT_OR_BELOW_FLOOR,
        INHIBIT_DISPATCH_ACTIVE,
    )

    intent = make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)
    base = {"grid_import_w": 990.0, "grid_export_w": 0.0, "battery_power_w": 0.0}
    command = limit_command(requested, safe_discharge_power_kw(make_context(**base)))

    assert command.safety_limited is True

    blocked = make_context(
        device_power_kw=command.power_kw, dispatch_active=True, **base
    )
    assert evaluate(intent, blocked).inhibit_reason == INHIBIT_DISPATCH_ACTIVE

    at_floor = make_context(device_power_kw=command.power_kw, soc_percent=20.0, **base)
    assert evaluate(intent, at_floor).inhibit_reason == INHIBIT_AT_OR_BELOW_FLOOR


# --- G. the low-load ladder -------------------------------------------------


@pytest.mark.parametrize(
    ("label", "capacity_kw", "expect_limited", "expect_safe"),
    [
        ("2.0 kW absorption", 2.0, False, True),
        ("1.0 kW absorption", 1.0, True, True),
        ("0.5 kW absorption", 0.5, True, True),
        ("0.25 kW absorption", 0.25, True, True),
        ("0.05 kW absorption", 0.05, False, False),
        ("no absorption", 0.0, False, False),
    ],
)
def test_the_ladder_from_plenty_of_load_down_to_none(
    label: str, capacity_kw: float, expect_limited: bool, expect_safe: bool
) -> None:
    """Requested 1.1 kW throughout, so only the absorption varies."""
    _, command, context, _ = clamped(capacity_kw=capacity_kw)
    verdict = evaluate(make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS), context)

    assert command.safety_limited is expect_limited, label
    assert verdict.safe is expect_safe, label
    if not expect_safe:
        assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT, label


# --- H. stability under meter noise -----------------------------------------


def test_meter_noise_moves_the_command_at_most_one_step_at_a_time() -> None:
    """No hysteresis mechanism is introduced, and this is why none is needed.

    A hundred watts of noise on a healthy absorption figure moves the command by
    at most one helper step, and every value it takes is safe. Alternating
    between two adjacent safe steps is cosmetic: both are below the bound, and a
    reduction is exempt from the write cooldown precisely because reducing cannot
    increase risk.
    """
    powers = set()
    for noise_w in range(-100, 101, 10):
        _, command, _, safe_kw = clamped(capacity_kw=0.99 + noise_w / 1000.0)
        assert command.power_kw <= safe_kw + 1e-12
        powers.add(command.power_kw)

    assert powers, "the sweep produced nothing"
    assert max(powers) - min(powers) <= 2 * CONTROL_POWER_STEP_KW + 1e-9


def test_flipping_between_eligible_and_inhibited_needs_a_real_load_change() -> None:
    """The eligible/inhibited boundary is far from the working point, not beside it.

    Going from the live 0.8 kW command to no command at all requires the
    absorption to fall from 0.99 kW to below 0.222 kW -- a swing of more than
    three quarters of a kilowatt. That is a load switching off, not meter noise.

    And in the rare case where absorption really does hover at the threshold, the
    existing write cooldown covers it: restarting a command is a *start*, which
    is rate-limited to one planning interval, while every reduction is exempt.
    """
    from custom_components.alpha_ems_manager.const import CONTROL_COOLDOWN_SECONDS

    inhibit_below_kw = CONTROL_MIN_POWER_KW / 0.9
    working_point_kw = LIVE_CAPACITY_KW

    assert inhibit_below_kw == pytest.approx(0.2222, abs=1e-4)
    assert working_point_kw - inhibit_below_kw > 0.75
    assert CONTROL_COOLDOWN_SECONDS == 900


# --- I. nothing about export became executable ------------------------------


def test_the_clamp_bounds_a_discharge_and_cannot_produce_an_export() -> None:
    """The clamp's whole purpose is that grid export does not occur.

    Its output is always at or below the safely absorbable power, so by
    construction it cannot be the vehicle for the Phase-8 ``export`` action --
    which wants the opposite thing.
    """
    for capacity_kw in (0.25, 0.5, 0.99, 2.0, 5.0):
        _, command, context, safe_kw = clamped(capacity_kw=capacity_kw)
        verdict = evaluate(make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS), context)
        if verdict.safe and command.moves_battery:
            assert command.power_kw <= absorbing_capacity_kw(context)
            assert command.power_kw <= safe_kw + 1e-12


def test_the_clamp_names_no_export_actuator() -> None:
    """Read from the source, so a later refactor cannot smuggle one in."""
    import inspect

    from custom_components.alpha_ems_manager import alphaess_device

    source = inspect.getsource(alphaess_device.limit_command)

    for forbidden in ("force_export", "force_import", "excess_export", "pv_switch"):
        assert forbidden not in source, forbidden


def test_the_clamp_is_pure_and_returns_a_command_not_a_side_effect() -> None:
    """Same inputs, same answer, and the input command is never mutated."""
    intent = make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)
    before = requested.as_dict()

    first = limit_command(requested, 0.891)
    second = limit_command(requested, 0.891)

    assert first == second
    assert requested.as_dict() == before
    assert requested.power_kw == pytest.approx(1.1)


# ===========================================================================
# J. through the real coordinator
# ===========================================================================
#
# Everything above drives the pure functions. This section drives the whole
# pipeline the way a refresh does, because the two-context assembly, the
# diagnostics contract and the "nothing is sent" promise are properties of the
# wiring rather than of the arithmetic.


@pytest.fixture
def captured_calls(hass) -> list:
    """Capture every call to a service the control layer is allowed to make.

    Registered as real handlers so a write attempt would *succeed* rather than
    raise -- otherwise an attempted call could be mistaken for an absent service
    and the test would pass for the wrong reason.
    """
    from custom_components.alpha_ems_manager.alphaess_device import PERMITTED_SERVICES

    calls: list = []

    async def record(call) -> None:
        calls.append(call)

    for domain, service in PERMITTED_SERVICES:
        hass.services.async_register(domain, service, record)
    return calls


def mode_setters() -> set[str]:
    """Return every module that calls ``set_control_mode``.

    Module level rather than inside the async test, because a coroutine has no
    business doing blocking filesystem work -- and the answer is a property of the
    package, not of any one refresh.
    """
    import ast
    from pathlib import Path

    found: set[str] = set()
    for path in Path("custom_components/alpha_ems_manager").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_control_mode"
            ):
                found.add(path.stem)
    return found


def point_meter(hass, watts: float) -> None:
    """Describe a site importing ``watts`` with the sun down and the battery idle.

    Self-consistent by construction: ``load = 0 PV + 0 battery + watts import``,
    so the authoritative formula reads exactly ``watts`` of absorbing capacity.
    """
    from .conftest import BATTERY_POWER, GRID_POWER, HOUSE_LOAD, PV_POWER, set_sensor

    set_sensor(hass, PV_POWER, 0, "W", "power")
    set_sensor(hass, HOUSE_LOAD, watts, "W", "power")
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, watts, "W", "power")


async def drive_control(
    hass, entry, *, import_w: float, mode: str = CONTROL_MODE_SHADOW
) -> dict:
    """Point the meter at one absorption figure and return the control report.

    Driven to a real *discharge* recommendation via the Phase-3 history, because
    with a hold the state would be ``idle`` and the interesting case would go
    untested.
    """
    from .test_battery_entities import drive
    from .test_control_modes import set_mode

    await set_mode(hass, mode)
    point_meter(hass, import_w)
    await drive(entry.runtime_data)
    return entry.runtime_data.control_report


async def test_the_export_check_block_reports_every_stage(
    hass, setup_integration, control_surface
) -> None:
    """The diagnostics contract, asserted as an exact key set.

    This block had **no test at all** before beta.15 -- a pre-existing gap, and
    the reason a rename in it could have gone out silently. Pinned as a set so a
    useful field cannot be quietly swapped for a useless one.
    """
    report = await drive_control(hass, setup_integration, import_w=2000)
    check = report["export_check"]

    assert set(check) == {
        "requested_power_kw",
        "absorbing_capacity_kw",
        "safety_margin_percent",
        "safe_capacity_kw",
        "safety_limited",
        "limited_power_kw",
        "final_command_power_kw",
        "grid_import_w",
        "grid_export_w",
        "battery_power_w",
        "inhibit_reason",
        "basis",
        "ordering",
    }
    assert check["safety_margin_percent"] == 10.0
    assert "clamped down to the maximum safely absorbable" in check["basis"]
    assert "if no representable safe command remains" in check["basis"]
    assert "apply the margin to the capacity" in check["ordering"]
    for key, value in check.items():
        assert not isinstance(value, (dict, list, tuple)), key


async def test_a_healthy_absorption_reports_no_limiting(
    hass, setup_integration, control_surface
) -> None:
    """Two kilowatts of import against a sub-kilowatt request: nothing to reduce."""
    report = await drive_control(hass, setup_integration, import_w=2000)
    check = report["export_check"]

    assert check["absorbing_capacity_kw"] == pytest.approx(2.0)
    assert check["safe_capacity_kw"] == pytest.approx(1.8)
    assert check["safety_limited"] is False
    assert check["limited_power_kw"] is None
    assert check["final_command_power_kw"] == check["requested_power_kw"]
    assert report["state"] == CONTROL_STATE_ELIGIBLE
    assert report["command"]["safety_limited"] is False


async def test_a_small_absorption_is_reported_as_limited_and_stays_eligible(
    hass, setup_integration, control_surface
) -> None:
    """The behaviour change, end to end: eligible with a reduced power.

    Before beta.15 this exact refresh produced ``inhibited`` / ``would_export``.
    """
    report = await drive_control(hass, setup_integration, import_w=400)
    check = report["export_check"]

    assert check["absorbing_capacity_kw"] == pytest.approx(0.4)
    assert check["safe_capacity_kw"] == pytest.approx(0.36)
    assert check["safety_limited"] is True
    assert check["limited_power_kw"] == pytest.approx(0.3)
    assert check["final_command_power_kw"] == pytest.approx(0.3)
    assert check["final_command_power_kw"] < check["requested_power_kw"]
    assert check["inhibit_reason"] is None

    assert report["state"] == CONTROL_STATE_ELIGIBLE
    assert report["safety"]["safe"] is True
    assert report["command"]["safety_limited"] is True
    assert report["command"]["power_kw"] == pytest.approx(0.3)
    assert report["command"]["requested_power_kw"] > 0.3
    # Only the mode stopped it, which is what shadow is for.
    assert report["authorization"]["refusal"] == REFUSE_MODE_NOT_ACTIVE
    assert report["last_write"] is None


async def test_almost_no_absorption_still_inhibits_with_the_old_reason(
    hass, setup_integration, control_surface
) -> None:
    """The failure path is unchanged, including the unreduced figures it reports."""
    report = await drive_control(hass, setup_integration, import_w=60)
    check = report["export_check"]

    assert report["state"] == CONTROL_STATE_INHIBITED
    assert report["safety"]["inhibit_reason"] == INHIBIT_WOULD_EXPORT
    assert check["inhibit_reason"] == INHIBIT_WOULD_EXPORT
    assert check["safety_limited"] is False
    assert check["limited_power_kw"] is None
    # The request, not a reduction of it: that is what makes the refusal legible.
    assert check["final_command_power_kw"] == check["requested_power_kw"]
    assert report["command"]["safety_limited"] is False


async def test_the_energy_bookkeeping_is_consistent_in_the_real_report(
    hass, setup_integration, control_surface
) -> None:
    """Commanded energy follows the reduced power, and stays within the allowance."""
    report = await drive_control(hass, setup_integration, import_w=400)
    command = report["command"]

    assert command["safety_limited"] is True
    assert command["commanded_energy_ac_kwh"] == pytest.approx(
        command["power_kw"] * INTERVAL_HOURS, abs=1e-4
    )
    assert command["commanded_energy_ac_kwh"] <= command["allowed_energy_ac_kwh"]
    assert command["undelivered_energy_ac_kwh"] == pytest.approx(
        command["allowed_energy_ac_kwh"] - command["commanded_energy_ac_kwh"], abs=1e-4
    )
    assert command["undelivered_energy_ac_kwh"] > 0.0


async def test_the_commands_carry_the_limited_power_not_the_requested_one(
    hass, setup_integration, control_surface
) -> None:
    """The step list is what would be written, so it must carry the reduction.

    The whole value of shadow rests on this: if the reported step list carried
    the requested power while the verdict was computed on the reduced one, shadow
    would be showing a command active would never send.
    """
    report = await drive_control(hass, setup_integration, import_w=400)

    powers = [
        step["value"]
        for step in report["commands"]
        if "power" in step["entity_id"] and step.get("value") is not None
    ]

    assert powers == [pytest.approx(0.3)]


async def test_shadow_and_active_clamp_identically(
    hass, setup_integration, control_surface
) -> None:
    """The clamp is upstream of the only mode-aware stage, so it cannot differ."""
    from custom_components.alpha_ems_manager.const import CONTROL_MODE_ACTIVE

    shadow = await drive_control(hass, setup_integration, import_w=400)
    shadow_check = dict(shadow["export_check"])
    shadow_command = dict(shadow["command"])

    active = await drive_control(
        hass, setup_integration, import_w=400, mode=CONTROL_MODE_ACTIVE
    )

    assert active["export_check"] == shadow_check
    assert active["command"] == shadow_command
    assert active["state"] == shadow["state"] == CONTROL_STATE_ELIGIBLE


async def test_a_clamped_command_still_reaches_no_service(
    hass, setup_integration, control_surface, captured_calls
) -> None:
    """The barrier is downstream of everything above it.

    Asserted in **active** mode with a clamped, eligible, safe command -- the
    most permissive state the release can reach -- so this is the strongest form
    of the zero-actuation promise for this change.
    """
    from custom_components.alpha_ems_manager.const import (
        CONTROL_EXECUTION_AVAILABLE,
        CONTROL_MODE_ACTIVE,
    )

    report = await drive_control(
        hass, setup_integration, import_w=400, mode=CONTROL_MODE_ACTIVE
    )

    assert CONTROL_EXECUTION_AVAILABLE is False
    assert report["state"] == CONTROL_STATE_ELIGIBLE
    assert report["command"]["safety_limited"] is True
    assert report["commands_planned"] > 0
    assert report["authorization"]["authorized"] is False
    assert report["last_write"] is None
    assert captured_calls == []


async def test_the_clamp_changes_no_economic_figure(
    hass, setup_integration, control_surface, frank
) -> None:
    """The planner is untouched. Only the final command may be limited.

    Two refreshes whose meters differ by more than a kilowatt of absorption --
    enough to clamp one and not the other -- must produce the same desired plan,
    the same capability plan and the same euro figures. The safety layer may
    subtract capability; it may not reach backwards into the optimizer.
    """
    from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
    from .frank_capture import synthetic_day
    from .test_control_modes import set_mode

    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await set_mode(hass, CONTROL_MODE_SHADOW)

    point_meter(hass, 2000)
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    plenty = coordinator.data["economic"]
    plenty_report = coordinator.control_report

    point_meter(hass, 400)
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    scarce = coordinator.data["economic"]
    scarce_report = coordinator.control_report

    # The safety layer saw two different worlds...
    assert (
        plenty_report["export_check"]["safe_capacity_kw"]
        > (scarce_report["export_check"]["safe_capacity_kw"])
    )
    # ...and the optimizer noticed none of it.
    assert scarce.action == plenty.action
    assert scarce.capability_action == plenty.capability_action
    assert scarce.capability_gap_reason == plenty.capability_gap_reason
    assert scarce.desired.cost_eur == pytest.approx(plenty.desired.cost_eur)
    assert scarce.capability.cost_eur == pytest.approx(plenty.capability.cost_eur)
    assert scarce.economic_value_forgone_eur == pytest.approx(
        plenty.economic_value_forgone_eur
    )


async def test_the_entity_count_and_the_economic_contract_are_untouched(
    hass, setup_integration, control_surface
) -> None:
    """No new entity, no new attribute. This change is diagnostics and safety."""
    from .test_economic_published import ECONOMIC_ATTRIBUTES
    from .test_entity_contract import CONTRACT

    await drive_control(hass, setup_integration, import_w=400)

    assert len(CONTRACT) == 13
    assert len(ECONOMIC_ATTRIBUTES) == 8

    economic = hass.states.get("sensor.alpha_ems_economic_action")
    assert economic is not None
    assert "safety_limited" not in economic.attributes


# ===========================================================================
# K. the diff-audit questions, asserted rather than reasoned about
# ===========================================================================


def test_swapping_import_for_export_can_only_shrink_the_bound() -> None:
    """The sign question, asked directly.

    A sign error here is the one defect that would turn the clamp inside out: it
    would find capacity on an exporting site and command a discharge into it. So
    rather than trust the formula's comment, the same magnitude is presented as
    import and then as export, and the export reading must never yield the larger
    bound.
    """
    for watts in (100.0, 400.0, 990.0, 2500.0):
        importing = safe_discharge_power_kw(
            make_context(grid_import_w=watts, grid_export_w=0.0, battery_power_w=0.0)
        )
        exporting = safe_discharge_power_kw(
            make_context(grid_import_w=0.0, grid_export_w=watts, battery_power_w=0.0)
        )

        assert importing > 0.0
        assert exporting == pytest.approx(0.0)
        assert exporting < importing


def test_charging_is_never_credited_as_absorption() -> None:
    """A charging battery is creating the load, not absorbing somebody else's.

    Counting it would let a charge command justify a discharge command, which is
    the same sign confusion in a different costume. ``battery_power_w`` is
    positive for charging, and only the negated (discharging) part is credited.
    """
    charging = safe_discharge_power_kw(
        make_context(grid_import_w=1000.0, grid_export_w=0.0, battery_power_w=3000.0)
    )
    idle = safe_discharge_power_kw(
        make_context(grid_import_w=1000.0, grid_export_w=0.0, battery_power_w=0.0)
    )
    discharging = safe_discharge_power_kw(
        make_context(grid_import_w=1000.0, grid_export_w=0.0, battery_power_w=-3000.0)
    )

    assert charging == pytest.approx(idle)
    assert discharging > idle


def test_every_power_in_the_clamp_is_in_kilowatts() -> None:
    """W against kW is a factor of a thousand, and this is a safety bound.

    The meter is the only thing in the path measured in watts, and it is divided
    once inside the authoritative formula. A thousandfold slip would be visible
    here as a bound three orders of magnitude out.
    """
    context = make_context(grid_import_w=990.0, grid_export_w=0.0, battery_power_w=0.0)

    assert absorbing_capacity_kw(context) == pytest.approx(0.99)
    assert safe_discharge_power_kw(context) == pytest.approx(0.891)
    # Sanity bracket: a plausible household bound, not a kilowatt-scale slip.
    assert 0.0 < safe_discharge_power_kw(context) < 100.0


def test_a_bound_a_hair_below_the_request_reduces_conservatively() -> None:
    """Float noise at the boundary must fall the safe way, and it does.

    A bound of 1.0999999999 against a 1.1 kW request drops one step rather than
    rounding back up to 1.1. One step of unnecessary caution is the correct
    direction for a rounding artefact in an export bound.
    """
    intent = make_intent(energy_ac_kwh=1.1 * INTERVAL_HOURS)
    requested = build_command(intent)

    just_below = limit_command(requested, 1.1 - 1e-10)
    just_above = limit_command(requested, 1.1 + 1e-10)

    assert just_below.power_kw <= 1.1
    assert just_above is requested
    assert just_below.power_kw == pytest.approx(1.0)


def test_the_clamp_changed_no_persisted_schema() -> None:
    """``DeviceCommand`` is diagnostics-only, so the new fields persist nowhere.

    Both storage versions are pinned: a safety fix that bumped a schema would be
    a migration nobody asked for. The forecast minor moved to 6 in beta.16 for a
    reason of its own -- additive economic reporting fields in beta.16, and the
    terminal-figure rename in beta.17 -- and the pin is updated rather than
    loosened so this test keeps failing if the *clamp* ever starts persisting
    something.
    """
    import inspect

    from custom_components.alpha_ems_manager import history_store, storage
    from custom_components.alpha_ems_manager.const import (
        FORECAST_STORAGE_MINOR_VERSION,
        STORAGE_MINOR_VERSION,
    )

    # 5 since beta.19: Stage B remembers published revisions and one causal
    # ownership record. Neither is a control figure -- the assertions below still
    # forbid a command, a safety verdict or a power from reaching either store.
    assert STORAGE_MINOR_VERSION == 5
    assert FORECAST_STORAGE_MINOR_VERSION == 7

    for module in (storage, history_store):
        source = inspect.getsource(module)
        assert "DeviceCommand" not in source, module.__name__
        assert "safety_limited" not in source, module.__name__
        assert "requested_power_kw" not in source, module.__name__


def test_the_clamp_reaches_no_activity_entry() -> None:
    """Activity is about the economic plan, and a meter fluctuation is not one.

    The fingerprint that decides whether a logbook line is written reads the
    economic outcome only, so a command reduced by 0.1 kW cannot produce one.
    Asserted from the signature and the source rather than by counting lines,
    because the absence of a coupling is what matters.
    """
    import inspect

    from custom_components.alpha_ems_manager import activity

    # The announcement policy reads planned runs, the clock and -- since
    # beta.19 -- a narrow execution summary. Still nothing from the safety layer:
    # no command, no power, no verdict, so a command reduced by 0.1 kW cannot
    # produce a line.
    assert set(inspect.signature(activity.next_activity).parameters) == {
        "previous",
        "runs",
        "now",
        "execution",
    }

    source = inspect.getsource(activity)
    for forbidden in (
        "safety_limited",
        "limited_power_kw",
        "absorbing_capacity",
        "device_power_kw",
        "export_margin",
    ):
        assert forbidden not in source, forbidden


def test_the_service_caller_set_did_not_grow() -> None:
    """Two modules call a service, and neither of them is the clamp."""
    import ast
    from pathlib import Path

    component = Path("custom_components/alpha_ems_manager")
    callers = set()
    for path in component.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("async_call", "call")
            ):
                callers.add(path.stem)

    assert callers == {"alphaess_adapter", "solcast_source"}


# ===========================================================================
# L. the clamp cannot produce Control State `off`
# ===========================================================================
#
# The transient that held beta.15 back reported ``state == off``. Its root cause
# turned out to be the select's debounced refresh, not the clamp -- but "turned
# out to be" is a story, and a story is not a proof. This section is the proof:
# ``off`` is reachable only from the mode, and the clamp cannot reach the mode.


def test_off_is_decided_before_any_command_exists() -> None:
    """Structural: the ``off`` branch returns before an intent is even built.

    Read from the source, because this is a claim about control flow. In the
    ``off`` branch there is no ``translate``, no ``build_command``, no
    ``limit_command`` and no ``ControlContext`` -- so no clamp outcome can
    influence it, whatever the meter says.
    """
    import ast
    import inspect

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    source = inspect.getsource(AlphaEmsCoordinator._build_control_report)
    tree = ast.parse(inspect.cleandoc(source).replace("def ", "def ", 1))

    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)

    # The first statement after the docstring reads the mode; the branch that
    # follows it returns.
    off_branch = next(
        node
        for node in function.body
        if isinstance(node, ast.If) and "CONTROL_MODE_OFF" in ast.dump(node.test)
    )
    body = ast.dump(ast.Module(body=off_branch.body, type_ignores=[]))

    assert "Return" in body
    for forbidden in ("translate", "build_command", "limit_command", "ControlContext"):
        assert forbidden not in body, forbidden


@pytest.mark.parametrize("import_w", [4000, 990, 400, 222, 60, 0])
async def test_no_absorption_figure_can_produce_off(
    hass, setup_integration, control_surface, import_w: float
) -> None:
    """Behavioural: sweep the clamp across its whole range of outcomes.

    Unlimited, limited and refused are all represented, and none of them yields
    ``off``. The mode is what decides ``off``, and the meter is not the mode.
    """
    report = await drive_control(hass, setup_integration, import_w=import_w)

    assert report["state"] != CONTROL_STATE_OFF
    assert report["mode"] == CONTROL_MODE_SHADOW
    assert report["state"] in (CONTROL_STATE_ELIGIBLE, CONTROL_STATE_INHIBITED)


async def test_no_discharge_request_leaves_the_clamp_untouched(
    hass, setup_integration, control_surface
) -> None:
    """(A) With nothing to discharge, ``limit_command`` returns on its first guard.

    This is the path the flaky test actually took: no seeded history, so the plan
    holds and there is no discharge command for the clamp to consider. The state
    is ``idle`` -- the gate passed and there was nothing to send -- and it is
    emphatically not ``off``.
    """
    from custom_components.alpha_ems_manager.const import CONTROL_STATE_IDLE

    from .test_control_modes import refresh, set_mode

    await set_mode(hass, CONTROL_MODE_SHADOW)
    point_meter(hass, 2000)
    report = await refresh(hass, setup_integration)

    assert report["intent"]["action"] == ACTION_HOLD
    assert report["command"]["power_kw"] == 0.0
    assert report["command"]["safety_limited"] is False
    assert report["export_check"]["safety_limited"] is False
    assert report["state"] == CONTROL_STATE_IDLE
    assert report["state"] != CONTROL_STATE_OFF


async def test_a_clampable_request_reaches_eligible(
    hass, setup_integration, control_surface
) -> None:
    """(B) A discharge that can be safely reduced becomes eligible."""
    report = await drive_control(hass, setup_integration, import_w=400)

    assert report["command"]["safety_limited"] is True
    assert report["state"] == CONTROL_STATE_ELIGIBLE


async def test_an_unclampable_request_reaches_inhibited(
    hass, setup_integration, control_surface
) -> None:
    """(C) A discharge that cannot be reduced usefully becomes inhibited."""
    report = await drive_control(hass, setup_integration, import_w=60)

    assert report["command"]["safety_limited"] is False
    assert report["state"] == CONTROL_STATE_INHIBITED
    assert report["safety"]["inhibit_reason"] == INHIBIT_WOULD_EXPORT


async def test_the_clamp_never_writes_the_mode(
    hass, setup_integration, control_surface
) -> None:
    """(D) The mode survives every clamp outcome, and nothing else can set it.

    ``set_control_mode`` has exactly one caller outside the coordinator -- the
    select entity -- and neither Phase-4 module can reach it. Asserted from the
    source as well as behaviourally, because "the clamp did not happen to change
    the mode" is weaker than "the clamp cannot".
    """
    assert mode_setters() == {"select"}

    for import_w in (2000, 400, 60):
        report = await drive_control(hass, setup_integration, import_w=import_w)
        assert report["mode"] == CONTROL_MODE_SHADOW
        assert setup_integration.runtime_data.control_mode == CONTROL_MODE_SHADOW
