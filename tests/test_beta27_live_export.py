"""beta.27: Live net export -- the meter is the target, and the battery a ceiling.

Three things are proved here, and they are separate claims:

1. **the arithmetic** -- the meter target is realised, which means the house load is
   supplied *in addition* to the export and production reduces the discharge
   required. Commanding the planned export magnitude as battery power is the
   likeliest implementation error in this release, and it under-exports by exactly
   the house load;
2. **the authorisation** -- an export reaches the actuator only through
   ``authorize_export``, whose checklist is enumerated below, one refusal per test.
   ``evaluate`` is untouched and still refuses the reserve-guard discharge;
3. **the surface** -- the only actuator is Dispatch Mode 2 with positive signed
   power. The Force Discharging helper family is never written, which is the trap
   the ``net_export -> ACTION_DISCHARGE`` mapping created.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.alphaess_adapter import (
    steps_outside_capability,
)
from custom_components.alpha_ems_manager.alphaess_device import (
    DISCHARGE_FAMILY,
    DISPATCH_MODE_SOC_CONTROL,
    DISPATCH_POWER,
    CommandStep,
    dispatch_refusal,
    plan_dispatch_arm,
    plan_dispatch_power,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_EXECUTABLE_DISPATCH_SIGNS,
    CONTROL_LIVE_DISPATCH_INTENTS,
    CONTROL_REFUSE_DISPATCH_SIGN,
    DISPATCH_EXPORT_CLAMP_ORDER,
    DISPATCH_LIMIT_DYNAMIC_RESERVE,
    DISPATCH_LIMIT_MAX_DISCHARGE,
    DISPATCH_LIMIT_NONE,
    DISPATCH_LIMIT_REMAINING_DISCHARGE,
    DISPATCH_LIMIT_REMAINING_EXPORT,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXPORT_AUTHORISATION_ORDER,
    EXPORT_REFUSE_CONFLICTING_FEATURE,
    EXPORT_REFUSE_DISPATCH_FOREIGN,
    EXPORT_REFUSE_INCOHERENT,
    EXPORT_REFUSE_INVERTER_LIMIT,
    EXPORT_REFUSE_MIN_SOC,
    EXPORT_REFUSE_MISSING_ENTITY,
    EXPORT_REFUSE_NO_BATTERY_ALLOWANCE,
    EXPORT_REFUSE_NO_EXPORT_TARGET,
    EXPORT_REFUSE_NO_FAILSAFE,
    EXPORT_REFUSE_NO_QUARTER,
    EXPORT_REFUSE_NOT_EXPORT_INTENT,
    EXPORT_REFUSE_NOT_OWNED,
    EXPORT_REFUSE_QUARTER_NOT_OPEN,
    EXPORT_REFUSE_RECORD_MISMATCH,
    EXPORT_REFUSE_RESERVE_FLOOR,
    EXPORT_REFUSE_SOC_UNUSABLE,
    EXPORT_REFUSE_TICK_HORIZON,
)
from custom_components.alpha_ems_manager.dispatch import (
    QuarterProgress,
    battery_rate_to_export_kw,
    decide_export,
    export_rate_to_battery_kw,
    permitted_sign,
    sign_matches_intent,
    tick_energy_cap_kw,
)
from custom_components.alpha_ems_manager.safety import ExportRequest, authorize_export

QUARTER_SECONDS = 15 * 60


def export(
    *,
    house_kw: float,
    pv_kw: float,
    export_kwh: float,
    battery_kwh: float = 100.0,
    seconds: float = QUARTER_SECONDS,
    max_discharge_kw: float | None = None,
    reserve_headroom_kwh: float | None = None,
    grid_export_limit_kw: float | None = None,
    last_kw: float | None = None,
):
    """Return the real export decision for one instant inside a quarter."""
    return decide_export(
        progress=QuarterProgress(
            seconds_remaining=seconds,
            battery_remaining_kwh=battery_kwh,
            grid_remaining_kwh=export_kwh,
        ),
        house_load_kw=house_kw,
        pv_kw=pv_kw,
        max_discharge_kw=max_discharge_kw,
        reserve_headroom_kwh=reserve_headroom_kwh,
        grid_export_limit_kw=grid_export_limit_kw,
        last_applied_kw=last_kw,
    )


def authorised(**overrides) -> ExportRequest:
    """Return a request that passes every condition, for one field to be spoiled."""
    fields = {
        "intent": EXECUTION_INTENT_NET_EXPORT,
        "quarter_admitted": True,
        "quarter_open": True,
        "owned": True,
        "causation_proven": True,
        "foreign_dispatch": False,
        "coherent": True,
        "conflicting_feature": False,
        "missing_entities": (),
        "failsafe_available": True,
        "soc_percent": 60.0,
        "configured_min_soc_percent": 20.0,
        "reserve_headroom_kwh": 4.0,
        "battery_remaining_kwh": 1.0,
        "grid_export_remaining_kwh": 0.8,
        "requested_kw": 2.2,
        "inverter_max_discharge_kw": 5.0,
        "site_export_limit_kw": None,
        "tick_cap_kw": 40.0,
    }
    fields.update(overrides)
    return ExportRequest(**fields)


# == 1. the meter is the target, not the battery ============================


@pytest.mark.parametrize(
    ("house_kw", "pv_kw", "export_kw", "expected_battery_kw"),
    [
        # The two measured cases from the plan, which are the ones a reader checks.
        (0.9, 0.0, 1.3, 2.2),
        (1.0, 1.0, 2.0, 2.0),
        # House alone: the battery supplies the house *and* the export.
        (0.0, 0.0, 1.0, 1.0),
        (2.0, 0.0, 1.0, 3.0),
        (0.5, 0.0, 0.0, 0.5),
        # Production alone: it reduces the discharge one-for-one.
        (0.0, 1.0, 1.0, 0.0),
        (0.0, 2.0, 1.0, -1.0),
        (0.0, 0.5, 2.0, 1.5),
        # Both, in every combination of which dominates.
        (1.0, 0.5, 1.0, 1.5),
        (0.5, 1.0, 1.0, 0.5),
        (3.0, 3.0, 2.5, 2.5),
        (4.0, 1.0, 0.5, 3.5),
        (1.0, 4.0, 0.5, -2.5),
        # Larger figures, so a sign slip cannot hide inside a small number.
        (0.0, 0.0, 5.0, 5.0),
        (2.5, 7.5, 3.0, -2.0),
        (7.5, 2.5, 3.0, 8.0),
    ],
)
def test_the_conversion_follows_the_canonical_identity(
    house_kw: float, pv_kw: float, export_kw: float, expected_battery_kw: float
) -> None:
    """``dispatch = house - pv + export``, and nothing else.

    The identity is ``grid = house - pv - dispatch`` with ``grid < 0`` on export, so
    an intended export of ``E`` needs ``house - pv + E`` at the battery. Sixteen
    cases because the two error modes -- forgetting the house term and getting the
    production sign backwards -- each pass some subset of a smaller matrix.
    """
    assert export_rate_to_battery_kw(
        house_load_kw=house_kw, pv_kw=pv_kw, export_kw=export_kw
    ) == pytest.approx(expected_battery_kw)


@pytest.mark.parametrize(
    ("house_kw", "pv_kw", "battery_kw"),
    [
        (0.9, 0.0, 2.2),
        (1.0, 1.0, 2.0),
        (0.0, 0.0, 1.0),
        (2.0, 0.0, 3.0),
        (3.0, 3.0, 2.5),
    ],
)
def test_the_two_conversions_are_inverses(
    house_kw: float, pv_kw: float, battery_kw: float
) -> None:
    """A battery-side ceiling can be expressed as the export it permits, and back.

    The property that makes it safe to have both: clamps 4 and 5 live in different
    domains and are **converted**, never compared, so a round trip must be exact.
    """
    as_export = battery_rate_to_export_kw(
        house_load_kw=house_kw, pv_kw=pv_kw, battery_kw=battery_kw
    )
    back = export_rate_to_battery_kw(
        house_load_kw=house_kw, pv_kw=pv_kw, export_kw=as_export
    )

    assert back == pytest.approx(battery_kw)


def test_commanding_the_export_magnitude_directly_would_under_export() -> None:
    """The error this whole conversion exists to prevent, with its cost.

    1.3 kW of intended export beneath 0.9 kW of house load needs 2.2 kW of battery.
    Commanding 1.3 kW instead delivers 0.4 kW at the meter -- under-exporting by
    exactly the house load, which is the tell.
    """
    decision = export(house_kw=0.9, pv_kw=0.0, export_kwh=1.3 * 0.25, seconds=900.0)

    assert decision.applied_kw == pytest.approx(2.2, abs=0.05)

    naive_delivered = battery_rate_to_export_kw(
        house_load_kw=0.9, pv_kw=0.0, battery_kw=1.3
    )
    assert naive_delivered == pytest.approx(0.4)
    assert 1.3 - naive_delivered == pytest.approx(0.9)


def test_the_export_setpoint_is_always_positive_or_zero() -> None:
    """Positive Mode 2, by the arithmetic rather than by a later correction."""
    for house_kw in (0.0, 1.0, 4.0):
        for pv_kw in (0.0, 2.0, 10.0):
            for export_kwh in (0.0, 0.1, 1.0):
                decision = export(house_kw=house_kw, pv_kw=pv_kw, export_kwh=export_kwh)
                assert decision.applied_kw >= 0.0, (house_kw, pv_kw, export_kwh)


# == 2. case 26: actual meter export, never the marginal figure =============


def test_only_the_actual_meter_target_is_followed_when_idle_already_exports() -> None:
    """**Case 26.** Production already exporting, so actual and marginal differ.

    House 0.5, production 2.0, so the site exports 1.5 kW with the battery idle.
    Stage A plans an *actual* meter export of 3.0 kW for the quarter; the marginal
    figure -- what the battery caused -- is 1.5.

    Following the actual target needs ``0.5 - 2.0 + 3.0 = 1.5`` kW of battery, and
    the meter then reads 3.0. Following the marginal figure would command
    ``0.5 - 2.0 + 1.5 = 0.0`` and export only the 1.5 the sun was already sending --
    under-exporting by exactly the idle export, every quarter, silently.
    """
    house_kw, pv_kw = 0.5, 2.0
    idle_export_kw = pv_kw - house_kw
    assert idle_export_kw == pytest.approx(1.5)

    actual_target_kw = 3.0
    marginal_kw = actual_target_kw - idle_export_kw
    assert marginal_kw == pytest.approx(1.5)

    decision = export(
        house_kw=house_kw,
        pv_kw=pv_kw,
        export_kwh=actual_target_kw * 0.25,
        seconds=900.0,
    )

    assert decision.applied_kw == pytest.approx(1.5, abs=0.05)
    # And the meter reads the actual target, not the marginal one.
    assert battery_rate_to_export_kw(
        house_load_kw=house_kw, pv_kw=pv_kw, battery_kw=decision.applied_kw
    ) == pytest.approx(actual_target_kw, abs=0.05)

    # The marginal reading would have commanded nothing at all.
    assert export_rate_to_battery_kw(
        house_load_kw=house_kw, pv_kw=pv_kw, export_kw=marginal_kw
    ) == pytest.approx(0.0)


def test_it_does_not_add_the_batterys_contribution_on_top() -> None:
    """The other half of case 26: no over-export either.

    The failure mode symmetric to the one above would treat the target as the
    battery's *additional* export and reach 4.5 kW at the meter.
    """
    decision = export(house_kw=0.5, pv_kw=2.0, export_kwh=3.0 * 0.25, seconds=900.0)
    delivered = battery_rate_to_export_kw(
        house_load_kw=0.5, pv_kw=2.0, battery_kw=decision.applied_kw
    )

    assert delivered == pytest.approx(3.0, abs=0.05)
    assert delivered < 4.5


# == 3. the clamp order, and the reserve as an absolute ====================


def test_the_documented_clamp_order_is_published_and_complete() -> None:
    """Ten clamps, in one place, so the order is auditable rather than implied."""
    assert len(DISPATCH_EXPORT_CLAMP_ORDER) == 10
    assert len(set(DISPATCH_EXPORT_CLAMP_ORDER)) == 10


def test_the_inverter_discharge_limit_binds_and_is_named() -> None:
    """Clamp 1, and the reported reason says which bound it was."""
    decision = export(
        house_kw=0.0, pv_kw=0.0, export_kwh=10.0, max_discharge_kw=3.0, seconds=900.0
    )

    assert decision.applied_kw == pytest.approx(3.0, abs=0.05)
    assert decision.limited_by == DISPATCH_LIMIT_MAX_DISCHARGE


def test_no_export_price_unlocks_a_reserve_violation() -> None:
    """**Invariant 9, and it is absolute.**

    The reserve is a promise about tonight. A quarter being profitable is not a
    reason to break it, so the headroom bounds the discharge whatever the target
    says -- and with no headroom at all, nothing is exported.
    """
    # 0.4 kWh of headroom over the 0.25 h remaining is 1.6 kW, and that is the most
    # the pack may give however profitable the quarter is.
    decision = export(
        house_kw=0.0,
        pv_kw=0.0,
        export_kwh=10.0,
        reserve_headroom_kwh=0.4,
        seconds=900.0,
    )

    assert decision.applied_kw == pytest.approx(1.6, abs=0.05)
    assert decision.limited_by == DISPATCH_LIMIT_DYNAMIC_RESERVE

    exhausted = export(
        house_kw=0.0, pv_kw=0.0, export_kwh=10.0, reserve_headroom_kwh=0.0
    )
    assert exhausted.applied_kw == pytest.approx(0.0)


def test_the_authorised_battery_discharge_is_a_ceiling_on_an_export() -> None:
    """Clamp 4: the battery figure bounds an export rather than driving it.

    The mirror of the charge case, and the asymmetry in one assertion -- here the
    battery figure is the ceiling and the meter figure the objective.
    """
    decision = export(
        house_kw=0.0,
        pv_kw=0.0,
        export_kwh=10.0,
        battery_kwh=0.5,
        seconds=900.0,
    )

    assert decision.applied_kw == pytest.approx(2.0, abs=0.05)
    assert decision.limited_by == DISPATCH_LIMIT_REMAINING_DISCHARGE


def test_no_export_tick_can_exceed_the_remaining_authorised_energy() -> None:
    """**Invariant 6 on the export path, in both domains.**

    Asserted as a property, and with an honest note on why no single case shows the
    explicit cap *binding*: ``QuarterProgress.hours`` floors the divisor at the same
    horizon ``tick_energy_cap_kw`` uses, so the objective rate is already at most
    the cap in whichever domain drives it. The two agree exactly at the horizon and
    the objective is smaller everywhere outside it.

    So the cap in ``decide_export`` is a **backstop**, not an active clamp -- it
    would begin binding the moment that floor were changed or removed, which is
    precisely what a backstop is for. What matters is the invariant, and the
    invariant holds in both domains at every remaining time.
    """
    for seconds in (0.0, 1.0, 30.0, 89.0, 90.0, 91.0, 300.0, 900.0):
        for grid_kwh in (0.02, 0.1, 0.5, 2.0):
            for battery_kwh in (0.05, 0.5, 100.0):
                decision = export(
                    house_kw=0.0,
                    pv_kw=0.0,
                    export_kwh=grid_kwh,
                    battery_kwh=battery_kwh,
                    seconds=seconds,
                )
                battery_cap = tick_energy_cap_kw(battery_kwh)
                meter_cap = tick_energy_cap_kw(grid_kwh)
                assert decision.applied_kw <= battery_cap + 1e-9, (
                    seconds,
                    grid_kwh,
                    battery_kwh,
                )
                delivered_export = battery_rate_to_export_kw(
                    house_load_kw=0.0, pv_kw=0.0, battery_kw=decision.applied_kw
                )
                assert delivered_export <= meter_cap + 1e-9, (seconds, grid_kwh)


def test_a_spent_export_target_never_drifts_into_serving_the_house() -> None:
    """**Nothing left at the meter means nothing at the battery.**

    The trap the identity sets: with the target spent, ``house - pv + 0`` is 1.0 kW
    -- the power that would hold the meter at zero by supplying the house from the
    pack. That is ``serve_load``, which this release does not execute and which has
    no published meter target to be measured against.

    Left unguarded it would have fired on the tick path every time an export
    finished early in its quarter, discharging the battery under an authorisation
    that had already been met.
    """
    decision = export(house_kw=1.0, pv_kw=0.0, export_kwh=0.0)

    assert decision.applied_kw == pytest.approx(0.0)
    assert decision.limited_by == DISPATCH_LIMIT_REMAINING_EXPORT

    # And with production running too, which is the same trap with a smaller number.
    assert export(house_kw=2.0, pv_kw=0.5, export_kwh=0.0).applied_kw == pytest.approx(
        0.0
    )


def test_an_unbounded_export_reports_no_clamp() -> None:
    """A decision nothing reduced says so, rather than naming an inactive bound."""
    decision = export(house_kw=0.0, pv_kw=0.0, export_kwh=1.0, seconds=900.0)

    assert decision.limited_by == DISPATCH_LIMIT_NONE


# == 4. the authorisation checklist, one refusal per test ==================


def test_a_fully_satisfied_request_is_authorised() -> None:
    """The baseline: every condition met, and every condition reached."""
    verdict = authorize_export(authorised())

    assert verdict.safe
    assert verdict.inhibit_reason is None
    assert verdict.checks_evaluated == len(EXPORT_AUTHORISATION_ORDER)
    assert verdict.checks_passed == verdict.checks_evaluated


def test_the_published_order_matches_the_order_actually_checked() -> None:
    """The checklist is auditable from the constant, not only from the source."""
    verdict = authorize_export(authorised())

    assert [name for name, _ok in verdict.checks] == list(EXPORT_AUTHORISATION_ORDER)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # -- is this even an export? --
        ({"intent": EXECUTION_INTENT_GRID_CHARGE}, EXPORT_REFUSE_NOT_EXPORT_INTENT),
        ({"intent": "serve_load"}, EXPORT_REFUSE_NOT_EXPORT_INTENT),
        ({"intent": None}, EXPORT_REFUSE_NOT_EXPORT_INTENT),
        ({"quarter_admitted": False}, EXPORT_REFUSE_NO_QUARTER),
        ({"quarter_open": False}, EXPORT_REFUSE_QUARTER_NOT_OPEN),
        # -- can anything be commanded at all? --
        ({"missing_entities": ("input_number.x",)}, EXPORT_REFUSE_MISSING_ENTITY),
        ({"failsafe_available": False}, EXPORT_REFUSE_NO_FAILSAFE),
        ({"conflicting_feature": True}, EXPORT_REFUSE_CONFLICTING_FEATURE),
        ({"foreign_dispatch": True}, EXPORT_REFUSE_DISPATCH_FOREIGN),
        # -- is it ours, provably? --
        ({"owned": False}, EXPORT_REFUSE_NOT_OWNED),
        ({"causation_proven": False}, EXPORT_REFUSE_RECORD_MISMATCH),
        ({"coherent": False}, EXPORT_REFUSE_INCOHERENT),
        # -- has the battery anything to give, above both floors? --
        ({"soc_percent": None}, EXPORT_REFUSE_SOC_UNUSABLE),
        ({"soc_percent": 20.0}, EXPORT_REFUSE_MIN_SOC),
        ({"soc_percent": 10.0}, EXPORT_REFUSE_MIN_SOC),
        ({"reserve_headroom_kwh": 0.0}, EXPORT_REFUSE_RESERVE_FLOOR),
        ({"reserve_headroom_kwh": -1.0}, EXPORT_REFUSE_RESERVE_FLOOR),
        # -- anything left authorised, in both domains? --
        ({"battery_remaining_kwh": 0.0}, EXPORT_REFUSE_NO_BATTERY_ALLOWANCE),
        ({"grid_export_remaining_kwh": 0.0}, EXPORT_REFUSE_NO_EXPORT_TARGET),
        # -- does the power respect every bound? --
        ({"inverter_max_discharge_kw": 1.0}, EXPORT_REFUSE_INVERTER_LIMIT),
        ({"site_export_limit_kw": 1.0}, "site_export_limit"),
        ({"tick_cap_kw": 1.0}, EXPORT_REFUSE_TICK_HORIZON),
        ({"requested_kw": 0.05}, "power_below_device_minimum"),
        (
            {
                "requested_kw": 999.0,
                "inverter_max_discharge_kw": None,
                "tick_cap_kw": None,
            },
            "power_above_device_maximum",
        ),
    ],
)
def test_every_condition_fails_closed_on_its_own(
    overrides: dict, expected: str
) -> None:
    """Spoil exactly one field, and the export is refused for exactly that reason.

    One test per clause, because a checklist asserted only in aggregate passes with
    a condition silently removed.
    """
    verdict = authorize_export(authorised(**overrides))

    assert not verdict.safe
    assert verdict.inhibit_reason == expected


def test_an_unconstrained_bound_is_not_a_refusal() -> None:
    """``None`` means genuinely unconstrained, and must not fail closed as zero.

    The distinction matters: no site export limit is configured in this integration,
    so treating the absent value as a bound of zero would refuse every export.
    """
    verdict = authorize_export(
        authorised(
            inverter_max_discharge_kw=None,
            site_export_limit_kw=None,
            tick_cap_kw=None,
            reserve_headroom_kwh=None,
            configured_min_soc_percent=None,
        )
    )

    assert verdict.safe


def test_the_authorisation_writes_nothing_and_names_no_entity() -> None:
    """It authorises an economic intent; it is not an actuator path.

    Asserted structurally: the function's own module may name entities elsewhere,
    but this function takes only plain values and returns only a verdict, so there
    is nothing it could write through.
    """
    import inspect

    from custom_components.alpha_ems_manager import safety

    source = inspect.getsource(safety.authorize_export)

    for forbidden in ("hass", "async_", "CommandStep", "input_number", "input_boolean"):
        assert forbidden not in source, forbidden


# == 5. the surface: Dispatch Mode 2, and never a helper family ============


def test_the_intent_to_sign_map_permits_exactly_two_directions() -> None:
    """Charge negative, export positive, and nothing else executable at all."""
    assert CONTROL_EXECUTABLE_DISPATCH_SIGNS == {
        EXECUTION_INTENT_GRID_CHARGE: -1,
        EXECUTION_INTENT_NET_EXPORT: +1,
    }
    assert permitted_sign(EXECUTION_INTENT_NET_EXPORT) == +1
    assert permitted_sign(EXECUTION_INTENT_GRID_CHARGE) == -1
    # Everything unverified refuses, without being enumerated anywhere.
    assert permitted_sign("serve_load") is None
    assert permitted_sign(None) is None
    assert permitted_sign("anything_at_all") is None


def test_the_live_executable_intents_are_the_same_two() -> None:
    """One set decides the actuator surface, and it is keyed on the intent.

    This is the set the arm branch tests. It used to test ``action != charge``,
    which -- once ``net_export`` mapped to ``ACTION_DISCHARGE`` -- would have armed
    an export on the Force Discharging helper family.
    """
    assert (
        frozenset({EXECUTION_INTENT_GRID_CHARGE, EXECUTION_INTENT_NET_EXPORT})
        == CONTROL_LIVE_DISPATCH_INTENTS
    )
    assert set(CONTROL_EXECUTABLE_DISPATCH_SIGNS) == set(CONTROL_LIVE_DISPATCH_INTENTS)


def test_a_negative_power_under_an_export_intent_is_refused() -> None:
    """The value gate, keyed on the intent. Wrong-way dispatch cannot be sent."""
    steps = plan_dispatch_power(-2.2)

    assert dispatch_refusal(EXECUTION_INTENT_NET_EXPORT, steps) == (
        CONTROL_REFUSE_DISPATCH_SIGN
    )
    assert dispatch_refusal(EXECUTION_INTENT_GRID_CHARGE, steps) is None


def test_a_positive_power_under_a_charge_intent_is_refused() -> None:
    """And symmetrically, which a single scalar could not express."""
    steps = plan_dispatch_power(2.2)

    assert dispatch_refusal(EXECUTION_INTENT_GRID_CHARGE, steps) == (
        CONTROL_REFUSE_DISPATCH_SIGN
    )
    assert dispatch_refusal(EXECUTION_INTENT_NET_EXPORT, steps) is None


def test_a_missing_or_unknown_intent_fails_closed() -> None:
    """No intent is not a licence to guess a direction."""
    assert dispatch_refusal(None, plan_dispatch_power(2.2)) == (
        CONTROL_REFUSE_DISPATCH_SIGN
    )
    assert dispatch_refusal("serve_load", plan_dispatch_power(2.2)) == (
        CONTROL_REFUSE_DISPATCH_SIGN
    )
    # Zero is permitted for either, and has to be: it is what the cleanup writes
    # and what the direction gate produces when it will not command a direction.
    assert dispatch_refusal(None, plan_dispatch_power(0.0)) is None


def test_zero_matches_every_intent() -> None:
    """Commanding nothing is never the wrong direction."""
    assert sign_matches_intent(EXECUTION_INTENT_NET_EXPORT, 0.0)
    assert sign_matches_intent(EXECUTION_INTENT_GRID_CHARGE, 0.0)
    # But only a *known* intent. With no authority there is nothing to match, zero
    # included -- deliberately stricter than ``dispatch_refusal``, which must permit
    # a zero write with no intent because that is what the cleanup does when the run
    # that authorised it is already over.
    assert not sign_matches_intent(None, 0.0)
    assert not sign_matches_intent(None, 1.0)
    assert not sign_matches_intent("serve_load", 1.0)
    assert dispatch_refusal(None, plan_dispatch_power(0.0)) is None


def test_a_live_export_arm_touches_only_the_dispatch_surface() -> None:
    """**The 6.0 regression.** Force Discharging is never written for an export.

    The whole trap: ``net_export -> ACTION_DISCHARGE`` exists so a stop can name
    what it stops, and ``ACTION_DISCHARGE`` is where the Force Discharging family
    used to lead. Asserted at the step list, which is the thing that reaches the
    wire.
    """
    steps = plan_dispatch_arm(
        mode=DISPATCH_MODE_SOC_CONTROL,
        power_kw=2.2,
        cutoff_soc_percent=21,
        duration_minutes=20,
        pv_enabled=True,
    )

    entities = {step.entity_id for step in steps}
    assert not entities & set(DISCHARGE_FAMILY.entities), entities
    assert steps_outside_capability(steps) == ()
    assert dispatch_refusal(EXECUTION_INTENT_NET_EXPORT, steps) is None

    power = [step for step in steps if step.entity_id == DISPATCH_POWER]
    assert len(power) == 1
    assert power[0].value == pytest.approx(2.2)


def test_the_discharge_family_is_still_refused_at_the_boundary() -> None:
    """Widening what Dispatch may do did not widen the helper surface."""
    step = CommandStep("input_boolean", "turn_on", DISCHARGE_FAMILY.activate)

    assert steps_outside_capability((step,)) == (DISCHARGE_FAMILY.activate,)


def test_an_export_command_is_unreachable_without_an_admitted_quarter() -> None:
    """No quarter, no export -- by construction, not by a caller remembering.

    ``export_intent_for`` takes a ``CarriedQuarter`` as its first positional
    argument, so there is no signature through which an export command could be
    built without one.
    """
    import inspect

    from custom_components.alpha_ems_manager.execution import export_intent_for

    signature = inspect.signature(export_intent_for)
    first = next(iter(signature.parameters.values()))

    assert first.name == "quarter"
    assert first.default is inspect.Parameter.empty


def test_control_intent_for_still_cannot_return_a_discharge() -> None:
    """The charge-only guarantee that made the interlock structural is intact.

    beta.27 builds the export elsewhere rather than widening this, which is the
    whole reason ``export_intent_for`` exists as a separate function.
    """
    import inspect

    from custom_components.alpha_ems_manager import execution

    source = inspect.getsource(execution.control_intent_for)

    assert "ACTION_CHARGE" in source
    assert "ACTION_DISCHARGE" not in source
