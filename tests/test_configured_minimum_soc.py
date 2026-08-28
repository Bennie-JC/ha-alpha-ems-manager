"""The configured minimum state of charge is the floor. It is not 20 %.

**Twenty per cent is the maintainer's setting and the option's default, not a
product rule.** It appears throughout the beta.31 and beta.32 notes because every
measurement was taken on one installation, and a reader could reasonably come away
believing the number is baked in. It is not, and this file is the proof: the same
scenario is solved at several configured floors and every floor-derived quantity is
asserted to move with it.

The data path, end to end:

``CONF_BATTERY_MIN_SOC_PERCENT`` (options storage, selector range 0..max)
-> ``SourceConfig.battery_min_soc_percent``
-> ``static_reserve(...)`` -> ``BatteryReserve.configured_min_soc_percent``
-> ``BatteryLimits.energy_for_soc(...)`` -> ``floor_energy_kwh``
-> the physics table's clamp, ``build_reserve_reachable``, the enforced curve, the
   bridge, the terminal floor, the beta.32 survival/export floor, the published
   diagnostics, and ``device_cutoff_percent`` for the Stage-B discharge cutoff.

Nothing on that path holds a literal 20.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.alphaess_device import device_cutoff_percent
from custom_components.alpha_ems_manager.battery import build_limits, static_reserve
from custom_components.alpha_ems_manager.const import (
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
)
from custom_components.alpha_ems_manager.economic import (
    ForecastRisk,
    IntervalPrice,
    actionable_intervals,
    build_horizon,
    build_outcome,
    build_physics_table,
    economic_as_dict,
    edge_creditable_energy_kwh,
    edge_value_eur_per_kwh,
    select_bucket_kwh,
)
from custom_components.alpha_ems_manager.reserve import (
    build_reserve,
    build_reserve_reachable,
    uncertainty_margin,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

CAPACITY_KWH = 21.6
#: Measured evidence, so the beta.32 protections are all switched on. A protection
#: that is off cannot be caught holding the wrong floor.
RISK = ForecastRisk(mae_kwh=0.06, bias_kwh=-0.02, error_persistence=0.7)


def limits_for(capacity: float = CAPACITY_KWH, max_soc: float = 100.0):
    """Return a pack, refusing to proceed if the fixture itself is malformed."""
    limits, missing = build_limits(
        capacity_kwh=capacity,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        round_trip_efficiency_percent=90.0,
        max_soc_percent=max_soc,
    )
    assert missing is None
    return limits


def solve_at(min_soc_percent: float, *, stored: float = 18.0, intervals: int = 48):
    """Solve one fixed scenario with **only** the configured minimum SoC varied.

    Everything else -- capacity, powers, efficiency, demands, prices, stored energy,
    horizon length, measured forecast evidence -- is held constant, so any figure
    that moves between two calls moved because of the configured floor.
    """
    limits = limits_for()
    reserve_policy = static_reserve(min_soc_percent)
    floor = limits.energy_for_soc(reserve_policy.configured_min_soc_percent)

    demands = tuple(
        IntervalDemand(index=i, baseline_kwh=0.5, pv_kwh=0.0) for i in range(intervals)
    )
    prices = tuple(
        IntervalPrice(
            import_eur_kwh=0.10 + 0.20 * ((i // 8) % 2),
            export_eur_kwh=0.10 + 0.20 * ((i // 8) % 2) - 0.13,
        )
        for i in range(intervals)
    )
    bucket, rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    table = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)
    assert table is not None, f"no physics table at a {min_soc_percent} % floor"

    actionable = actionable_intervals(demands, prices)
    probe = build_reserve_reachable(
        limits=limits,
        floor_energy_kwh=floor,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    autonomy = build_reserve(limits=limits, floor_energy_kwh=floor, demands=demands)
    margin = uncertainty_margin(
        probe, mae_kwh_per_interval=0.06, usable_capacity_kwh=limits.capacity_kwh
    )
    enforced = build_reserve_reachable(
        limits=limits,
        floor_energy_kwh=floor + margin.total_dc_kwh,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    curve = tuple(
        entry.required_dc_kwh
        if entry.required_dc_kwh is not None
        else floor + margin.total_dc_kwh
        for entry in enforced.intervals
    )
    horizon = build_horizon(
        demands=demands, prices=prices, required_reserve_kwh=curve, table=table
    )
    outcome = build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=stored,
        terminal_floor_kwh=floor,
        floor_energy_kwh=floor,
        minimum_trade_gain_eur=0.20,
        allow_grid_charging=True,
        allow_battery_export=True,
        grid_charge_margin_eur_per_kwh=0.05,
        battery_throughput_cost_eur_per_kwh=0.0,
        edge_value_eur_per_kwh=edge_value_eur_per_kwh(
            horizon.prices[:actionable],
            discharge_efficiency=limits.discharge_efficiency,
        ),
        edge_creditable_kwh=edge_creditable_energy_kwh(
            ceiling_kwh=limits.energy_for_soc(100.0), forecast_surplus_kwh=0.0
        ),
        autonomy=tuple(entry.required_dc_kwh for entry in autonomy.intervals),
        reachability=enforced,
        uncertainty=margin,
        actionable_interval_count=actionable,
        forecast_risk=RISK,
        bucket_rule=rule,
    )
    published = economic_as_dict(
        outcome,
        execution_blocked_reason=ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
        reachability=enforced,
        uncertainty=margin,
        floor_energy_kwh=floor,
        stored_dc_kwh=stored,
        discharge_efficiency=limits.discharge_efficiency,
    )
    trajectory = [entry.start_energy_dc_kwh for entry in outcome.desired.intervals]
    last = outcome.desired.intervals[-1]
    trajectory.append(last.start_energy_dc_kwh + last.battery_delta_dc_kwh)
    return {
        "limits": limits,
        "table": table,
        "floor_kwh": floor,
        "outcome": outcome,
        "published": published,
        "reachability_now": enforced.required_now_dc_kwh,
        "lowest_dc": min(trajectory),
        "lowest_soc": limits.soc_for_energy(min(trajectory)),
    }


# ===========================================================================
# A. the three required examples
# ===========================================================================


@pytest.mark.parametrize(
    ("min_soc", "expected_floor_kwh"),
    [(20.0, 4.32), (10.0, 2.16), (0.0, 0.0)],
)
def test_the_configured_minimum_becomes_the_planner_hard_floor(
    min_soc: float, expected_floor_kwh: float
) -> None:
    """20 % -> 4.32 kWh, 10 % -> 2.16 kWh, 0 % -> no EMS reserve at all.

    ``energy_for_soc`` is ``percent / 100 * capacity`` and nothing else, so the
    floor is a pure function of the configured value on a 21.6 kWh pack.
    """
    solved = solve_at(min_soc)

    assert solved["floor_kwh"] == pytest.approx(expected_floor_kwh)
    # It reaches the solver as the terminal bound and the diagnostics as the
    # published hard floor -- the same number, from the same source.
    assert solved["published"]["planning"]["hard_floor_dc_kwh"] == pytest.approx(
        expected_floor_kwh, abs=0.01
    )
    # Quantised up to a bucket boundary, so within one bucket of the configured
    # figure and never below it.
    terminal = solved["outcome"].desired.terminal_floor_kwh
    assert expected_floor_kwh - solved["table"].bucket_kwh <= terminal
    assert terminal <= expected_floor_kwh + solved["table"].bucket_kwh


def test_a_zero_floor_adds_no_configured_reserve() -> None:
    """Zero is a legal setting, and it means the EMS reserves nothing.

    Only the inverter's and the BMS's own protection remain. Refusing zero would be
    an arbitrary restriction; silently substituting something for it would be worse.
    """
    solved = solve_at(0.0)

    assert solved["floor_kwh"] == 0.0
    assert solved["outcome"].desired.terminal_floor_kwh == pytest.approx(0.0)
    # The lattice can reach an empty pack: the clamp imposes no bound of its own.
    table = solved["table"]
    deepest = min(
        table.energy(move.target)
        for source in range(table.buckets + 1)
        for move in table.moves[source]
        if move.target < source
    )
    assert deepest == pytest.approx(0.0)
    # And the Stage-B discharge cutoff falls to the vendor helper's own range
    # minimum rather than to anything the EMS chose.
    assert device_cutoff_percent(0.0) < device_cutoff_percent(10.0)


# ===========================================================================
# B. the required comparison: 20 against 10, one scenario, every figure
# ===========================================================================


def test_lowering_the_configured_floor_lowers_every_floor_derived_figure() -> None:
    """The same scenario at 20 % and at 10 %, and nothing may stay behind.

    A 10 percentage-point change on a 21.6 kWh pack is exactly 2.16 kWh, so each
    quantity built on the floor must move by that amount -- not by some of it, and
    not at all.
    """
    step = 21.6 * 0.10
    twenty = solve_at(20.0)
    ten = solve_at(10.0)

    assert twenty["floor_kwh"] - ten["floor_kwh"] == pytest.approx(step)
    # Physical reachability: ``required = floor + deficit``, same demands, so the
    # whole curve shifts by exactly the floor change.
    assert twenty["reachability_now"] - ten["reachability_now"] == pytest.approx(step)
    # The enforced head the solver obeys, and the physical head beneath it.
    assert twenty["outcome"].physical_reserve_head_kwh - ten[
        "outcome"
    ].physical_reserve_head_kwh == pytest.approx(step, abs=0.3)
    assert twenty["outcome"].enforced_reserve_head_kwh - ten[
        "outcome"
    ].enforced_reserve_head_kwh == pytest.approx(step, abs=0.3)
    # Published diagnostics.
    assert twenty["published"]["planning"]["hard_floor_dc_kwh"] - ten["published"][
        "planning"
    ]["hard_floor_dc_kwh"] == pytest.approx(step, abs=0.01)
    # The surplus is measured *above* the requirement, so it moves the other way by
    # the same amount -- a lower floor frees exactly what it stopped reserving.
    assert ten["published"]["planning"]["exportable_surplus_dc_kwh"] - twenty[
        "published"
    ]["planning"]["exportable_surplus_dc_kwh"] == pytest.approx(step, abs=0.01)
    # And the Stage-B discharge cutoff written to the inverter.
    assert device_cutoff_percent(20.0) - device_cutoff_percent(10.0) == 10


def test_no_beta_32_protection_silently_keeps_the_old_floor() -> None:
    """The export permission's base is the configured floor, not a remembered 20 %.

    ``economic_survival_to_refill_kwh`` -- published as ``export_floor_dc_kwh`` --
    is ``floor + sum(upper_net_demand) / eta``. If the floor term were pinned, the
    protection would keep behaving as though the user had never lowered the
    setting, which is precisely the failure this test exists to exclude.
    """
    step = 21.6 * 0.10
    twenty = solve_at(20.0)
    ten = solve_at(10.0)

    floors_twenty = twenty["outcome"].export_floor_kwh
    floors_ten = ten["outcome"].export_floor_kwh
    assert floors_twenty and floors_ten
    assert len(floors_twenty) == len(floors_ten)
    # Every interval, not merely the head.
    for index, (high, low) in enumerate(zip(floors_twenty, floors_ten, strict=True)):
        assert high - low == pytest.approx(step), index

    # The anti-churn extension is a bucket plus measured demand and carries no
    # floor term at all, so it is identical -- which is the correct answer, and is
    # asserted so that adding a floor term later is a visible decision.
    assert twenty["outcome"].anti_churn_buffer_kwh == pytest.approx(
        ten["outcome"].anti_churn_buffer_kwh
    )


def test_the_plan_actually_spends_below_the_old_floor_when_it_is_lowered() -> None:
    """The decisive behavioural check: the pack genuinely goes deeper.

    Every assertion above is about a *number* the planner holds. This one is about
    what the planner then does with it -- because a floor that moved while the
    trajectory did not would mean something else was binding.
    """
    twenty = solve_at(20.0)
    ten = solve_at(10.0)
    zero = solve_at(0.0)

    assert twenty["lowest_soc"] > 20.0
    assert ten["lowest_soc"] < 20.0, "a 10 % floor must permit going below 20 %"
    assert ten["lowest_soc"] > 10.0
    assert zero["lowest_soc"] < ten["lowest_soc"]
    # And none of them crosses its own floor.
    for solved in (twenty, ten, zero):
        assert solved["lowest_dc"] >= solved["floor_kwh"] - 1e-9
        assert solved["outcome"].desired.violation_kwh == pytest.approx(0.0)


# ===========================================================================
# C. the defect this file found: a high floor disabled the whole optimiser
# ===========================================================================


@pytest.mark.parametrize("min_soc", [0.0, 10.0, 20.0, 49.0, 50.0, 60.0, 80.0, 95.0])
def test_every_configured_floor_still_builds_a_physics_table(min_soc: float) -> None:
    """**The regression this file was written to catch.**

    ``build_physics_table`` calibrated its conversion ratios at a hardcoded
    ``soc_percent=50.0``. With a configured minimum at or above 50 % the probe
    state *is* the floor, so the clamp reduced the discharge reading to zero,
    ``discharge_ratio`` was 0, and the function returned ``None`` -- taking the
    entire economic plan with it, silently, under a comment asserting that
    ``build_limits`` precluded the case.

    Measured before the fix on the reference pack: a 49 % floor built a table and a
    50 % floor did not. The probe now sits at the midpoint of floor and ceiling.
    """
    limits = limits_for()
    floor = limits.energy_for_soc(min_soc)
    bucket, _rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    table = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)

    assert table is not None, f"a {min_soc} % floor must still be solvable"
    # The ratios are pure efficiency constants and must not vary with the probe.
    assert table.charge_dc_per_ac == pytest.approx(0.948683, abs=1e-6)
    assert table.discharge_dc_per_ac == pytest.approx(1.054093, abs=1e-6)


def test_a_floor_at_the_ceiling_is_the_one_case_with_no_lattice() -> None:
    """Not an accident of the probe -- a named impossibility.

    With the floor at the ceiling there is no room to charge and none to discharge,
    so there is genuinely nothing to solve. That must remain the *only* ``None``.
    """
    limits = limits_for()
    full = limits.energy_for_soc(limits.max_soc_percent)
    bucket, _rule = select_bucket_kwh(limits, floor_energy_kwh=full)

    assert build_physics_table(limits, floor_energy_kwh=full, bucket_kwh=bucket) is None
    # One bucket of room is enough.
    assert (
        build_physics_table(limits, floor_energy_kwh=full - 1.0, bucket_kwh=bucket)
        is not None
    )


def test_a_high_floor_keeps_the_aligned_bucket_rather_than_the_fallback() -> None:
    """The second symptom of the same defect, and it degraded the lattice.

    ``select_bucket_kwh`` searches for a bucket that divides the maximum-power
    quarter exactly, and it evaluates each candidate by *building a table*. With
    the calibration failing at a high floor every candidate was rejected, so the
    search silently fell back to the constant bucket -- a five per cent loss of
    representable power in both directions, on top of having no plan at all.
    """
    limits = limits_for()
    aligned, rule = select_bucket_kwh(
        limits, floor_energy_kwh=limits.energy_for_soc(60.0)
    )
    reference, reference_rule = select_bucket_kwh(
        limits, floor_energy_kwh=limits.energy_for_soc(20.0)
    )

    assert rule == reference_rule
    assert aligned == pytest.approx(reference)


# ===========================================================================
# D. no literal twenty on the path
# ===========================================================================


def test_twenty_percent_is_a_default_and_not_a_rule() -> None:
    """It is the option's default, which a user is free to change.

    Asserted explicitly because the release notes quote 20 % throughout: that is
    the maintainer's configured value during development, and a reader should be
    able to find the one place it is actually written down.
    """
    assert DEFAULT_BATTERY_MIN_SOC_PERCENT == 20.0
    # And nothing downstream re-imposes it: the reserve carries whatever it is given.
    for value in (0.0, 7.5, 10.0, 20.0, 33.3, 60.0):
        assert static_reserve(value).configured_min_soc_percent == pytest.approx(value)


def test_a_non_round_pack_gets_a_proportional_floor_too() -> None:
    """No coincidence of the 21.6 kWh reference pack.

    13.7 kWh at 12 % is 1.644 kWh, and the maximum state of charge does not enter
    the floor at all -- it bounds the *ceiling*.
    """
    limits = limits_for(capacity=13.7, max_soc=95.0)

    assert limits.energy_for_soc(12.0) == pytest.approx(1.644)
    assert limits.energy_for_soc(0.0) == pytest.approx(0.0)
    assert limits.energy_for_soc(20.0) == pytest.approx(2.74)

    bucket, _rule = select_bucket_kwh(
        limits, floor_energy_kwh=limits.energy_for_soc(12.0)
    )
    table = build_physics_table(
        limits, floor_energy_kwh=limits.energy_for_soc(12.0), bucket_kwh=bucket
    )
    assert table is not None
    deepest = min(
        table.energy(move.target)
        for source in range(table.buckets + 1)
        for move in table.moves[source]
        if move.target < source
    )
    assert deepest >= 1.644 - bucket - 1e-9
