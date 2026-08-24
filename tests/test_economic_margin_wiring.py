"""The grid-charge margin actually reaching the solver -- the beta.21 fix.

**The defect this module exists to prevent from returning.** The margin was a
configurable option that changed nothing. `solve` accepted it and used it,
`build_outcome` forwarded it, the coordinator read it out of the config entry --
and the executor function between them had no such parameter, so the value was
dropped on the floor and the solve ran at the `0.0` default.

Stock installs were unaffected because the default *is* zero, which is exactly
why it survived a full test suite: every existing margin test calls `solve`
directly, so all of them passed while the setting did nothing in production.

That shape is the lesson. `async_add_executor_job` passes **positionally**, so a
parameter that is not in the signature is a setting that silently does nothing,
and a parameter in the wrong position is worse -- it would have applied the trade
gain as a margin and nobody would have seen it. The tests here are therefore
about the *path*, not about the arithmetic: the arithmetic is covered in
`test_economic_margin.py` and needs no repetition.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from custom_components.alpha_ems_manager import coordinator as coordinator_module
from custom_components.alpha_ems_manager.battery import build_limits
from custom_components.alpha_ems_manager.economic import IntervalPrice
from custom_components.alpha_ems_manager.simulation import IntervalDemand

CAPACITY_KWH = 22.0
POWER_KW = 10.0
FLOOR_PERCENT = 20.0
COUNT = 12


def limits_for():
    """Return the reference installation's limits."""
    limits, reason = build_limits(
        capacity_kwh=CAPACITY_KWH,
        max_charge_kw=POWER_KW,
        max_discharge_kw=POWER_KW,
        round_trip_efficiency_percent=90.0,
    )
    assert reason is None
    return limits


LIMITS = limits_for()
FLOOR = LIMITS.energy_for_soc(FLOOR_PERCENT)
DEMANDS = tuple(
    IntervalDemand(index=index, baseline_kwh=0.25, pv_kwh=0.0) for index in range(COUNT)
)
RESERVE = tuple([FLOOR] * COUNT)


def prices_for(spread: float):
    """Return a cheap first half and a dearer second half.

    ``spread`` is the gross arbitrage per kWh before the round trip takes its
    cut, so a small spread is a genuinely thin trade rather than a contrived one.
    """
    return tuple(
        IntervalPrice(
            import_eur_kwh=0.20 if index < 6 else 0.20 + spread,
            export_eur_kwh=(0.20 if index < 6 else 0.20 + spread) - 0.02,
        )
        for index in range(COUNT)
    )


def solved(*, spread: float, margin: float, gain: float = 0.0):
    """Return an outcome through the **real executor path**, not through ``solve``.

    Going through ``_solve_economic`` is the whole point: it is the layer that
    dropped the value, so a test that called ``solve`` would pass against the bug.
    """
    return coordinator_module._solve_economic(
        LIMITS,
        FLOOR,
        FLOOR + 1.0,
        FLOOR,
        DEMANDS,
        prices_for(spread),
        RESERVE,
        0.0,
        gain,
        margin,
        True,
        True,
    )


def bought_kwh(outcome) -> float:
    """Return the grid energy the plan buys, marginal to doing nothing."""
    return sum(
        max(0.0, entry.marginal_grid_import_kwh) for entry in outcome.desired.intervals
    )


def exported_kwh(outcome) -> float:
    """Return the grid energy the plan exports, marginal to doing nothing."""
    return sum(
        max(0.0, entry.marginal_grid_export_kwh) for entry in outcome.desired.intervals
    )


# ===========================================================================
# 4. value propagation -- the configured figure reaches the real solve
# ===========================================================================


def test_the_configured_margin_reaches_the_solve_call(monkeypatch) -> None:
    """**The regression test for the bug itself.**

    Asserted at ``build_outcome`` rather than at ``solve`` because that is the
    boundary the value was lost crossing. A spy is used rather than an assertion
    on behaviour so the failure names the cause instead of a symptom.
    """
    seen: dict[str, object] = {}
    real = coordinator_module.build_outcome

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(coordinator_module, "build_outcome", spy)

    solved(spread=0.30, margin=0.25, gain=0.10)

    assert seen["grid_charge_margin_eur_per_kwh"] == 0.25
    # And in its own slot: a positional mix-up would have swapped these two and
    # applied the trade gain as a per-kWh margin.
    assert seen["minimum_trade_gain_eur"] == 0.10


def test_the_executor_call_passes_every_parameter_it_declares() -> None:
    """The structural guard, because the call is positional.

    ``async_add_executor_job`` takes ``*args``, so an argument dropped at the call
    site is not a ``TypeError`` waiting to happen at import time -- it is a
    silently shifted list. Nothing but a count check catches that, and the count
    is what was wrong.
    """
    declared = list(inspect.signature(coordinator_module._solve_economic).parameters)
    source = inspect.getsource(
        coordinator_module.AlphaEmsCoordinator._async_economic_outcome
    )
    tree = ast.parse(inspect.cleandoc(source))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(
            isinstance(arg, ast.Name) and arg.id == "_solve_economic"
            for arg in node.args
        )
    ]
    assert len(calls) == 1, "there must be exactly one place the solve is dispatched"
    # The first argument is the function itself; the rest are its parameters.
    passed = len(calls[0].args) - 1
    assert passed == len(declared), (passed, len(declared), declared)


def test_the_margin_is_read_from_the_configuration() -> None:
    """The other end of the path: the option becomes a config field."""
    from custom_components.alpha_ems_manager.const import (
        CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH,
        DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    )

    fields = coordinator_module.SourceConfig.__dataclass_fields__
    assert "grid_charge_margin_eur_per_kwh" in fields
    assert CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH == "grid_charge_margin_eur_per_kwh"
    assert DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH == 0.0


# ===========================================================================
# 1. default inertness -- zero must change nothing
# ===========================================================================


@pytest.mark.parametrize("spread", [0.06, 0.12, 0.30, 0.60])
def test_a_zero_margin_decides_exactly_what_it_decided_before(spread: float) -> None:
    """**The compatibility half of the fix, and the one that matters most.**

    Wiring a setting through is only safe if its default is genuinely inert. Zero
    is the default, so every installation that has never touched the option must
    plan identically to beta.20 -- asserted on the whole interval trajectory and
    the cost, not on a summary.
    """
    wired = solved(spread=spread, margin=0.0)
    # ``solve`` defaults the margin to 0.0, so omitting it is the pre-fix path.
    from custom_components.alpha_ems_manager.economic import (
        build_horizon,
        build_outcome,
        build_physics_table,
        select_bucket_kwh,
    )

    bucket, rule = select_bucket_kwh(LIMITS, floor_energy_kwh=FLOOR)
    table = build_physics_table(LIMITS, floor_energy_kwh=FLOOR, bucket_kwh=bucket)
    horizon = build_horizon(
        demands=DEMANDS,
        prices=prices_for(spread),
        required_reserve_kwh=RESERVE,
        table=table,
    )
    unwired = build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=FLOOR + 1.0,
        terminal_floor_kwh=FLOOR,
        floor_energy_kwh=FLOOR,
        minimum_trade_gain_eur=0.0,
        allow_grid_charging=True,
        allow_battery_export=True,
        reserve_above_capacity_kwh=0.0,
        table_ms=0.0,
        bucket_rule=rule,
    )

    assert wired.desired.cost_eur == pytest.approx(unwired.desired.cost_eur)
    assert wired.desired.grid_charge_margin_eur == 0.0
    assert [entry.action for entry in wired.desired.intervals] == [
        entry.action for entry in unwired.desired.intervals
    ]
    assert [
        entry.battery_charge_ac_kwh for entry in wired.desired.intervals
    ] == pytest.approx(
        [entry.battery_charge_ac_kwh for entry in unwired.desired.intervals]
    )
    assert [
        entry.battery_discharge_ac_kwh for entry in wired.desired.intervals
    ] == pytest.approx(
        [entry.battery_discharge_ac_kwh for entry in unwired.desired.intervals]
    )


# ===========================================================================
# 2 and 3. a thin trade is refused, a fat one is not
# ===========================================================================


def test_a_thin_trade_survives_a_zero_margin_and_dies_at_ten_cents() -> None:
    """The acceptance behaviour, through the wiring rather than through ``solve``.

    A twelve-cent gross spread does not clear ten cents per kWh once the round
    trip has taken its share, so the same opportunity flips from accepted to
    refused on the strength of the setting alone.
    """
    free = solved(spread=0.12, margin=0.0)
    charged = solved(spread=0.12, margin=0.10)

    assert bought_kwh(free) > 1.0
    assert bought_kwh(charged) == pytest.approx(0.0, abs=1e-9)


def test_a_clearly_profitable_trade_still_happens_with_a_margin() -> None:
    """A margin is a threshold, not a prohibition.

    The failure this guards is a margin applied to the whole plan rather than to
    marginal grid-caused kWh, which would suppress good trades along with thin
    ones.
    """
    for margin in (0.10, 0.25):
        outcome = solved(spread=0.60, margin=margin)
        assert bought_kwh(outcome) > 1.0, margin
        assert outcome.desired.grid_charge_margin_eur > 0.0, margin


def test_raising_the_margin_can_only_reduce_what_is_bought() -> None:
    """Monotone in the setting, swept, because a threshold that is not is a bug."""
    previous = None
    for tenths in range(0, 8):
        outcome = solved(spread=0.30, margin=tenths / 10.0)
        bought = bought_kwh(outcome)
        if previous is not None:
            assert bought <= previous + 1e-9, (tenths, bought, previous)
        previous = bought


# ===========================================================================
# 5. the margin is a *charging* threshold and touches nothing else
# ===========================================================================


def test_the_margin_does_not_alter_export_allocation() -> None:
    """It is charged on marginal grid-caused *charging*. Selling is not charging.

    Asserted with a pack that starts with energy to sell and no reason to buy, so
    any change in the export schedule would have to come from the margin.
    """
    outcomes = []
    for margin in (0.0, 0.10, 0.50):
        outcome = coordinator_module._solve_economic(
            LIMITS,
            FLOOR,
            CAPACITY_KWH,
            FLOOR,
            DEMANDS,
            prices_for(0.30),
            RESERVE,
            0.0,
            0.0,
            margin,
            False,
            True,
        )
        outcomes.append(outcome)

    baseline = outcomes[0].desired
    for outcome in outcomes[1:]:
        plan = outcome.desired
        assert plan.cost_eur == pytest.approx(baseline.cost_eur)
        assert [entry.action for entry in plan.intervals] == [
            entry.action for entry in baseline.intervals
        ]
        assert [
            entry.battery_discharge_ac_kwh for entry in plan.intervals
        ] == pytest.approx(
            [entry.battery_discharge_ac_kwh for entry in baseline.intervals]
        )
        assert exported_kwh(outcome) == pytest.approx(exported_kwh(outcomes[0]))


# ===========================================================================
# 7. mutation -- resetting the margin must be caught
# ===========================================================================


def test_dropping_the_margin_back_to_zero_is_caught(monkeypatch) -> None:
    """The mutation: forward ``0.0`` instead of the configured figure.

    This is the bug, written as a mutation. It reproduces the exact pre-fix
    behaviour -- a setting the user changed and the plan ignored -- and the
    assertion is that the refusal above stops being a refusal.
    """
    real = coordinator_module.build_outcome

    def reset(**kwargs):
        kwargs["grid_charge_margin_eur_per_kwh"] = 0.0
        return real(**kwargs)

    monkeypatch.setattr(coordinator_module, "build_outcome", reset)

    mutated = solved(spread=0.12, margin=0.10)

    # Under the mutation the thin trade goes ahead, which is precisely what the
    # released beta.20 did with a configured margin of ten cents.
    assert bought_kwh(mutated) > 1.0
    assert mutated.desired.grid_charge_margin_eur == 0.0


def test_swapping_the_two_economic_settings_is_caught(monkeypatch) -> None:
    """The subtler mutation: right values, wrong slots.

    The call is positional, so this is the failure mode a count check cannot see.
    A fixed fee charged per kWh and a per-kWh margin charged once are different
    quantities, and confusing them is silent.
    """
    real = coordinator_module.build_outcome

    def swap(**kwargs):
        kwargs["minimum_trade_gain_eur"], kwargs["grid_charge_margin_eur_per_kwh"] = (
            kwargs["grid_charge_margin_eur_per_kwh"],
            kwargs["minimum_trade_gain_eur"],
        )
        return real(**kwargs)

    honest = solved(spread=0.12, margin=0.10, gain=0.0)
    monkeypatch.setattr(coordinator_module, "build_outcome", swap)
    swapped = solved(spread=0.12, margin=0.10, gain=0.0)

    assert bought_kwh(honest) == pytest.approx(0.0, abs=1e-9)
    assert bought_kwh(swapped) > 1.0
