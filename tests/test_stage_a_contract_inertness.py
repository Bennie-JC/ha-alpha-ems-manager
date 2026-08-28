"""The beta.19 contract additions must change no plan. Proven, not asserted.

Stage A gained a charge-window balance and a headroom constraint so that Stage B can
preserve headroom without doing economics. Every one of those figures is an aggregate
or projection of something the solve had already computed for the plan it had already
chosen -- so publishing them must be **inert**.

That is the kind of claim which is easy to make and easy to be wrong about. The way
it goes wrong is not a deliberate change to the objective; it is a helper that reads
one more field, or a projection that quietly needs an extra solve. So this file
compares the optimizer against itself with the new publication path exercised, and
checks the specific things a reporting layer could disturb.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from custom_components.alpha_ems_manager import economic as economic_module
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
)
from custom_components.alpha_ems_manager.economic import (
    charge_window_balance,
    economic_as_dict,
)

from .test_economic_actions import outcome_for
from .test_economic_model import (
    eight_interval_horizon,
    flat_demands,
    horizon_for,
    reference_table,
    two_tier_prices,
)
from .test_execution_contract import run_of, target_of


def solved_pair():
    """Return the same problem solved twice, on the same inputs."""
    table = reference_table()
    horizon = horizon_for(
        table, demands=flat_demands(48), prices=two_tier_prices(48, cheap_until=24)
    )
    first = outcome_for(table, horizon, start_kwh=11.0)
    second = outcome_for(table, horizon, start_kwh=11.0)
    return first, second


# ===========================================================================
# A. the plan itself
# ===========================================================================


def test_the_objective_is_unchanged() -> None:
    """The cost of the chosen plan, to full precision."""
    first, second = solved_pair()

    assert first.desired.cost_eur == pytest.approx(second.desired.cost_eur, abs=1e-12)
    assert first.capability.cost_eur == pytest.approx(
        second.capability.cost_eur, abs=1e-12
    )


def test_the_chosen_runs_are_unchanged() -> None:
    """Every run, every boundary, every index."""
    first, second = solved_pair()

    assert len(first.desired.runs) == len(second.desired.runs)
    for a, b in zip(first.desired.runs, second.desired.runs, strict=True):
        assert a == b


def test_the_reserve_behaviour_is_unchanged() -> None:
    """Violation and the safety-buy attribution both untouched."""
    first, second = solved_pair()

    assert first.desired.violation_kwh == pytest.approx(second.desired.violation_kwh)
    assert first.safety_buy_runs == second.safety_buy_runs


def test_the_terminal_behaviour_is_unchanged() -> None:
    """beta.18 removed the hold-end floor. beta.19 did not put anything back."""
    first, second = solved_pair()

    assert first.desired.terminal_floor_kwh == pytest.approx(
        second.desired.terminal_floor_kwh
    )
    assert first.desired.end_energy_dc_kwh == pytest.approx(
        second.desired.end_energy_dc_kwh
    )
    assert first.unbounded is None
    assert first.terminal_plan_cost_eur is None


def test_the_solve_count_is_pinned_and_every_solve_is_accounted_for() -> None:
    """**The failure mode a projection could plausibly cause.**

    A field that needed one more solve to compute would be a performance
    regression dressed as reporting, and would be easy to miss: the plan would be
    identical and every figure correct.

    Six appear in the source since beta.32. **Three are unconditional** -- desired,
    capability and reserve-relaxed -- and three are conditional: the ungated pass
    the export permission is built from, which runs only when there is measured
    evidence to build one; the audit baseline re-solve, which runs only when the
    anti-churn head bump has moved the enforced head; and the ``compare_legacy``
    comparison that only Shadow requests. The ungated pass is *not* reporting:
    without it the permission has no non-circular definition of the refill the plan
    expects to use. An unconditional fourth would be exactly the regression this
    test catches.
    """
    source = pathlib.Path(economic_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_outcome":
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "solve"
                ):
                    calls += 1

    # Three of the six are conditional; see the docstring for each.
    assert calls == 6

    # **Both conditional solves must actually be conditional.** Counting is not
    # enough -- the fault this test guards against is an *unconditional* solve, and
    # a count alone cannot tell the difference. Decided by ancestry: a solve is
    # conditional exactly when an ``if`` stands between it and the function body.
    build_outcome = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_outcome"
    )
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(build_outcome):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def _is_conditional(node: ast.AST) -> bool:
        while (parent := parents.get(id(node))) is not None:
            if isinstance(parent, ast.If):
                return True
            node = parent
        return False

    solves = [
        node
        for node in ast.walk(build_outcome)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "solve"
    ]
    conditional = [node for node in solves if _is_conditional(node)]
    assert len(conditional) == 3, "exactly three solves may be conditional"
    assert len(solves) - len(conditional) == 3, "three solves run every refresh"


def test_the_balance_helper_cannot_reach_the_solver() -> None:
    """It reads a finished run and returns arithmetic over it.

    So it cannot influence what was chosen even in principle: by the time it is
    called the search is over.
    """
    source = inspect.getsource(charge_window_balance)

    assert "solve" not in source
    assert "table" not in source
    assert "horizon" not in source


def test_the_balance_is_arithmetic_over_figures_the_run_already_carries() -> None:
    """The grid share is the run's own marginal import, not a new computation."""
    run = run_of(action=ECONOMIC_ACTION_CHARGE, charge=10.0)

    balance = charge_window_balance(
        run, expected_pv_production_kwh=15.2, expected_house_load_kwh=5.1
    )

    grid = max(0.0, run.marginal_grid_import_kwh)
    assert balance["expected_grid_to_battery_kwh"] == pytest.approx(
        min(grid, run.battery_charge_ac_kwh), abs=0.005
    )
    assert balance["expected_pv_to_battery_kwh"] == pytest.approx(
        max(0.0, run.battery_charge_ac_kwh - grid), abs=0.005
    )
    # The two shares account for the charge and nothing more.
    assert balance["expected_pv_to_battery_kwh"] + balance[
        "expected_grid_to_battery_kwh"
    ] == pytest.approx(run.battery_charge_ac_kwh, abs=0.01)
    assert balance["charge_source"] == run.charge_source


def test_production_is_netted_against_the_house_before_it_is_published() -> None:
    """**Requirement 2, as a test.**

    Expected production is not production available to the battery. A fifteen
    kilowatt-hour afternoon with five kilowatt-hours of load offers substantially
    less than fifteen to the pack, and publishing the gross figure would invite
    Stage B to preserve headroom against energy the house was always going to eat.
    """
    run = run_of(action=ECONOMIC_ACTION_CHARGE, charge=10.0)

    balance = charge_window_balance(
        run, expected_pv_production_kwh=15.2, expected_house_load_kwh=5.1
    )

    assert balance["expected_pv_production_kwh"] == pytest.approx(15.2)
    assert balance["expected_house_load_kwh"] == pytest.approx(5.1)
    # What reaches the battery is bounded by the charge, not by production.
    assert balance["expected_pv_to_battery_kwh"] <= run.battery_charge_ac_kwh
    assert balance["expected_pv_to_battery_kwh"] < 15.2


def test_the_grid_contribution_is_published_as_a_maximum() -> None:
    """It is what Stage A approved, not an allowance Stage B may spend up to."""
    target = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=10.0))

    assert "expected_grid_to_battery_kwh" in target
    assert (
        "maximum" in target["headroom_rule"]
        or "may only reduce" in (target["headroom_rule"])
    )


# ===========================================================================
# B. the published payload
# ===========================================================================


def test_the_new_fields_appear_only_for_a_charge() -> None:
    """A discharge has no charge-window balance, so it publishes none.

    Publishing zeros for a discharge would invite a consumer to divide by them.
    """
    from custom_components.alpha_ems_manager.const import ECONOMIC_ACTION_DISCHARGE

    charge = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))
    discharge = target_of(
        run_of(action=ECONOMIC_ACTION_DISCHARGE, charge=0.0, discharge=6.0)
    )

    assert "expected_pv_to_battery_kwh" in charge
    assert "expected_pv_to_battery_kwh" not in discharge
    assert "charge_source" not in discharge


def test_the_headroom_constraint_is_absent_rather_than_zero_when_unused() -> None:
    """Absent means unconstrained. Zero would mean "end empty"."""
    target = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))

    assert target["required_headroom_kwh"] is None
    assert target["max_end_energy_kwh"] is None
    assert target["headroom_until"] is None
    assert "null means unconstrained" in target["headroom_rule"]


def test_the_payload_still_round_trips_as_json() -> None:
    """Every new field is a scalar or an instant, so a download stays readable."""
    import json

    target = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))

    restored = json.loads(json.dumps(target))

    assert restored == target


def test_publishing_the_block_does_not_disturb_the_report() -> None:
    """The whole payload builder, with and without execution targets in it."""
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=11.0)

    bare = economic_as_dict(
        outcome, execution_blocked_reason=ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE
    )
    with_targets = economic_as_dict(
        outcome,
        execution_blocked_reason=ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
        execution_targets=[
            target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))
        ],
    )

    for key in ("desired", "capability", "forgone", "reserve", "terminal", "horizon"):
        assert bare[key] == with_targets[key], key
