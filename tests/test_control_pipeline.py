"""The control pipeline, exercised without Home Assistant.

Intent translation, the safety gate, the authorization boundary, the vendor
mapping and its quantisation. All four are pure, so everything here runs against
synthetic state and every condition can be driven directly rather than arranged
for.

The invariants worth naming up front, because the rest of the file is in service
of them:

* the gate returns the *same* verdict in shadow as in active;
* authorization can only ever subtract from it;
* a commanded power never delivers more energy than the decision layer allowed;
* a commanded cutoff never lets the device stop below the user's floor;
* an active dispatch is never ours, whatever its parameters look like.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.alphaess_device import (
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    FAMILIES,
    OWNERSHIP_PROVABLE,
    PERMITTED_SERVICES,
    build_command,
    device_cutoff_percent,
    device_duration_minutes,
    device_power_kw,
    plan_commands,
)
from custom_components.alpha_ems_manager.battery import (
    INTERVAL_HOURS,
    BatteryRequest,
    build_limits,
    static_reserve,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_HOLD,
    ACTION_NO_DECISION,
    CONTROL_COOLDOWN_SECONDS,
    CONTROL_CUTOFF_MAX_PERCENT,
    CONTROL_CUTOFF_MIN_PERCENT,
    CONTROL_CUTOFF_PERCENT_PER_BIT,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_HOLD_MONITOR_WINDOW_W,
    CONTROL_INHIBIT_REASONS,
    CONTROL_MAX_POWER_KW,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
    CONTROL_POWER_STEP_KW,
    INHIBIT_AT_OR_BELOW_FLOOR,
    INHIBIT_BATTERY_NOT_CONFIGURED,
    INHIBIT_BATTERY_POWER_STALE,
    INHIBIT_BATTERY_POWER_UNUSABLE,
    INHIBIT_CONTROL_ENTITY_UNAVAILABLE,
    INHIBIT_CUTOFF_OUT_OF_RANGE,
    INHIBIT_DISPATCH_ACTIVE,
    INHIBIT_DURATION_OUT_OF_RANGE,
    INHIBIT_EXCESS_EXPORT_ACTIVE,
    INHIBIT_HOUSE_LOAD_STALE,
    INHIBIT_HOUSE_LOAD_UNUSABLE,
    INHIBIT_MISSING_CONTROL_ENTITY,
    INHIBIT_NO_DECISION,
    INHIBIT_NO_FAILSAFE_AUTOMATION,
    INHIBIT_NO_PLAN,
    INHIBIT_PEAK_SHAVING_ACTIVE,
    INHIBIT_PLAN_UNAVAILABLE,
    INHIBIT_POWER_ABOVE_DEVICE_MAXIMUM,
    INHIBIT_POWER_BELOW_DEVICE_MINIMUM,
    INHIBIT_SOC_STALE,
    INHIBIT_SOC_UNUSABLE,
    INHIBIT_STALE_PLAN_AGE,
    INHIBIT_STALE_PLAN_DAY,
    INHIBIT_STALE_PLAN_INTERVAL,
    INHIBIT_WOULD_EXPORT,
    MAX_CONTROL_HORIZON_MINUTES,
    MIN_CONTROL_HORIZON_MINUTES,
    REFUSE_COOLDOWN,
    REFUSE_EXECUTION_NOT_ENABLED,
    REFUSE_EXECUTION_UNAVAILABLE,
    REFUSE_MODE_NOT_ACTIVE,
    REFUSE_NO_COMMANDS,
    REFUSE_UNSAFE,
)
from custom_components.alpha_ems_manager.control import ControlIntent, translate
from custom_components.alpha_ems_manager.plan import (
    BatteryDecision,
    BatteryPlan,
    build_plan,
)
from custom_components.alpha_ems_manager.policy import HoldPolicy
from custom_components.alpha_ems_manager.safety import (
    ControlContext,
    authorize,
    evaluate,
)

NOW = datetime(2026, 8, 20, 12, 0, 5, tzinfo=UTC)
TODAY = date(2026, 8, 20)
HORIZON = 20

#: The live installation's own figures, so the numbers here mean something.
CAPACITY_KWH = 22.0
MAX_POWER_KW = 10.0
FLOOR_PERCENT = 20.0


def make_intent(
    *,
    action: str = ACTION_DISCHARGE,
    energy_ac_kwh: float = 0.5,
    floor_soc_percent: float = FLOOR_PERCENT,
    energy_limit_bound: bool = False,
    horizon_minutes: int = HORIZON,
    start_index: int = 48,
    built_at: datetime = NOW,
    target_day: date = TODAY,
) -> ControlIntent:
    """Return an intent built directly, for driving the gate."""
    return ControlIntent(
        action=action,
        energy_ac_kwh=energy_ac_kwh,
        average_power_kw=energy_ac_kwh / INTERVAL_HOURS,
        interval_hours=INTERVAL_HOURS,
        floor_soc_percent=floor_soc_percent,
        energy_limit_bound=energy_limit_bound,
        horizon_minutes=horizon_minutes,
        target_day=target_day,
        start_index=start_index,
        built_at=built_at,
        reason="forecast_load_and_available_energy",
        policy="reserve_guard",
        policy_version=1,
    )


def make_context(**overrides: object) -> ControlContext:
    """Return a context in which every condition passes, then apply overrides."""
    intent = overrides.pop("_intent", None)
    command_power = 2.0 if intent is None else 0.0
    defaults: dict[str, object] = {
        "mode": CONTROL_MODE_SHADOW,
        "execution_enabled": False,
        "missing_entities": (),
        "unavailable_entities": (),
        "failsafe_available": True,
        "excess_export_active": False,
        "peak_shaving_active": False,
        "dispatch_active": False,
        "battery_configured": True,
        "plan_problem": None,
        "current_start_index": 48,
        "today": TODAY,
        "now": NOW,
        "soc_percent": 60.0,
        "soc_age_seconds": 5.0,
        "battery_power_w": -1200.0,
        "battery_power_age_seconds": 1.0,
        "house_load_w": 4000.0,
        "house_load_age_seconds": 2.0,
        "max_source_age_seconds": 300.0,
        "device_power_kw": command_power,
        "device_cutoff_percent": 21,
        "device_duration_minutes": HORIZON,
        "export_margin_percent": 10.0,
        "seconds_since_last_write": None,
    }
    defaults.update(overrides)
    return ControlContext(**defaults)  # type: ignore[arg-type]


def make_plan(
    *,
    action: str = ACTION_DISCHARGE,
    energy: float = 0.5,
    constraints: tuple[str, ...] = (),
    unavailable: str | None = None,
    target_day: date | None = TODAY,
    start_index: int | None = 48,
) -> BatteryPlan:
    """Return a plan carrying a chosen decision, without running a policy."""
    limits, error = build_limits(
        capacity_kwh=CAPACITY_KWH,
        max_charge_kw=MAX_POWER_KW,
        max_discharge_kw=MAX_POWER_KW,
        round_trip_efficiency_percent=90.0,
    )
    assert limits is not None and error is None
    from custom_components.alpha_ems_manager.battery import BatteryInputs, build_state

    reserve = static_reserve(FLOOR_PERCENT)
    state = build_state(soc_percent=60.0, limits=limits, reserve=reserve)
    request = (
        BatteryRequest.idle()
        if action == ACTION_HOLD
        else BatteryRequest.discharge(energy / INTERVAL_HOURS)
    )
    decision = BatteryDecision(
        action=action,
        request=request,
        allowed_energy_ac_kwh=energy,
        reason="forecast_load_and_available_energy",
        constraints=constraints,
        policy="reserve_guard",
        policy_version=1,
    )
    return BatteryPlan(
        decision=decision,
        state=state,
        inputs=BatteryInputs(
            soc_percent=60.0,
            capacity_kwh=CAPACITY_KWH,
            max_charge_kw=MAX_POWER_KW,
            max_discharge_kw=MAX_POWER_KW,
            round_trip_efficiency_percent=90.0,
            configured_min_soc_percent=FLOOR_PERCENT,
        ),
        reserve=reserve,
        unavailable_reason=unavailable,
        target_day=target_day,
        start_index=start_index,
    )


def make_clamped_plan() -> BatteryPlan:
    """Return a plan whose clamp genuinely reduced the request.

    Needed because a plan whose request and allowed energy agree cannot
    distinguish "copied the clamped figure" from "recomputed from the request" --
    and the interesting case is exactly the one where the clamp bound.
    """
    from custom_components.alpha_ems_manager.const import CONSTRAINT_MIN_SOC

    plan = make_plan(energy=0.2, constraints=(CONSTRAINT_MIN_SOC,))
    decision = plan.decision
    return BatteryPlan(
        decision=BatteryDecision(
            action=decision.action,
            # Asked for the full discharge limit; allowed a fraction of it.
            request=BatteryRequest.discharge(MAX_POWER_KW),
            allowed_energy_ac_kwh=0.2,
            reason=decision.reason,
            constraints=decision.constraints,
            policy=decision.policy,
            policy_version=decision.policy_version,
        ),
        state=plan.state,
        inputs=plan.inputs,
        reserve=plan.reserve,
        unavailable_reason=None,
        target_day=plan.target_day,
        start_index=plan.start_index,
    )


# ===========================================================================
# 1. intent translation is a projection, not a decision
# ===========================================================================


def test_the_energy_is_copied_and_never_recomputed() -> None:
    """Byte-equal, not approximately equal.

    The allowed energy has already been through the single clamp, so anything
    that recomputed it would be reintroducing the limit it is meant to trust.
    """
    plan = make_plan(energy=0.2115)
    intent = translate(plan, now=NOW, horizon_minutes=HORIZON)

    assert intent is not None
    assert intent.energy_ac_kwh == plan.decision.allowed_energy_ac_kwh


def test_a_clamped_decision_carries_the_clamped_energy_not_the_request() -> None:
    """When the clamp bound, the request and the allowance genuinely differ.

    The intent must carry the allowance. Recomputing from the request would
    reintroduce the limit the clamp exists to apply -- and on this plan the two
    differ by more than a factor of six, so nothing subtle is required to see it.
    """
    plan = make_clamped_plan()
    requested = plan.decision.request.power_kw * INTERVAL_HOURS
    intent = translate(plan, now=NOW, horizon_minutes=HORIZON)

    assert intent is not None
    assert requested == pytest.approx(2.5)
    assert intent.energy_ac_kwh == pytest.approx(0.2)
    assert intent.energy_ac_kwh == plan.decision.allowed_energy_ac_kwh
    assert intent.energy_ac_kwh < requested


def test_the_average_power_is_exactly_the_energy_over_the_interval() -> None:
    """Within the one-ulp bound the decision layer already measured."""
    for milli_kwh in range(0, 3000, 7):
        energy = milli_kwh / 1000.0
        intent = translate(make_plan(energy=energy), now=NOW, horizon_minutes=HORIZON)
        assert intent is not None
        assert intent.average_power_kw * intent.interval_hours == pytest.approx(
            energy, abs=9e-16
        )


def test_a_hold_carries_exactly_zero_energy() -> None:
    """A hold moves nothing, so there is nothing to command."""
    intent = translate(
        make_plan(action=ACTION_HOLD, energy=0.0), now=NOW, horizon_minutes=HORIZON
    )

    assert intent is not None
    assert intent.action == ACTION_HOLD
    assert intent.energy_ac_kwh == 0.0
    assert intent.average_power_kw == 0.0
    assert intent.moves_battery is False


def test_a_declined_decision_produces_no_intent_at_all() -> None:
    """No decision is different from a decision to do nothing."""
    plan = make_plan(action=ACTION_NO_DECISION, energy=0.0)

    assert translate(plan, now=NOW, horizon_minutes=HORIZON) is None


def test_no_plan_produces_no_intent() -> None:
    """``None`` in, ``None`` out; it never raises."""
    assert translate(None, now=NOW, horizon_minutes=HORIZON) is None


def test_a_plan_without_an_interval_identity_produces_no_intent() -> None:
    """A decision that cannot be checked for staleness is not actionable."""
    assert (
        translate(make_plan(start_index=None), now=NOW, horizon_minutes=HORIZON) is None
    )
    assert (
        translate(make_plan(target_day=None), now=NOW, horizon_minutes=HORIZON) is None
    )


@pytest.mark.parametrize("action", [ACTION_HOLD, ACTION_DISCHARGE, ACTION_CHARGE])
def test_translation_never_changes_the_direction(action: str) -> None:
    """The test that makes "projection, not decision" checkable."""
    plan = make_plan(action=action, energy=0.0 if action == ACTION_HOLD else 0.5)
    intent = translate(plan, now=NOW, horizon_minutes=HORIZON)

    assert intent is not None
    assert intent.action == plan.decision.action


def test_the_energy_limit_flag_is_read_off_the_decision() -> None:
    """Not derived here: the constraint set already says it."""
    from custom_components.alpha_ems_manager.const import CONSTRAINT_MIN_SOC

    bound = translate(
        make_plan(constraints=(CONSTRAINT_MIN_SOC,)),
        now=NOW,
        horizon_minutes=HORIZON,
    )
    unbound = translate(make_plan(), now=NOW, horizon_minutes=HORIZON)

    assert bound is not None and bound.energy_limit_bound is True
    assert unbound is not None and unbound.energy_limit_bound is False


def test_the_intent_carries_no_ownership_field() -> None:
    """Ownership is a property of the device, not of an intention."""
    assert not any("own" in field for field in ControlIntent.__dataclass_fields__)


def test_translation_never_raises_on_a_real_plan() -> None:
    """Total, like the decision layer it consumes."""
    plan = build_plan(
        soc_percent=None,
        capacity_kwh=None,
        max_charge_kw=None,
        max_discharge_kw=None,
        round_trip_efficiency_percent=None,
        configured_min_soc_percent=FLOOR_PERCENT,
        today_forecast=None,
        tomorrow_forecast=None,
        elapsed_intervals=0,
        today=TODAY,
        policy=HoldPolicy(),
    )

    assert translate(plan, now=NOW, horizon_minutes=HORIZON) is None


# ===========================================================================
# 2. quantisation always resolves downwards
# ===========================================================================


def test_a_commanded_power_never_over_delivers() -> None:
    """The one promise the power mapping makes, swept exhaustively.

    Five thousand energies, in one watt-hour steps, covering the whole range a
    twenty kilowatt inverter could be asked for.
    """
    violations = []
    for milli_kwh in range(0, 5001):
        energy = milli_kwh / 1000.0
        power = device_power_kw(energy, INTERVAL_HOURS)
        if power * INTERVAL_HOURS > energy:
            violations.append((energy, power))

    assert violations == []


def test_the_commanded_power_is_exact_on_a_whole_step() -> None:
    """Equality where the energy is a whole number of steps, not merely close."""
    step_energy = CONTROL_POWER_STEP_KW * INTERVAL_HOURS
    for steps in range(1, 200):
        energy = steps * step_energy
        assert device_power_kw(energy, INTERVAL_HOURS) * INTERVAL_HOURS == (
            pytest.approx(energy, abs=1e-12)
        )


def test_the_power_is_floored_and_never_rounded_up() -> None:
    """2.97 kW of average power commands 2.9, not 3.0."""
    energy = 2.97 * INTERVAL_HOURS

    assert device_power_kw(energy, INTERVAL_HOURS) == pytest.approx(2.9)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, 0.0), (-1.0, 0.0), (float("nan"), 0.0), (float("inf"), 0.0)],
)
def test_an_unusable_energy_commands_nothing(value: float, expected: float) -> None:
    """Refused rather than clamped, following the model's own convention."""
    assert device_power_kw(value, INTERVAL_HOURS) == expected


def test_a_cutoff_never_lets_the_device_stop_below_the_floor() -> None:
    """Swept over every integer floor, because the claim is arithmetic.

    The device truncates percent to register bits, so asking for exactly the
    floor lands slightly below it. One extra percent compensates, and one is
    enough because the worst-case truncation loss is under a percent.
    """
    for floor in range(0, 100):
        commanded = device_cutoff_percent(float(floor))
        bits = int(commanded / CONTROL_CUTOFF_PERCENT_PER_BIT)
        effective = bits * CONTROL_CUTOFF_PERCENT_PER_BIT
        assert effective >= floor, (floor, commanded, effective)


def test_the_cutoff_stays_inside_the_helper_range() -> None:
    """Including the two ends, where a user may legitimately sit."""
    for floor in (-5.0, 0.0, 4.0, 20.0, 98.0, 99.0, 100.0, 250.0):
        commanded = device_cutoff_percent(floor)
        assert CONTROL_CUTOFF_MIN_PERCENT <= commanded <= CONTROL_CUTOFF_MAX_PERCENT


def test_a_zero_reserve_still_gets_the_device_minimum() -> None:
    """Conservative in the safe direction: it stops higher than asked."""
    assert device_cutoff_percent(0.0) == CONTROL_CUTOFF_MIN_PERCENT


def test_the_duration_snaps_to_the_helper_step() -> None:
    """Always a multiple of the device's own step."""
    for minutes in range(0, 200):
        assert device_duration_minutes(minutes) % 5 == 0


def test_the_default_horizon_survives_snapping() -> None:
    """Twenty minutes is already on the grid, so it passes through."""
    assert device_duration_minutes(HORIZON) == HORIZON


def test_the_device_minimum_clears_the_teardown_window() -> None:
    """Why the minimum is two steps rather than one.

    The control surface reads a battery inside a fifty watt band as having
    finished and tears the dispatch down. One step is only twice that; a dispatch
    tracks the commanded power rather than matching it, so a single-step command
    could land inside the band while behaving correctly.
    """
    assert pytest.approx(2 * CONTROL_POWER_STEP_KW) == CONTROL_MIN_POWER_KW
    assert CONTROL_MIN_POWER_KW * 1000.0 >= 4 * CONTROL_HOLD_MONITOR_WINDOW_W


# ===========================================================================
# 3. the mapping: right family, right order, nothing else
# ===========================================================================


def test_a_discharge_maps_only_to_the_battery_discharge_family() -> None:
    """Never to a grid-rate actuator, which would be wrong by the house load."""
    command = build_command(make_intent(action=ACTION_DISCHARGE, energy_ac_kwh=0.5))
    steps = plan_commands(command)

    assert [step.entity_id for step in steps] == [
        DISCHARGE_FAMILY.power,
        DISCHARGE_FAMILY.cutoff_soc,
        DISCHARGE_FAMILY.duration,
        DISCHARGE_FAMILY.hold,
        DISCHARGE_FAMILY.activate,
    ]


def test_a_charge_maps_only_to_the_battery_charge_family() -> None:
    """Built and tested, and reached by no policy that ships."""
    command = build_command(make_intent(action=ACTION_CHARGE, energy_ac_kwh=0.5))
    steps = plan_commands(command)

    assert [step.entity_id for step in steps] == [
        CHARGE_FAMILY.power,
        CHARGE_FAMILY.cutoff_soc,
        CHARGE_FAMILY.duration,
        CHARGE_FAMILY.hold,
        CHARGE_FAMILY.activate,
    ]


def test_no_shipped_policy_ever_asks_for_a_charge() -> None:
    """Asserted over the real policy list, not over an assumption."""
    from custom_components.alpha_ems_manager.policy import SHIPPED_POLICIES

    for policy in SHIPPED_POLICIES:
        source = policy.__doc__ or ""
        assert "BatteryRequest.charge(" not in source


def test_the_activation_boolean_is_always_last() -> None:
    """So an interrupted sequence leaves inert numbers, not a half command."""
    for action in (ACTION_DISCHARGE, ACTION_CHARGE):
        steps = plan_commands(
            build_command(make_intent(action=action, energy_ac_kwh=0.5))
        )
        assert steps[-1].entity_id == FAMILIES[action].activate
        assert steps[-1].service == "turn_on"
        for step in steps[:-1]:
            assert step.entity_id != FAMILIES[action].activate


def test_the_device_hold_flag_is_always_left_off() -> None:
    """Left off so the device tears its own dispatch down at the cutoff.

    Named for the device concept it is. The decision layer's own hold means the
    opposite -- do not move the battery -- and letting the two words meet would
    be a real hazard rather than a stylistic one.
    """
    for action in (ACTION_DISCHARGE, ACTION_CHARGE):
        command = build_command(make_intent(action=action, energy_ac_kwh=0.5))
        assert command.device_hold_flag is False
        hold_step = next(
            step
            for step in plan_commands(command)
            if step.entity_id == FAMILIES[action].hold
        )
        assert hold_step.service == "turn_off"


def test_a_hold_plans_nothing_whatever_the_device_is_doing() -> None:
    """Including a dispatch whose parameters match exactly what we would send.

    This is the shape of the mistake being avoided: a person watching the shadow
    recommendation is precisely who would arm those figures by hand, so a
    parameter match is at its most convincing when it is most wrong.
    """
    command = build_command(make_intent(action=ACTION_HOLD, energy_ac_kwh=0.0))

    assert command.moves_battery is False
    assert plan_commands(command) == ()


def test_every_planned_step_uses_a_permitted_service() -> None:
    """Swept over both directions and a wide power range."""
    for action in (ACTION_DISCHARGE, ACTION_CHARGE):
        for milli_kwh in range(50, 2500, 37):
            command = build_command(
                make_intent(action=action, energy_ac_kwh=milli_kwh / 1000.0)
            )
            for step in plan_commands(command):
                assert (step.domain, step.service) in PERMITTED_SERVICES


def test_the_commanded_energy_never_exceeds_the_allowed_energy() -> None:
    """The command reports its own shortfall, and it is never negative."""
    for milli_kwh in range(0, 2500, 13):
        energy = milli_kwh / 1000.0
        command = build_command(make_intent(energy_ac_kwh=energy))
        assert command.commanded_energy_ac_kwh <= command.allowed_energy_ac_kwh
        assert command.undelivered_energy_ac_kwh >= 0.0


def test_the_undelivered_energy_is_at_most_one_step() -> None:
    """The whole cost of quantising, bounded."""
    step_energy = CONTROL_POWER_STEP_KW * INTERVAL_HOURS
    for milli_kwh in range(0, 2500, 7):
        command = build_command(make_intent(energy_ac_kwh=milli_kwh / 1000.0))
        if command.moves_battery:
            assert command.undelivered_energy_ac_kwh < step_energy


def test_ownership_is_not_provable_and_is_stated_as_such() -> None:
    """A named fact rather than an assumption buried in a branch."""
    assert OWNERSHIP_PROVABLE is False


# ===========================================================================
# 4. the safety gate
# ===========================================================================


def test_a_fully_healthy_context_is_safe() -> None:
    """The baseline the negative cases are measured against."""
    intent = make_intent(energy_ac_kwh=0.5)
    command = build_command(intent)
    verdict = evaluate(intent, make_context(device_power_kw=command.power_kw))

    assert verdict.safe is True
    assert verdict.inhibit_reason is None
    assert verdict.checks_evaluated == verdict.checks_passed


GATE_CASES: tuple[tuple[str, dict[str, object]], ...] = (
    (INHIBIT_MISSING_CONTROL_ENTITY, {"missing_entities": ("sensor.gone",)}),
    (
        INHIBIT_CONTROL_ENTITY_UNAVAILABLE,
        {"unavailable_entities": ("sensor.dead",)},
    ),
    (INHIBIT_NO_FAILSAFE_AUTOMATION, {"failsafe_available": False}),
    (INHIBIT_EXCESS_EXPORT_ACTIVE, {"excess_export_active": True}),
    (INHIBIT_PEAK_SHAVING_ACTIVE, {"peak_shaving_active": True}),
    (INHIBIT_DISPATCH_ACTIVE, {"dispatch_active": True}),
    (INHIBIT_BATTERY_NOT_CONFIGURED, {"battery_configured": False}),
    (INHIBIT_NO_PLAN, {"plan_problem": INHIBIT_NO_PLAN}),
    (INHIBIT_PLAN_UNAVAILABLE, {"plan_problem": INHIBIT_PLAN_UNAVAILABLE}),
    (INHIBIT_NO_DECISION, {"plan_problem": INHIBIT_NO_DECISION}),
    (INHIBIT_STALE_PLAN_DAY, {"today": date(2026, 8, 21)}),
    (INHIBIT_STALE_PLAN_INTERVAL, {"current_start_index": 49}),
    (INHIBIT_STALE_PLAN_AGE, {"now": NOW + timedelta(minutes=16)}),
    (INHIBIT_SOC_UNUSABLE, {"soc_percent": None}),
    (INHIBIT_SOC_STALE, {"soc_age_seconds": 301.0}),
    (INHIBIT_BATTERY_POWER_UNUSABLE, {"battery_power_w": None}),
    (INHIBIT_BATTERY_POWER_STALE, {"battery_power_age_seconds": 301.0}),
    (INHIBIT_HOUSE_LOAD_UNUSABLE, {"house_load_w": None}),
    (INHIBIT_HOUSE_LOAD_STALE, {"house_load_age_seconds": 301.0}),
    (INHIBIT_AT_OR_BELOW_FLOOR, {"soc_percent": FLOOR_PERCENT}),
    (INHIBIT_POWER_BELOW_DEVICE_MINIMUM, {"device_power_kw": 0.1}),
    (
        INHIBIT_POWER_ABOVE_DEVICE_MAXIMUM,
        {"device_power_kw": CONTROL_MAX_POWER_KW + 0.1, "house_load_w": 5.0e7},
    ),
    (INHIBIT_CUTOFF_OUT_OF_RANGE, {"device_cutoff_percent": 1}),
    (INHIBIT_DURATION_OUT_OF_RANGE, {"device_duration_minutes": 5}),
    (INHIBIT_WOULD_EXPORT, {"house_load_w": 1000.0, "device_power_kw": 2.0}),
)


@pytest.mark.parametrize(
    ("reason", "overrides"), GATE_CASES, ids=[case[0] for case in GATE_CASES]
)
def test_each_condition_inhibits_with_its_own_reason(
    reason: str, overrides: dict[str, object]
) -> None:
    """Every condition, violated one at a time, reporting exactly one cause."""
    verdict = evaluate(make_intent(energy_ac_kwh=0.5), make_context(**overrides))

    assert verdict.safe is False
    assert verdict.inhibit_reason == reason


def test_every_gate_case_is_covered() -> None:
    """The parametrised table covers the whole documented vocabulary.

    Without this, adding a reason and forgetting to test it would pass silently.
    """
    covered = {reason for reason, _ in GATE_CASES}

    assert covered == set(CONTROL_INHIBIT_REASONS)


def test_only_the_first_failing_condition_is_reported() -> None:
    """Ordered so the most informative cause wins.

    A missing helper is a better answer than a stale reading taken through it.
    """
    verdict = evaluate(
        make_intent(),
        make_context(
            missing_entities=("sensor.gone",),
            failsafe_available=False,
            soc_percent=None,
            dispatch_active=True,
        ),
    )

    assert verdict.inhibit_reason == INHIBIT_MISSING_CONTROL_ENTITY


def test_the_gate_never_returns_a_reduced_command() -> None:
    """A gate that scaled a request would have made a decision of its own.

    The verdict carries no magnitude at all, which is the structural way to say
    it: there is nothing on it that a caller could mistake for a smaller command.
    """
    verdict = evaluate(
        make_intent(energy_ac_kwh=2.0),
        make_context(device_power_kw=8.0, house_load_w=1000.0),
    )

    assert verdict.safe is False
    assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT
    assert not any(
        "power" in field or "energy" in field
        for field in type(verdict).__dataclass_fields__
    )


def test_the_export_margin_is_applied() -> None:
    """Ten percent below a four kilowatt load leaves 3.6 kW of headroom."""
    safe = evaluate(
        make_intent(),
        make_context(
            house_load_w=4000.0, device_power_kw=3.6, export_margin_percent=10.0
        ),
    )
    unsafe = evaluate(
        make_intent(),
        make_context(
            house_load_w=4000.0, device_power_kw=3.7, export_margin_percent=10.0
        ),
    )

    assert safe.safe is True
    assert unsafe.inhibit_reason == INHIBIT_WOULD_EXPORT


def test_a_hold_needs_no_house_load_and_no_device_range() -> None:
    """Everything below the hold short-circuit constrains a moving battery.

    Applying it to a hold would inhibit doing nothing because a sensor was
    briefly quiet -- and would make the "gate passed, nothing to send" state
    unreachable.
    """
    verdict = evaluate(
        make_intent(action=ACTION_HOLD, energy_ac_kwh=0.0),
        make_context(
            house_load_w=None,
            house_load_age_seconds=9999.0,
            device_power_kw=0.0,
            device_cutoff_percent=0,
            device_duration_minutes=0,
        ),
    )

    assert verdict.safe is True


def test_a_charge_is_never_refused_for_exporting() -> None:
    """A charge imports; it cannot push energy out."""
    verdict = evaluate(
        make_intent(action=ACTION_CHARGE, energy_ac_kwh=2.0),
        make_context(device_power_kw=8.0, house_load_w=100.0),
    )

    assert verdict.safe is True


@pytest.mark.parametrize(
    "mode", [CONTROL_MODE_OFF, CONTROL_MODE_SHADOW, CONTROL_MODE_ACTIVE]
)
def test_the_gate_gives_the_same_verdict_in_every_mode(mode: str) -> None:
    """The test the earlier design could not have passed.

    Shadow is only worth watching if its verdict is the real one, which means no
    gate condition may depend on the mode. Driven over the whole condition table
    rather than over one happy case.
    """
    intent = make_intent(energy_ac_kwh=0.5)
    reference = evaluate(intent, make_context(mode=CONTROL_MODE_SHADOW))
    assert evaluate(intent, make_context(mode=mode)) == reference

    for _, overrides in GATE_CASES:
        expected = evaluate(intent, make_context(mode=CONTROL_MODE_SHADOW, **overrides))
        actual = evaluate(intent, make_context(mode=mode, **overrides))
        assert actual == expected, overrides


def test_a_missing_intent_inhibits_rather_than_raising() -> None:
    """Total, like everything else on the refresh path."""
    verdict = evaluate(None, make_context())

    assert verdict.safe is False
    assert verdict.inhibit_reason == INHIBIT_NO_PLAN


# ===========================================================================
# 5. the authorization boundary
# ===========================================================================


def _safe_verdict() -> object:
    return evaluate(make_intent(energy_ac_kwh=0.5), make_context())


def test_an_unsafe_verdict_is_never_authorized() -> None:
    """And it carries the gate's own reason, so nothing is lost."""
    verdict = evaluate(make_intent(), make_context(soc_percent=None))
    decision = authorize(
        verdict, make_context(), commands_planned=5, starts_or_increases=True
    )

    assert decision.authorized is False
    assert decision.refusal == REFUSE_UNSAFE
    assert decision.unsafe_reason == INHIBIT_SOC_UNUSABLE


def test_shadow_is_refused_for_the_mode_and_nothing_worse() -> None:
    """Shadow reaches the last stage with a clean verdict, and stops there."""
    decision = authorize(
        _safe_verdict(),
        make_context(mode=CONTROL_MODE_SHADOW),
        commands_planned=5,
        starts_or_increases=True,
    )

    assert decision.refusal == REFUSE_MODE_NOT_ACTIVE


def test_active_without_the_enable_is_refused() -> None:
    """The deliberate switch, which lives in configuration rather than runtime."""
    decision = authorize(
        _safe_verdict(),
        make_context(mode=CONTROL_MODE_ACTIVE, execution_enabled=False),
        commands_planned=5,
        starts_or_increases=True,
    )

    assert decision.refusal == REFUSE_EXECUTION_NOT_ENABLED


def test_active_with_the_enable_is_still_refused_by_the_release_barrier() -> None:
    """The last line, and in this release the one that always holds."""
    decision = authorize(
        _safe_verdict(),
        make_context(mode=CONTROL_MODE_ACTIVE, execution_enabled=True),
        commands_planned=5,
        starts_or_increases=True,
    )

    assert CONTROL_EXECUTION_AVAILABLE is False
    assert decision.authorized is False
    assert decision.refusal == REFUSE_EXECUTION_UNAVAILABLE


def test_authorization_can_only_subtract() -> None:
    """Splitting the gate from the permission admits nothing new.

    Driven over the full cross-product of the condition table and every mode and
    enable combination: an authorized decision always rests on a safe verdict.
    """
    intent = make_intent(energy_ac_kwh=0.5)
    for _, overrides in ((None, {}), *GATE_CASES):
        for mode in (CONTROL_MODE_OFF, CONTROL_MODE_SHADOW, CONTROL_MODE_ACTIVE):
            for enabled in (False, True):
                context = make_context(
                    mode=mode, execution_enabled=enabled, **overrides
                )
                verdict = evaluate(intent, context)
                for planned in (0, 5):
                    for rising in (False, True):
                        decision = authorize(
                            verdict,
                            context,
                            commands_planned=planned,
                            starts_or_increases=rising,
                        )
                        if decision.authorized:
                            assert verdict.safe, overrides


def test_nothing_is_ever_authorized_in_this_release() -> None:
    """The dynamic half of the release-barrier proof."""
    intent = make_intent(energy_ac_kwh=0.5)
    for mode in (CONTROL_MODE_OFF, CONTROL_MODE_SHADOW, CONTROL_MODE_ACTIVE):
        for enabled in (False, True):
            context = make_context(mode=mode, execution_enabled=enabled)
            decision = authorize(
                evaluate(intent, context),
                context,
                commands_planned=5,
                starts_or_increases=True,
            )
            assert decision.authorized is False


def test_an_empty_command_list_is_a_no_op_rather_than_a_hazard() -> None:
    """Reported by the permission stage, not by the gate.

    A hold plans nothing, and that is a perfectly safe outcome -- so conflating
    it with a refusal would make the two unreadable in diagnostics.
    """
    intent = make_intent(action=ACTION_HOLD, energy_ac_kwh=0.0)
    context = make_context(mode=CONTROL_MODE_ACTIVE, execution_enabled=True)
    verdict = evaluate(intent, context)

    assert verdict.safe is True

    decision = authorize(
        verdict, context, commands_planned=0, starts_or_increases=False
    )
    # The release barrier is reached first, which is itself the point: even the
    # no-op path cannot get past it.
    assert decision.refusal == REFUSE_EXECUTION_UNAVAILABLE


def test_no_commands_is_reachable_when_the_barrier_is_lifted() -> None:
    """Proven against the ordering directly, since the barrier hides it.

    Kept because a later release will lift the barrier, and the refusal after it
    must already be correct.
    """
    import custom_components.alpha_ems_manager.safety as safety_module

    context = make_context(mode=CONTROL_MODE_ACTIVE, execution_enabled=True)
    verdict = _safe_verdict()

    original = safety_module.CONTROL_EXECUTION_AVAILABLE
    safety_module.CONTROL_EXECUTION_AVAILABLE = True
    try:
        assert (
            authorize(
                verdict, context, commands_planned=0, starts_or_increases=False
            ).refusal
            == REFUSE_NO_COMMANDS
        )
        assert (
            authorize(
                verdict,
                context,
                commands_planned=5,
                starts_or_increases=True,
            ).authorized
            is True
        )
    finally:
        safety_module.CONTROL_EXECUTION_AVAILABLE = original


def test_a_cooldown_holds_a_rising_command_but_never_a_falling_one() -> None:
    """Reducing battery movement can only reduce risk, so it is never delayed."""
    import custom_components.alpha_ems_manager.safety as safety_module

    verdict = _safe_verdict()
    recent = make_context(
        mode=CONTROL_MODE_ACTIVE,
        execution_enabled=True,
        seconds_since_last_write=CONTROL_COOLDOWN_SECONDS - 1,
    )

    original = safety_module.CONTROL_EXECUTION_AVAILABLE
    safety_module.CONTROL_EXECUTION_AVAILABLE = True
    try:
        assert (
            authorize(
                verdict, recent, commands_planned=5, starts_or_increases=True
            ).refusal
            == REFUSE_COOLDOWN
        )
        assert (
            authorize(
                verdict, recent, commands_planned=5, starts_or_increases=False
            ).authorized
            is True
        )
    finally:
        safety_module.CONTROL_EXECUTION_AVAILABLE = original


def test_no_previous_write_is_not_a_cooldown() -> None:
    """Nothing to cool down from is not the same as a write zero seconds ago."""
    import custom_components.alpha_ems_manager.safety as safety_module

    context = make_context(
        mode=CONTROL_MODE_ACTIVE,
        execution_enabled=True,
        seconds_since_last_write=None,
    )
    original = safety_module.CONTROL_EXECUTION_AVAILABLE
    safety_module.CONTROL_EXECUTION_AVAILABLE = True
    try:
        assert (
            authorize(
                _safe_verdict(),
                context,
                commands_planned=5,
                starts_or_increases=True,
            ).authorized
            is True
        )
    finally:
        safety_module.CONTROL_EXECUTION_AVAILABLE = original


# ===========================================================================
# 6. the horizon contract
# ===========================================================================


def test_a_duration_shorter_than_one_interval_is_refused() -> None:
    """It would lapse before the next refresh could renew it.

    Guarded twice on purpose: the options selector cannot express one, and the
    gate refuses one anyway. A single guard at either layer alone would be a
    guard with a hole in it.
    """
    assert MIN_CONTROL_HORIZON_MINUTES > INTERVAL_HOURS * 60

    for minutes in range(0, MIN_CONTROL_HORIZON_MINUTES, 5):
        verdict = evaluate(make_intent(), make_context(device_duration_minutes=minutes))
        assert verdict.inhibit_reason == INHIBIT_DURATION_OUT_OF_RANGE


def test_the_accepted_horizon_range_is_all_usable() -> None:
    """Every value the form can produce passes the gate."""
    for minutes in range(
        MIN_CONTROL_HORIZON_MINUTES, MAX_CONTROL_HORIZON_MINUTES + 1, 5
    ):
        verdict = evaluate(make_intent(), make_context(device_duration_minutes=minutes))
        assert verdict.safe is True, minutes


def test_the_horizon_is_documented_as_a_dead_man_margin() -> None:
    """Not a delivery window, and the intent says so where a reader will see it."""
    intent = make_intent()

    assert "dead-man" in intent.as_dict()["horizon_basis"]
    assert "not an instantaneous" in intent.as_dict()["average_power_basis"]


# ===========================================================================
# 7. the instrumentation that replaced the discarded balance gate
# ===========================================================================


def test_no_balance_figure_reaches_the_gate() -> None:
    """The residual is diagnostics, not a write gate.

    On an installation whose house-load figure comes from one grid meter while
    the balance check reads another, that residual reduces to the difference
    between the two meters: the battery term cancels identically and the state of
    charge never appears. So its magnitude is not evidence about the readings
    this gate depends on, however large it grows.
    """
    fields = set(ControlContext.__dataclass_fields__)

    assert not any("residual" in field for field in fields)
    assert not any("balance" in field for field in fields)
    assert not any("gross" in field for field in fields)


def test_the_soc_coherence_instrument_agrees_with_a_real_discharge() -> None:
    """Falling state of charge with negative power is coherent."""
    from custom_components.alpha_ems_manager.soc_coherence import (
        COHERENCE_AGREE,
        SocCoherenceMonitor,
    )

    monitor = SocCoherenceMonitor()
    sample = monitor.observe(
        index=40,
        soc_before_percent=60.0,
        soc_after_percent=57.0,
        battery_power_w=-2500.0,
        capacity_kwh=CAPACITY_KWH,
        interval_hours=INTERVAL_HOURS,
    )

    assert sample is not None
    assert sample.verdict == COHERENCE_AGREE
    assert monitor.agree == 1


def test_a_stuck_state_of_charge_is_reported_as_a_disagreement() -> None:
    """One side moving while the other does not is exactly the shape to catch."""
    from custom_components.alpha_ems_manager.soc_coherence import (
        COHERENCE_DISAGREE,
        SocCoherenceMonitor,
    )

    monitor = SocCoherenceMonitor()
    sample = monitor.observe(
        index=40,
        soc_before_percent=60.0,
        soc_after_percent=60.0,
        battery_power_w=-4000.0,
        capacity_kwh=CAPACITY_KWH,
        interval_hours=INTERVAL_HOURS,
    )

    assert sample is not None
    assert sample.verdict == COHERENCE_DISAGREE
    assert monitor.disagree == 1


def test_a_quiet_interval_is_inconclusive_rather_than_wrong() -> None:
    """Below the sensor's own resolution there is nothing to conclude."""
    from custom_components.alpha_ems_manager.soc_coherence import (
        COHERENCE_INCONCLUSIVE,
        SocCoherenceMonitor,
    )

    monitor = SocCoherenceMonitor()
    sample = monitor.observe(
        index=40,
        soc_before_percent=60.0,
        soc_after_percent=60.0,
        battery_power_w=10.0,
        capacity_kwh=CAPACITY_KWH,
        interval_hours=INTERVAL_HOURS,
    )

    assert sample is not None
    assert sample.verdict == COHERENCE_INCONCLUSIVE
    assert monitor.agreement_rate is None


def test_the_instrument_measures_the_sensor_resolution_it_sees() -> None:
    """The figure that decides whether this could ever carry a veto."""
    from custom_components.alpha_ems_manager.soc_coherence import (
        SocCoherenceMonitor,
    )

    monitor = SocCoherenceMonitor()
    for before, after in ((60.0, 59.9), (59.9, 59.5), (59.5, 59.4)):
        monitor.observe(
            index=40,
            soc_before_percent=before,
            soc_after_percent=after,
            battery_power_w=-500.0,
            capacity_kwh=CAPACITY_KWH,
            interval_hours=INTERVAL_HOURS,
        )

    assert monitor.observed_step_percent == pytest.approx(0.1, abs=1e-9)


def test_the_instrument_keeps_a_bounded_trail() -> None:
    """Diagnostics caps every list at sixteen, and this is no exception."""
    from custom_components.alpha_ems_manager.soc_coherence import (
        SocCoherenceMonitor,
    )

    monitor = SocCoherenceMonitor()
    for index in range(40):
        monitor.observe(
            index=index,
            soc_before_percent=60.0,
            soc_after_percent=59.0,
            battery_power_w=-2000.0,
            capacity_kwh=CAPACITY_KWH,
            interval_hours=INTERVAL_HOURS,
        )

    assert len(monitor.recent) <= 16
    assert len(monitor.as_dict()["recent"]) <= 16


def test_the_instrument_declines_when_it_cannot_compare() -> None:
    """No capacity means no energy, which is not an inconclusive comparison."""
    from custom_components.alpha_ems_manager.soc_coherence import (
        SocCoherenceMonitor,
    )

    monitor = SocCoherenceMonitor()

    assert (
        monitor.observe(
            index=1,
            soc_before_percent=60.0,
            soc_after_percent=59.0,
            battery_power_w=-2000.0,
            capacity_kwh=0.0,
            interval_hours=INTERVAL_HOURS,
        )
        is None
    )
    assert monitor.agree == monitor.disagree == monitor.inconclusive == 0


def test_no_reported_figure_is_a_nan() -> None:
    """Every diagnostics number is finite, whatever the inputs were."""
    from custom_components.alpha_ems_manager.soc_coherence import (
        SocCoherenceMonitor,
    )

    monitor = SocCoherenceMonitor()
    monitor.observe(
        index=1,
        soc_before_percent=60.0,
        soc_after_percent=59.0,
        battery_power_w=-2000.0,
        capacity_kwh=CAPACITY_KWH,
        interval_hours=INTERVAL_HOURS,
    )
    payload = monitor.as_dict()
    for sample in payload["recent"]:
        for key, value in sample.items():
            if isinstance(value, float):
                assert math.isfinite(value), key
