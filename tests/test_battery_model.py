"""The battery's physical model, and the limits that may never be crossed.

Pure arithmetic, so none of this needs Home Assistant. The file is organised
around the two things that would actually hurt if they were wrong: the electrical
boundary the efficiency is applied at, and the clamp.

Six of the assertions below were written against defects that were reproduced
numerically before the model was accepted, and each is named where it appears:
a negative request creating energy, a simultaneous charge and discharge
destroying it invisibly, a state of charge below zero permitting an over-fill, an
efficiency above one, a negative capacity yielding a silently inert battery, and
``NaN`` surviving ``min`` in one argument order but not the other.
"""

from __future__ import annotations

import math
from itertools import product

import pytest

from custom_components.alpha_ems_manager.battery import (
    INTERVAL_HOURS,
    BatteryLimits,
    BatteryRequest,
    BatteryState,
    advance,
    apply_request,
    build_limits,
    build_state,
    sanitize_soc_percent,
    split_grid_energy,
    static_reserve,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_HOLD,
    BATTERY_MAX_SOC_PERCENT,
    CONSTRAINT_MAX_CHARGE_POWER,
    CONSTRAINT_MAX_DISCHARGE_POWER,
    CONSTRAINT_MAX_SOC,
    CONSTRAINT_MIN_SOC,
    MODE_CHARGE,
    MODE_DISCHARGE,
    MODE_IDLE,
    QUARTER_MINUTES,
    REASON_INVALID_EFFICIENCY,
    REASON_MISSING_CAPACITY,
    REASON_MISSING_POWER_LIMITS,
    RESERVE_CONFIGURED,
)


def limits_for(
    *,
    capacity_kwh: float = 10.0,
    max_charge_kw: float = 5.0,
    max_discharge_kw: float = 5.0,
    round_trip_efficiency_percent: float = 90.0,
) -> BatteryLimits:
    """Return usable limits, asserting they were accepted."""
    limits, reason = build_limits(
        capacity_kwh=capacity_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        round_trip_efficiency_percent=round_trip_efficiency_percent,
    )
    assert limits is not None, reason
    return limits


def state_for(soc_percent: float, *, min_soc: float = 20.0, **kwargs) -> BatteryState:
    """Return a state seeded from a state of charge, asserting it was accepted."""
    state = build_state(
        soc_percent=soc_percent,
        limits=limits_for(**kwargs),
        reserve=static_reserve(min_soc),
    )
    assert state is not None
    return state


# -- the interval is fifteen minutes, and not a parameter --------------------


def test_the_interval_duration_is_derived_and_not_a_parameter() -> None:
    """Every quarter-hour is fifteen minutes; only the count changes with DST.

    Accepting a duration would invite ``1.0``, ``900`` or -- worst -- ``0.91666``
    "for the short daylight-saving hour", which is precisely the class of mistake
    the chronological-interval design exists to prevent. So there is no such
    parameter, and this pins that.
    """
    assert INTERVAL_HOURS == QUARTER_MINUTES / 60.0 == 0.25

    import inspect

    signature = inspect.signature(apply_request)
    assert list(signature.parameters) == ["state", "request"]


# -- the electrical boundary -------------------------------------------------


def test_ten_kilowatt_hours_in_returns_exactly_nine_out() -> None:
    """The single highest-value assertion in the phase.

    Ten kilowatt-hours of AC energy charged at 90 % round trip must raise the
    stored DC energy by exactly ``sqrt(0.9) * 10``, and discharging all of it must
    return exactly 9.0 kWh AC. Bit-exact, deliberately: this fails if an
    efficiency ever migrates into the state-of-charge arithmetic, if the DC
    boundary is flipped, or if the symmetric split changes without the default
    changing with it.
    """
    limits = limits_for(capacity_kwh=100.0, max_charge_kw=50.0, max_discharge_kw=50.0)
    empty = build_state(soc_percent=0.0, limits=limits, reserve=static_reserve(0.0))
    assert empty is not None

    charged = apply_request(empty, BatteryRequest.charge(10.0 / INTERVAL_HOURS))
    assert charged.charge_ac_kwh == 10.0
    assert charged.end_energy_kwh == 9.486832980505138

    full = advance(empty, charged)
    emptied = apply_request(full, BatteryRequest.discharge(50.0))
    assert emptied.discharge_ac_kwh == 9.0
    assert emptied.end_energy_kwh == 0.0


def test_efficiency_never_touches_the_state_of_charge_arithmetic() -> None:
    """A state of charge is a DC quantity and carries no efficiency factor."""
    for percent in (50.0, 75.0, 90.0, 100.0):
        limits = limits_for(round_trip_efficiency_percent=percent)
        # 5 kWh of DC energy in a 10 kWh pack is 50 %, whatever the inverter does.
        assert limits.soc_for_energy(5.0) == 50.0
        assert limits.energy_for_soc(50.0) == 5.0


def test_the_efficiency_split_is_symmetric_and_reproduces_the_round_trip() -> None:
    """One configured figure, split into two fields kept separately."""
    limits = limits_for(round_trip_efficiency_percent=90.0)
    assert limits.charge_efficiency == limits.discharge_efficiency
    assert limits.charge_efficiency == math.sqrt(0.9)
    assert limits.round_trip_efficiency == pytest.approx(0.9, abs=1e-12)


def test_a_perfect_battery_loses_nothing() -> None:
    """At 100 % round trip the boundary is transparent, and nothing divides badly."""
    limits = limits_for(round_trip_efficiency_percent=100.0)
    assert limits.charge_efficiency == 1.0
    empty = build_state(soc_percent=0.0, limits=limits, reserve=static_reserve(0.0))
    assert empty is not None

    charged = apply_request(empty, BatteryRequest.charge(4.0))
    assert charged.charge_ac_kwh == 1.0
    assert charged.end_energy_kwh == 1.0


def test_the_usable_and_deliverable_energies_are_named_apart() -> None:
    """Ten kilowatt-hours, half full, a fifth reserved: 3 kWh DC, 2.85 kWh AC.

    Conflating the two is the most likely way to over-promise a reserve, so they
    are separate properties under names that say which side they are on.
    """
    state = state_for(50.0, min_soc=20.0)
    assert state.energy_kwh == 5.0
    assert state.usable_energy_kwh == 3.0
    assert state.deliverable_energy_kwh == pytest.approx(2.846049894151541, abs=1e-12)
    assert state.deliverable_energy_kwh < state.usable_energy_kwh


# -- refusals: nothing plausible is built from nonsense ----------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"capacity_kwh": None}, REASON_MISSING_CAPACITY),
        ({"capacity_kwh": 0.0}, REASON_MISSING_CAPACITY),
        ({"capacity_kwh": -10.0}, REASON_MISSING_CAPACITY),
        ({"capacity_kwh": float("nan")}, REASON_MISSING_CAPACITY),
        ({"capacity_kwh": float("inf")}, REASON_MISSING_CAPACITY),
        ({"capacity_kwh": 10_000.0}, REASON_MISSING_CAPACITY),
        ({"capacity_kwh": True}, REASON_MISSING_CAPACITY),
        ({"capacity_kwh": "10"}, REASON_MISSING_CAPACITY),
        ({"max_charge_kw": None}, REASON_MISSING_POWER_LIMITS),
        ({"max_charge_kw": 0.0}, REASON_MISSING_POWER_LIMITS),
        ({"max_charge_kw": -5.0}, REASON_MISSING_POWER_LIMITS),
        ({"max_discharge_kw": None}, REASON_MISSING_POWER_LIMITS),
        ({"max_discharge_kw": float("nan")}, REASON_MISSING_POWER_LIMITS),
        ({"max_discharge_kw": 500.0}, REASON_MISSING_POWER_LIMITS),
        ({"round_trip_efficiency_percent": None}, REASON_INVALID_EFFICIENCY),
        ({"round_trip_efficiency_percent": 0.0}, REASON_INVALID_EFFICIENCY),
        ({"round_trip_efficiency_percent": -90.0}, REASON_INVALID_EFFICIENCY),
        ({"round_trip_efficiency_percent": 105.0}, REASON_INVALID_EFFICIENCY),
        # The one a user is most likely to type: a fraction where a percentage
        # belongs. Without the floor this would model a battery that loses ninety
        # per cent of everything, and look entirely plausible doing it.
        ({"round_trip_efficiency_percent": 0.9}, REASON_INVALID_EFFICIENCY),
        ({"round_trip_efficiency_percent": float("inf")}, REASON_INVALID_EFFICIENCY),
    ],
)
def test_unusable_hardware_facts_are_refused_and_named(
    kwargs: dict, reason: str
) -> None:
    """Never a division by zero, and never a silently inert battery.

    A negative capacity is the dangerous one: it inverts every comparison in the
    model, so both the available energy and the headroom clamp to zero and the
    battery does nothing at all while reporting perfectly valid figures. That is
    the failure mode ``normalization.py`` names as the most damaging available,
    and it has to be a refusal rather than a result.
    """
    base = {
        "capacity_kwh": 10.0,
        "max_charge_kw": 5.0,
        "max_discharge_kw": 5.0,
        "round_trip_efficiency_percent": 90.0,
    }
    limits, actual = build_limits(**{**base, **kwargs})

    assert limits is None
    assert actual == reason


def test_an_efficiency_above_one_cannot_create_energy() -> None:
    """Ten kilowatt-hours in must never be more than ten kilowatt-hours out."""
    limits, reason = build_limits(
        capacity_kwh=10.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency_percent=110.0,
    )
    assert limits is None
    assert reason == REASON_INVALID_EFFICIENCY


@pytest.mark.parametrize(
    "value", [None, float("nan"), float("inf"), float("-inf"), -20.0, 120.0, 101.5]
)
def test_an_impossible_state_of_charge_is_refused_not_clamped(value) -> None:
    """A reading outside the band is an unreadable source, not a number.

    ``-20 %`` is the one that matters. The charge headroom of a 10 kWh pack would
    be computed as ``(100 - -20) / 100 * 10`` = 12 kWh, so a single bad sample
    would permit filling the pack past its own capacity. The two clamps in
    ``apply_request`` look symmetrical and are not, which is why this is refused
    at the door.
    """
    assert sanitize_soc_percent(value) is None
    assert (
        build_state(
            soc_percent=value, limits=limits_for(), reserve=static_reserve(20.0)
        )
        is None
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-0.4, 0.0), (0.0, 0.0), (100.0, 100.0), (100.6, 100.0), (55.0, 55.0)],
)
def test_sensor_noise_at_either_end_is_clamped_back_in(
    value: float, expected: float
) -> None:
    """A narrow band is noise; beyond it is a different question entirely."""
    assert sanitize_soc_percent(value) == expected


# -- requests: the direction carries the sign -------------------------------


@pytest.mark.parametrize("magnitude", [-1.0, -0.001, float("nan"), float("inf"), 0.0])
def test_an_unusable_magnitude_degrades_to_idle(magnitude: float) -> None:
    """A negative request must never *add* energy to the battery.

    Reproduced against the first draft: ``min(-1.0, max_discharge)`` returns
    ``-1.0``, the non-negativity guards sat on the available energy rather than
    on the request, and -0.25 kWh of AC energy became +0.2635 kWh of stored DC
    energy -- an effective efficiency of 1.054. A negative is exactly what
    arrives if a caller ever passes a raw battery-power sensor.

    ``NaN`` is the same class of problem with a different mechanism: it survives
    ``min`` in one argument order and not the other, so it would have reached the
    stored energy and poisoned every interval after it.
    """
    state = state_for(50.0)
    for request in (
        BatteryRequest.discharge(magnitude),
        BatteryRequest.charge(magnitude),
    ):
        assert request.mode == MODE_IDLE
        assert request.power_kw == 0.0
        outcome = apply_request(state, request)
        assert outcome.charge_ac_kwh == 0.0
        assert outcome.discharge_ac_kwh == 0.0
        assert outcome.end_energy_kwh == state.energy_kwh


def test_a_request_cannot_charge_and_discharge_at_once() -> None:
    """Unrepresentable, not merely invalid.

    Two independently firing rules asking for 4 kW each would leave the grid
    residual exactly equal to the load -- identical to doing nothing -- while
    stored energy fell by ``1/eta - eta`` = 0.10541 kWh. Over ninety-six
    intervals that is 10.1 kWh, an entire pack, with a perfectly balanced grid
    trace and nothing in the only output a consumer reads to show it.

    Phase 1 assumed this away; the type makes it impossible to express.
    """
    request = BatteryRequest.discharge(4.0)
    assert request.mode == MODE_DISCHARGE
    assert not hasattr(request, "charge_power_kw")

    outcome = apply_request(state_for(50.0), request)
    assert outcome.charge_ac_kwh == 0.0 or outcome.discharge_ac_kwh == 0.0
    assert outcome.allowed_energy_ac_kwh == outcome.discharge_ac_kwh


def test_the_loss_a_simultaneous_request_would_have_caused_is_real() -> None:
    """Documents the magnitude the type prevents, so the guard is not mysterious."""
    limits = limits_for()
    eta = limits.discharge_efficiency
    per_interval = 1.0 / eta - eta

    assert per_interval == pytest.approx(0.10541, abs=1e-5)
    assert per_interval * 96 == pytest.approx(10.12, abs=0.01)


# -- the clamp ---------------------------------------------------------------


def test_a_power_request_is_clamped_to_the_ac_limit() -> None:
    """The power limit is AC-side, which is what a clamp meter reads."""
    outcome = apply_request(state_for(80.0), BatteryRequest.discharge(50.0))

    assert CONSTRAINT_MAX_DISCHARGE_POWER in outcome.constraints
    assert outcome.average_power_kw == pytest.approx(5.0)
    assert outcome.discharge_ac_kwh == pytest.approx(5.0 * INTERVAL_HOURS)


def test_a_charge_request_is_clamped_to_its_own_limit() -> None:
    """The two limits are separate fields and neither stands in for the other."""
    state = state_for(20.0, max_charge_kw=2.0, max_discharge_kw=9.0)
    outcome = apply_request(state, BatteryRequest.charge(9.0))

    assert CONSTRAINT_MAX_CHARGE_POWER in outcome.constraints
    assert outcome.average_power_kw == pytest.approx(2.0)


def test_a_discharge_is_clamped_to_the_energy_above_the_floor() -> None:
    """Twenty-one per cent of a 10 kWh pack above a 20 % floor is 0.1 kWh DC."""
    state = state_for(21.0, min_soc=20.0)
    assert state.usable_energy_kwh == pytest.approx(0.1)

    outcome = apply_request(state, BatteryRequest.discharge(5.0))

    assert CONSTRAINT_MIN_SOC in outcome.constraints
    assert outcome.discharge_ac_kwh == pytest.approx(0.1 * math.sqrt(0.9))
    assert outcome.end_energy_kwh == pytest.approx(2.0)


def test_a_charge_is_clamped_to_the_headroom() -> None:
    """A nearly full pack accepts only what fits."""
    state = state_for(99.0)
    assert state.headroom_energy_kwh == pytest.approx(0.1)

    outcome = apply_request(state, BatteryRequest.charge(5.0))

    assert CONSTRAINT_MAX_SOC in outcome.constraints
    assert outcome.end_energy_kwh == pytest.approx(10.0)
    assert outcome.charge_ac_kwh == pytest.approx(0.1 / math.sqrt(0.9))


def test_a_more_efficient_charger_imports_less_to_fill_the_same_headroom() -> None:
    """Counter-intuitive and physically correct, so it is pinned deliberately.

    In the ceiling-bound case the imported AC energy is ``headroom / efficiency``,
    which *decreases* as efficiency rises. Any sweep assuming otherwise is wrong,
    and this is the assertion that says so.
    """
    imports = []
    for percent in (60.0, 80.0, 100.0):
        state = state_for(99.0, round_trip_efficiency_percent=percent)
        imports.append(apply_request(state, BatteryRequest.charge(5.0)).charge_ac_kwh)

    assert imports[0] > imports[1] > imports[2]


def test_the_clamp_is_idempotent() -> None:
    """Feeding an allowed power back in reproduces it exactly.

    True only because the energy clamp is applied in DC terms *before* the
    conversion back to AC. ``(E_dc * eta) / eta`` can exceed ``E_dc`` by one ulp,
    and the subsequent ``min`` absorbs it; clamping the AC energy against
    ``available * eta`` instead would lose that and this test would fail.
    """
    for soc, magnitude in product((21.0, 50.0, 80.0, 99.0), (0.5, 4.0, 50.0)):
        state = state_for(soc)
        for factory in (BatteryRequest.discharge, BatteryRequest.charge):
            first = apply_request(state, factory(magnitude))
            again = apply_request(state, factory(first.average_power_kw))
            assert again.allowed_energy_ac_kwh == pytest.approx(
                first.allowed_energy_ac_kwh, abs=1e-12
            )
            assert again.end_energy_kwh == pytest.approx(
                first.end_energy_kwh, abs=1e-12
            )


# -- the floor ---------------------------------------------------------------


def test_at_the_floor_there_is_nothing_left_to_give() -> None:
    """Exactly at the reserve: no discharge, and the energy does not move."""
    state = state_for(20.0, min_soc=20.0)
    assert state.at_or_below_floor is True
    assert state.below_floor is False

    outcome = apply_request(state, BatteryRequest.discharge(4.0))

    assert outcome.discharge_ac_kwh == 0.0
    assert outcome.mode == MODE_IDLE
    assert outcome.action == ACTION_HOLD
    assert outcome.end_energy_kwh == state.energy_kwh


def test_below_the_floor_nothing_is_discharged_and_nothing_is_invented() -> None:
    """Under the reserve the pack is left exactly where it is.

    Not topped up to the floor: that would be fabricating energy, and recovering
    is a decision needing a justification this phase cannot produce.
    """
    state = state_for(15.0, min_soc=20.0)
    assert state.below_floor is True
    assert state.usable_energy_kwh == 0.0

    outcome = apply_request(state, BatteryRequest.discharge(4.0))

    assert outcome.discharge_ac_kwh == 0.0
    assert outcome.end_energy_kwh == pytest.approx(1.5)


def test_above_the_ceiling_a_discharge_lands_exactly_on_the_floor() -> None:
    """The asymmetry between the two out-of-band cases, made explicit."""
    limits = limits_for(max_discharge_kw=50.0)
    state = BatteryState(energy_kwh=10.0, limits=limits, reserve=static_reserve(20.0))
    outcome = apply_request(state, BatteryRequest.discharge(50.0))

    assert outcome.end_energy_kwh >= state.floor_energy_kwh
    charge = apply_request(state, BatteryRequest.charge(50.0))
    assert charge.charge_ac_kwh == 0.0
    assert charge.end_energy_kwh == pytest.approx(10.0)


def test_a_zero_floor_leaves_the_whole_pack_usable() -> None:
    """Zero is a legal reserve and must not be treated as unset."""
    state = state_for(100.0, min_soc=0.0)
    assert state.usable_energy_kwh == 10.0
    assert state.floor_energy_kwh == 0.0


def test_a_ninety_nine_percent_floor_leaves_almost_nothing() -> None:
    """An extreme but legal setting still behaves arithmetically."""
    state = state_for(100.0, min_soc=99.0)
    assert state.usable_energy_kwh == pytest.approx(0.1)


# -- the reserve, and the two floors ----------------------------------------


def test_the_static_reserve_makes_both_floors_equal() -> None:
    """In this phase the user's setting is also the policy target."""
    reserve = static_reserve(20.0)

    assert reserve.configured_min_soc_percent == 20.0
    assert reserve.effective_min_soc_percent == 20.0
    assert reserve.source == RESERVE_CONFIGURED
    assert reserve.raised_above_configured is False


@pytest.mark.parametrize(
    ("configured", "expected"), [(-5.0, 0.0), (0.0, 0.0), (150.0, 100.0), (20.0, 20.0)]
)
def test_the_reserve_factory_confines_the_configured_floor(
    configured: float, expected: float
) -> None:
    """A stored value outside the range cannot become a floor outside it."""
    reserve = static_reserve(configured)
    assert reserve.configured_min_soc_percent == expected
    assert reserve.effective_min_soc_percent == expected


def test_the_clamp_obeys_the_configured_floor_even_if_the_effective_one_is_higher() -> (
    None
):
    """The hard floor is the user's, and a raised policy target does not move it.

    Phase 7 will raise the effective reserve dynamically, and Phase 8 has to be
    able to say "a price spike justifies dipping into the reserve, but never below
    the floor the user set". This constructs that future state by hand to prove
    the clamp already behaves correctly under it.
    """
    from custom_components.alpha_ems_manager.battery import BatteryReserve
    from custom_components.alpha_ems_manager.const import RESERVE_DYNAMIC

    raised = BatteryReserve(
        configured_min_soc_percent=20.0,
        effective_min_soc_percent=40.0,
        source=RESERVE_DYNAMIC,
    )
    state = BatteryState(energy_kwh=3.0, limits=limits_for(), reserve=raised)

    assert raised.raised_above_configured is True
    # The clamp measures against the configured 20 %, so 1 kWh is available.
    assert state.usable_energy_kwh == pytest.approx(1.0)
    outcome = apply_request(state, BatteryRequest.discharge(50.0))
    assert outcome.end_energy_kwh == pytest.approx(2.0)


# -- the grid residual ------------------------------------------------------


def test_the_grid_split_is_unsigned_with_at_most_one_side_non_zero() -> None:
    """Shaped like ``split_grid_power``, so nothing downstream reasons about signs."""
    covered = split_grid_energy(
        load_ac_kwh=0.5, charge_ac_kwh=0.0, discharge_ac_kwh=0.2
    )
    assert covered.import_kwh == pytest.approx(0.3)
    assert covered.export_kwh == 0.0

    exporting = split_grid_energy(
        load_ac_kwh=0.1, charge_ac_kwh=0.0, discharge_ac_kwh=0.5
    )
    assert exporting.import_kwh == 0.0
    assert exporting.export_kwh == pytest.approx(0.4)

    charging = split_grid_energy(
        load_ac_kwh=0.5, charge_ac_kwh=0.25, discharge_ac_kwh=0.0
    )
    assert charging.import_kwh == pytest.approx(0.75)
    assert charging.export_kwh == 0.0

    for entry in (covered, exporting, charging):
        assert entry.import_kwh >= 0.0
        assert entry.export_kwh >= 0.0
        assert entry.import_kwh == 0.0 or entry.export_kwh == 0.0


# -- the invariant sweep ----------------------------------------------------

#: A deliberately coarse lattice rather than a random generator. The repository
#: has no property-testing dependency, and a fixed lattice is reproducible --
#: which matters more here than breadth, because a failure has to be
#: investigable, not merely reported.
_SOC = (0.0, 5.0, 19.9, 20.0, 20.1, 50.0, 99.9, 100.0)
_MIN_SOC = (0.0, 20.0, 99.0)
_CAPACITY = (0.1, 10.0, 200.0)
_POWER = (0.0, 0.1, 5.0, 50.0, 1e6)
_EFFICIENCY = (50.0, 90.0, 100.0)


def _lattice():
    """Yield every (state, request) pair in the sweep."""
    for soc, minimum, capacity, power, efficiency in product(
        _SOC, _MIN_SOC, _CAPACITY, _POWER, _EFFICIENCY
    ):
        limits = limits_for(
            capacity_kwh=capacity,
            max_charge_kw=5.0,
            max_discharge_kw=5.0,
            round_trip_efficiency_percent=efficiency,
        )
        state = build_state(
            soc_percent=soc, limits=limits, reserve=static_reserve(minimum)
        )
        assert state is not None
        for request in (
            BatteryRequest.idle(),
            BatteryRequest.charge(power),
            BatteryRequest.discharge(power),
        ):
            yield state, request


def test_the_sweep_actually_covers_something() -> None:
    """Guard the lattice against silently collapsing to nothing."""
    assert sum(1 for _ in _lattice()) == (
        len(_SOC) * len(_MIN_SOC) * len(_CAPACITY) * len(_POWER) * len(_EFFICIENCY) * 3
    )


def test_no_single_interval_ever_leaves_the_allowed_band() -> None:
    """The invariant that matters most, over the whole lattice.

    The band is widened to include wherever the interval began, because a pack
    starting below the floor must be left where it is rather than topped up to
    it. Everything else -- non-negativity, at most one direction, finiteness, the
    power limit -- is asserted in the same pass.
    """
    for state, request in _lattice():
        outcome = apply_request(state, request)
        limits = state.limits

        lower = min(state.floor_energy_kwh, state.energy_kwh)
        upper = max(state.ceiling_energy_kwh, state.energy_kwh)
        assert lower - 1e-9 <= outcome.end_energy_kwh <= upper + 1e-9, (
            state,
            request,
        )

        assert math.isfinite(outcome.end_energy_kwh)
        assert outcome.charge_ac_kwh >= 0.0
        assert outcome.discharge_ac_kwh >= 0.0
        assert outcome.charge_ac_kwh == 0.0 or outcome.discharge_ac_kwh == 0.0

        limit = (
            limits.max_charge_kw
            if outcome.mode == MODE_CHARGE
            else limits.max_discharge_kw
        )
        assert outcome.average_power_kw <= limit + 1e-9

        # Never more than was asked for, and never in the other direction.
        if not request.is_idle:
            assert outcome.average_power_kw <= request.power_kw + 1e-9
            assert outcome.mode in (request.mode, MODE_IDLE)
        else:
            assert outcome.mode == MODE_IDLE


def test_energy_is_conserved_across_the_boundary_everywhere() -> None:
    """The DC delta is the AC energy times that direction's efficiency, once."""
    for state, request in _lattice():
        outcome = apply_request(state, request)
        delta = outcome.end_energy_kwh - state.energy_kwh
        limits = state.limits

        if outcome.mode == MODE_CHARGE:
            assert delta == pytest.approx(
                outcome.charge_ac_kwh * limits.charge_efficiency, abs=1e-9
            )
        elif outcome.mode == MODE_DISCHARGE:
            assert -delta == pytest.approx(
                outcome.discharge_ac_kwh / limits.discharge_efficiency, abs=1e-9
            )
        else:
            assert delta == pytest.approx(0.0, abs=1e-9)


def test_the_same_inputs_always_produce_an_equal_outcome() -> None:
    """Determinism, asserted on the frozen records themselves."""
    for state, request in _lattice():
        assert apply_request(state, request) == apply_request(state, request)


def test_the_action_always_matches_the_energy_that_moved() -> None:
    """A reported action can never disagree with the energy beside it."""
    for state, request in _lattice():
        outcome = apply_request(state, request)
        if outcome.action == ACTION_CHARGE:
            assert outcome.charge_ac_kwh > 0.0
        elif outcome.action == ACTION_DISCHARGE:
            assert outcome.discharge_ac_kwh > 0.0
        else:
            assert outcome.action == ACTION_HOLD
            assert outcome.allowed_energy_ac_kwh == 0.0


def test_a_full_day_of_repeated_discharge_never_crosses_the_floor() -> None:
    """The single-interval invariant compounded over a whole civil day."""
    for minimum, intervals in product((0.0, 20.0, 99.0), (92, 96, 100)):
        state = state_for(100.0, min_soc=minimum)
        floor = state.floor_energy_kwh
        for _ in range(intervals):
            outcome = apply_request(state, BatteryRequest.discharge(5.0))
            assert outcome.end_energy_kwh >= floor - 1e-9
            state = advance(state, outcome)
        assert state.energy_kwh == pytest.approx(floor, abs=1e-9)
        assert state.soc_percent == pytest.approx(minimum, abs=1e-9)


def test_a_full_day_of_repeated_charge_never_crosses_the_ceiling() -> None:
    """The same, in the other direction and against the internal ceiling."""
    state = state_for(0.0, min_soc=0.0)
    for _ in range(100):
        outcome = apply_request(state, BatteryRequest.charge(5.0))
        assert outcome.end_energy_kwh <= state.ceiling_energy_kwh + 1e-9
        state = advance(state, outcome)
    assert state.soc_percent == pytest.approx(BATTERY_MAX_SOC_PERCENT, abs=1e-9)
