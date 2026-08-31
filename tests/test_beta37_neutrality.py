"""beta.37: the instrumentation changes nothing, proven at four layers.

**Gate 3, and the load-bearing invariant of the release.** An observability release
that moved a decision would be worse than no release: the whole value of the number
is that it describes the plan the optimiser actually chose.

Four layers, because each catches something the others cannot:

1. **AST.** Which functions may read the value curve at all. Names them, so a solve
   growing a call is a failure rather than a rename.
2. **Plan equality.** The comparator correction touches a published figure, so the
   selected plan is compared structurally across it -- actions, energies, campaign
   boundaries, and the two scalars that are allowed to differ named explicitly.
3. **Execution-target equality.** The level Stage B actually consumes, byte for byte
   including every ``quarter_schedule`` row. This layer did not exist before beta.37.
4. **Solve count.** The release must add no dynamic-programming solve, and the count
   is published, so it is asserted rather than argued.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.economic import (
    build_outcome,
    economic_value_summary,
    hold_cost,
)

from .beta34_shape import solve_at

HEAD, END, STORED = 28, 96, 8.294


# ===========================================================================
# 1. the reader allowlist
# ===========================================================================


def test_only_publishing_functions_read_the_economic_value() -> None:
    """**Who may call the summariser, named rather than counted.**

    The sibling test in ``test_beta35_stored_value`` guards the value curve itself.
    This one guards the beta.37 layer above it: ``economic_value_summary`` is a pure
    function that takes an outcome and returns a payload, and the only things allowed
    to call it are the coordinator's own publish-only readers.

    *Mutation: call it from ``solve``, ``build_outcome``, ``_walk_forward``,
    ``_decide`` or ``evaluate`` and this fails.*
    """
    package = pathlib.Path("custom_components/alpha_ems_manager")
    readers: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for outer in ast.walk(tree):
            if not isinstance(outer, ast.FunctionDef):
                continue
            for node in ast.walk(outer):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else None
                if isinstance(func, ast.Name):
                    name = func.id
                if name in {"economic_value_summary", "economic_value"}:
                    readers.append(f"{path.name}:{outer.name}")

    permitted = {
        # The one derivation, and the two coordinator readers that wrap it.
        "coordinator.py:_economic_value_for",
        "coordinator.py:economic_value",
        "coordinator.py:_economic_value_evidence",
        # The entity's guarded reader. Its whole body is a try/except around one
        # call, so a fault in the summariser costs one attribute and not the
        # refresh.
        "sensor.py:_economic_value_payload",
    }
    assert set(readers) <= permitted, sorted(set(readers) - permitted)
    forbidden = {
        "solve",
        "_walk_forward",
        "build_outcome",
        "_decide",
        "evaluate",
        "_execution_targets",
        "_stage_b_intent",
        "_dispatch_setpoint",
    }
    assert not (forbidden & {entry.split(":")[1] for entry in readers})


def test_the_summariser_takes_an_outcome_and_returns_a_payload() -> None:
    """Structurally incapable of changing anything: no state, no side effect.

    Called twice on the same outcome it returns equal payloads, and the outcome it
    was handed is unchanged -- which is what "read-only" has to mean for a frozen
    dataclass whose fields are all public.
    """
    outcome = solve_at(head=HEAD, end=END, stored=STORED).outcome
    before = (
        outcome.desired.cost_eur,
        outcome.desired.objective_eur,
        tuple(entry.action for entry in outcome.desired.intervals),
    )

    first = economic_value_summary(outcome, today_interval_count=96)
    second = economic_value_summary(outcome, today_interval_count=96)

    assert first == second
    assert (
        outcome.desired.cost_eur,
        outcome.desired.objective_eur,
        tuple(entry.action for entry in outcome.desired.intervals),
    ) == before


# ===========================================================================
# 2. the comparator correction moved a report and not a plan
# ===========================================================================


def test_the_comparator_correction_moves_only_the_comparator() -> None:
    """**The one change in beta.37 that touches a published number.**

    ``hold_cost`` now receives ``ambient_self_consumption``, so the passive baseline
    is priced under the same model as the plan it is compared against. On an
    installation whose inverter serves house load from the battery unbidden, the old
    baseline never discharged to the house while the plan modelled that it did -- and
    the advantage therefore credited the battery for a saving the inverter would have
    delivered while idle.

    **The two paths are the release boundary, not a settings toggle.** An earlier
    draft of this test compared a solve with ``ambient_self_consumption=True`` against
    one with it ``False`` and asserted the plan was unchanged. That was wrong and the
    test caught it: the flag is a *model input to the solve*, so turning it off
    legitimately changes the plan -- measured, ``cost_eur`` moved from -0.157 to
    +0.092. What beta.37 changed is narrower: whether ``hold_cost`` is told about a
    flag the solve already had.

    So the beta.36 path is reconstructed by patching ``hold_cost`` to discard the
    argument, exactly as the old signature did, and the same horizon is solved twice.
    The objective never reads ``hold_cost_eur``, so the plan must be identical --
    asserted structurally, on actions, both energy lists and the campaign boundaries.

    *Mutation: remove ``ambient_self_consumption`` from ``hold_cost`` and the
    ``hold_cost_eur`` assertion fails; make the objective read it and the rest do.*
    """
    from unittest.mock import patch

    from custom_components.alpha_ems_manager import economic as economic_module

    real = economic_module.hold_cost

    def beta36_hold_cost(**kwargs):
        """The old signature: the flag existed nowhere and defaulted to off."""
        kwargs.pop("ambient_self_consumption", None)
        kwargs.pop("hard_floor_kwh", None)
        return real(**kwargs)

    with patch.object(economic_module, "hold_cost", beta36_hold_cost):
        before = solve_at(head=HEAD, end=END, stored=STORED).outcome.desired
    after = solve_at(head=HEAD, end=END, stored=STORED).outcome.desired

    # The comparator moved, and downwards: the baseline now gets the saving.
    assert after.hold_cost_eur < before.hold_cost_eur
    assert before.hold_cost_eur - after.hold_cost_eur > 0.01, "and materially"

    # And nothing about the chosen plan did.
    assert after.cost_eur == pytest.approx(before.cost_eur)
    assert after.objective_eur == pytest.approx(before.objective_eur)
    assert [e.action for e in after.intervals] == [e.action for e in before.intervals]
    assert [e.battery_charge_ac_kwh for e in after.intervals] == pytest.approx(
        [e.battery_charge_ac_kwh for e in before.intervals]
    )
    assert [e.battery_discharge_ac_kwh for e in after.intervals] == pytest.approx(
        [e.battery_discharge_ac_kwh for e in before.intervals]
    )
    assert [(c.direction, c.start_index, c.end_index) for c in after.campaigns] == [
        (c.direction, c.start_index, c.end_index) for c in before.campaigns
    ]
    # And the switching fee, which is the scalar a wrong comparison inverts.
    assert after.switching_cost_eur == pytest.approx(before.switching_cost_eur)


def test_the_comparator_is_priced_under_the_plans_own_model() -> None:
    """Asserted on ``hold_cost`` directly, so the argument cannot be lost in a call."""
    solved = solve_at(head=HEAD, end=END, stored=STORED)
    table, horizon = solved.table, solved.outcome.horizon
    start = solved.outcome.desired.intervals[0].start_energy_dc_kwh

    absorb_only = hold_cost(horizon=horizon, table=table, start_energy_kwh=start)
    with_self_use = hold_cost(
        horizon=horizon,
        table=table,
        start_energy_kwh=start,
        ambient_self_consumption=True,
    )

    assert with_self_use < absorb_only, (with_self_use, absorb_only)

    # **And the floor clamp binds, which is the half a "cheaper is better" assertion
    # cannot see.** A pack at the floor cannot self-consume, so the two baselines must
    # agree there. Without the clamp the baseline would go on being credited with
    # ambient service straight through the floor -- cheaper, and physically false.
    #
    # Measured on a horizon with **no production**, so the walk can never rise above
    # the floor by absorbing surplus. With production present the pack does rise, the
    # clamp legitimately stops binding after the first interval, and the two baselines
    # differ for a reason that has nothing to do with the clamp.
    dark = solve_at(head=HEAD, end=END, stored=STORED, pv_fn=lambda index: 0.0)
    floor = dark.outcome.desired.terminal_floor_kwh
    at_floor_absorb = hold_cost(
        horizon=dark.outcome.horizon, table=dark.table, start_energy_kwh=floor
    )
    at_floor_self_use = hold_cost(
        horizon=dark.outcome.horizon,
        table=dark.table,
        start_energy_kwh=floor,
        ambient_self_consumption=True,
        hard_floor_kwh=floor,
    )
    assert at_floor_self_use == pytest.approx(at_floor_absorb, abs=1e-9), (
        at_floor_self_use,
        at_floor_absorb,
    )


# ===========================================================================
# 4. no additional solve
# ===========================================================================


def test_beta_thirty_seven_adds_no_dynamic_programming_solve() -> None:
    """**Counted, not argued.** ``solve_count`` is published for this reason.

    Every figure the sensor publishes is read from the outcome the refresh already
    produced: the advantage from two fields that were already computed, the marginal
    value from the head layer beta.35 already retained, the day split from the
    intervals already in memory.

    *Mutation: solve a second horizon to build the counterfactual and this fails.*
    """
    outcome = solve_at(head=HEAD, end=END, stored=STORED).outcome
    before = outcome.solve_count

    payload = economic_value_summary(
        outcome, today_interval_count=96, import_price_eur_kwh=0.32
    )

    assert payload["available"] is True
    assert outcome.solve_count == before
    # And the figure a reader would check against the harness's own baseline.
    assert before == 4, before


def test_the_summary_needs_no_table_and_no_price_series() -> None:
    """It is a projection of one object, which is why it costs nothing.

    Given only the outcome it produces the whole payload; the prices and the day
    length are context a caller supplies for display, and their absence removes
    fields rather than the state.
    """
    outcome = solve_at(head=HEAD, end=END, stored=STORED).outcome
    bare = economic_value_summary(outcome)

    assert bare["available"] is True
    assert isinstance(bare["state"], float)
    assert bare["current_import_price_eur_kwh"] is None
    assert bare["today"]["intervals"] == 0


# ===========================================================================
# 3. the execution targets, through the real coordinator
# ===========================================================================


async def test_the_execution_targets_are_unchanged_by_the_instrumentation(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
) -> None:
    """**The level Stage B consumes, byte for byte.**

    A plan-level equality is necessary and not sufficient: what the physical
    controller acts on is the published target and its ``quarter_schedule``. This
    drives a real refresh, snapshots the targets, then reads the Economic Value
    payload -- which is what the entity and the diagnostics download do -- and asserts
    the targets are the same object graph afterwards.

    Reading a sensor must not be able to move a setpoint. That sounds impossible; it
    is exactly the kind of thing a lazily-computed property can do, and this is
    cheaper than arguing about it.
    """
    from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
    from .frank_capture import synthetic_day
    from .test_beta24_live_charge import charge_now_price, live_coordinator
    from .test_economic_published import allow_trading

    coordinator = await live_coordinator(hass, config_data)
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await refresh_at(coordinator, local(NORMAL, 10, 45))

    import copy

    before = copy.deepcopy(list(coordinator.execution_targets))
    assert before, "the witness: there is something to be unchanged"

    payload = coordinator.economic_value()
    for _ in range(3):
        coordinator.economic_value()

    after = list(coordinator.execution_targets)
    assert after == before
    # And every quarter row of every target, named so a shallow compare cannot pass.
    for target_before, target_after in zip(before, after, strict=True):
        assert target_before.get("quarter_schedule") == target_after.get(
            "quarter_schedule"
        )
    assert isinstance(payload, dict)


async def test_reading_the_sensor_moves_no_plan(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
) -> None:
    """The same argument one layer up: the plan and the outcome are untouched."""
    from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
    from .frank_capture import synthetic_day
    from .test_beta24_live_charge import charge_now_price, live_coordinator
    from .test_economic_published import allow_trading

    coordinator = await live_coordinator(hass, config_data)
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await refresh_at(coordinator, local(NORMAL, 10, 45))

    outcome = (coordinator.data or {}).get("economic")
    assert outcome is not None
    before = (
        outcome.desired.cost_eur,
        outcome.desired.objective_eur,
        outcome.solve_count,
        tuple(entry.action for entry in outcome.desired.intervals),
    )

    coordinator.economic_value()

    assert (
        outcome.desired.cost_eur,
        outcome.desired.objective_eur,
        outcome.solve_count,
        tuple(entry.action for entry in outcome.desired.intervals),
    ) == before


def test_build_outcome_does_not_accept_an_economic_value_argument() -> None:
    """The solver's signature is the last line of defence, and it is checked.

    If a later release wired the instrumentation *into* the solve, the argument would
    have to appear here first. Asserted on the signature so the failure is at the
    boundary rather than three layers downstream.
    """
    import inspect

    names = set(inspect.signature(build_outcome).parameters)

    assert "economic_value" not in names
    assert "reason_code" not in names
    assert "stored_energy_marginal_value_eur_kwh" not in names
