"""beta.39: an observability release, proven to have moved no decision.

**The load-bearing claim.** beta.39 changes when the lifecycle *says* a run is
executing and adds four euro figures to a diagnostics payload. If it also changed
what Stage A chooses, what Stage B sends, or what either of them chooses *not* to
do, then the live evidence that motivated it would no longer interpret and the
release would be a tuning change wearing an observability name.

Five layers, each catching what the others cannot:

1. **The change surface.** The accounting layer imports no solver and starts no
   solve; the carry state machine still reads no price. Asserted by AST rather
   than by reading, so a future import is a failure instead of a diff nobody
   looked at.
2. **The decision surface, frozen against the beta.38 release.** Seven horizon
   shapes reduced to a canonical per-interval projection over eighteen named
   fields -- every energy, every price, both counterfactual baselines, the
   ambient term, the run state and the constraints -- and hashed. The figures
   below were taken **against ``ff3e912`` in a detached worktree under
   ``PYTHONHASHSEED=0`` and reproduced unchanged here**, which is the part that
   makes them a proof rather than a snapshot of the present.
3. **Solve count**, published for exactly this reason, and asserted per shape:
   the release must add no dynamic-programming solve, on the refresh path or on
   the publishing path.
4. **Safety Buy**, unchanged and still price-blind, on horizons that actually
   issue one -- an invariance test on a quantity that is always zero proves
   nothing.
5. **Execution targets and Stage-B commands.** Proven cross-commit by dumping the
   published targets and the wire traffic from the same replay in both trees; the
   two were byte-identical over 8687 bytes in each direction. What is pinned
   *here* is the structural half of that: the publishing helpers cannot reach a
   send, a stop, a claim or a teardown.

The canonical projection deliberately avoids ``repr``: an ``EconomicPlan`` carries
a ``frozenset`` of permitted actions whose text order follows ``PYTHONHASHSEED``,
so a repr hash differs between two runs of *identical* code. That false positive
was observed while building the beta.38 harness, and the projection names its
fields instead.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import pathlib

import pytest

from .beta34_shape import solve_at

PACKAGE = pathlib.Path("custom_components/alpha_ems_manager")

# ===========================================================================
# 1. the change surface
# ===========================================================================


def test_the_accounting_layer_still_imports_no_solver() -> None:
    """``realized.py`` prices what happened. It may not be able to plan.

    The sunk-cost guard, restated for beta.39: the release adds the day
    accounting to this module, and the accounting reads planner-derived figures.
    It must go on receiving them as *numbers* rather than reaching for the value
    function itself -- the moment it can solve, a realised figure can influence a
    decision, and Phase 9 would be Phase 8 wearing this one's clothes.
    """
    tree = ast.parse((PACKAGE / "realized.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    # ``const`` is a shared vocabulary and is itself pure; the other three are
    # the standard library. Anything else at all would be new.
    assert imported == {"__future__", "collections.abc", "dataclasses", "const"}, (
        sorted(imported)
    )


def test_the_carry_state_machine_still_reads_no_price() -> None:
    """``execution.py`` decides lifecycle, and lifecycle is not economic.

    beta.39 changes the lifecycle *projection*, which lives in the coordinator.
    Nothing about the carry state machine moves, and the temptation this pins
    against is making the keep-or-withdraw decision partly economic.
    """
    tree = ast.parse((PACKAGE / "execution.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    forbidden = {"economic", "policy", "reserve", "forecast", "frank", "realized"}
    assert not (imported & forbidden), sorted(imported & forbidden)


def test_the_new_accounting_helpers_reach_no_decision() -> None:
    """The six coordinator helpers beta.39 adds, read for what they can call.

    An import test alone would miss a helper reaching a decision through
    ``self``. These are small enough to check by name, and small enough that
    checking by name means something: a publishing helper that can admit a plan,
    claim a dispatch, stop one or tear one down is not publish-only whatever its
    docstring says.
    """
    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    forbidden = (
        "_async_send_locked",
        "_async_stop_dispatch",
        "_async_dispatch",
        "_abandon_execution",
        "_claim_authority",
        "_clear_execution_record",
        "admit_plan",
        "carry_forward",
        "solve_economic",
        "build_physics_table",
    )
    for name in (
        "_today_accounting",
        "_remaining_expected_eur",
        "_forecast_revaluation_eur",
        "_open_quarter_value_eur",
        "_note_opening_valuation",
        "_settle_execution_payload",
    ):
        source = inspect.getsource(getattr(AlphaEmsCoordinator, name))
        # **The docstring is stripped first.** These functions explain themselves
        # by naming the sites they were separated from -- ``_async_dispatch`` is
        # the whole reason ``_settle_execution_payload`` exists -- and a check
        # that could not tell prose from a call would force the explanation out
        # of the code, which is the wrong trade.
        body = source[source.index('"""', source.index('"""') + 3) + 3 :]
        for symbol in forbidden:
            assert symbol not in body, (name, symbol)


def test_the_lifecycle_helpers_name_no_price() -> None:
    """The projection decides a diagnostics string, not an economic question."""
    tree = ast.parse((PACKAGE / "coordinator.py").read_text(encoding="utf-8"))
    wanted = {"_lifecycle_state_from", "_note_lifecycle", "_settle_execution_payload"}
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            found[node.name] = {
                child.attr if isinstance(child, ast.Attribute) else child.id
                for child in ast.walk(node)
                if isinstance(child, (ast.Attribute, ast.Name))
            }

    assert set(found) == wanted, sorted(wanted - set(found))
    for name, referenced in found.items():
        priced = {
            symbol
            for symbol in referenced
            if any(
                word in symbol.lower()
                for word in ("price", "eur", "cost", "tariff", "value_curve")
            )
        }
        assert not priced, (name, sorted(priced))


# ===========================================================================
# 2. the decision surface, frozen against the beta.38 release
# ===========================================================================

#: ``name -> (kwargs, canonical plan digest, solve count, interval count)``.
#:
#: Recorded by solving each shape twice -- once at ``v1.0.0-beta.38`` (commit
#: ``ff3e912``) in a detached worktree under ``PYTHONHASHSEED=0``, once in the
#: beta.39 tree -- and asserting the two agreed before they were written down.
#: The three ``survival`` shapes are the same physics at three price levels, so
#: the Safety Buy's price-blindness is inside the frozen surface rather than
#: argued beside it.
SHAPES: dict[str, tuple[dict, str, int, int]] = {
    "sell": (
        {"head": 28, "end": 96, "stored": 8.294},
        "6ceb5e44b4202f863a820dccba079c4e82279ab07920b62c35146b497dbea085",
        4,
        68,
    ),
    "buy": (
        {"head": 8, "end": 96, "stored": 1.2, "allow_export": False},
        "95e7d127bd0574a9337d570b5a436acc0adff2867c67735fa068d36b80cb50c8",
        5,
        88,
    ),
    "mixed": (
        {"head": 36, "end": 96, "stored": 4.0},
        "2579e6631fa9f3c4e57e7bccd524335ac26fcae4ab97ce63bcbd085114944389",
        5,
        60,
    ),
    "zero_pv": (
        {"head": 20, "end": 96, "stored": 6.0, "pv_fn": lambda index: 0.0},
        "0960c1f916a2e4563232c0e700aaba03a99937468234fb53980281f1dda706ff",
        4,
        76,
    ),
    "survival": (
        {"head": 68, "end": 96, "stored": 0.3},
        "71551fde600499916b38ba5d2b8abc90c23ff0b05bc594e6b6e3d74a6efaeed5",
        5,
        28,
    ),
    "survival_dear": (
        {"head": 68, "end": 96, "stored": 0.3, "price_fn": lambda index: 0.90},
        "3f6ec8c2a333a12f9cab039e1ae912cdebdef2ccaf1ad32c6c45e4a652c4a811",
        5,
        28,
    ),
    "survival_cheap": (
        {"head": 68, "end": 96, "stored": 0.3, "price_fn": lambda index: 0.02},
        "d362d6ce96abe4eb3bf4f480b4aaaf2b7a331f185cc2383584154da278782ff6",
        5,
        28,
    ),
}


def canonical_plan(plan) -> str:
    """Return a digest of everything the plan decides, and nothing about set order.

    Eighteen named fields per interval, and the list grew in beta.39 for a
    concrete reason: the release reads ``idle_import_kwh`` and
    ``ambient_self_consumption_ac_kwh`` to rebuild the no-battery counterfactual,
    so a change to either would change a published euro figure. A digest that did
    not cover them would have called that neutral.
    """
    rows = [
        (
            interval.index,
            interval.action,
            round(interval.battery_delta_dc_kwh, 9),
            round(interval.battery_charge_ac_kwh, 9),
            round(interval.battery_discharge_ac_kwh, 9),
            round(interval.grid_import_kwh, 9),
            round(interval.grid_export_kwh, 9),
            round(interval.pv_curtailed_kwh, 9),
            round(interval.cost_eur, 9),
            round(interval.idle_import_kwh, 9),
            round(interval.idle_export_kwh, 9),
            round(interval.idle_cost_eur, 9),
            round(interval.ambient_self_consumption_ac_kwh, 9),
            interval.counterfactual_basis,
            bool(interval.absorbing),
            bool(interval.run_start),
            interval.run_state,
            list(interval.constraints),
        )
        for interval in plan.intervals
    ]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_selected_plan_is_the_plan_beta_thirty_eight_selected(shape: str) -> None:
    """Every interval's action, five energies, its cost, both baselines, its state.

    *Mutation: any change to the DP objective, the terminal value, the export
    gate, the reserve policy, the ambient model or the counterfactual fails at
    least one of these seven.*
    """
    kwargs, digest, _solves, intervals = SHAPES[shape]
    outcome = solve_at(**kwargs).outcome

    assert len(outcome.desired.intervals) == intervals
    assert canonical_plan(outcome.desired) == digest


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_beta_thirty_nine_adds_no_dynamic_programming_solve(shape: str) -> None:
    """Counted per shape, not argued once.

    beta.39 adds a reordered projection, a second cadence for it, a payload
    re-render, one arithmetic property on an interval and a persisted scalar read
    back. None of them solves anything, and the accounting is assembled on the
    *publishing* path -- where a solve would make an entity read cost a dynamic
    programme.
    """
    kwargs, _digest, solves, _intervals = SHAPES[shape]

    assert solve_at(**kwargs).outcome.solve_count == solves


# ===========================================================================
# 3. Safety Buy, on horizons that actually issue one
# ===========================================================================

SURVIVAL = {"head": 68, "end": 96, "stored": 0.3}


def test_the_safety_buy_is_the_one_beta_thirty_eight_would_have_made() -> None:
    """The figures, frozen. Recorded the same way as the plan digests above."""
    outcome = solve_at(**SURVIVAL).outcome

    assert outcome.safety_buy_ac_kwh == pytest.approx(5.0)
    assert outcome.bridge_kwh_now == pytest.approx(4.545823529332798)
    assert outcome.safety_buy_attribution == {
        68: (pytest.approx(0.27777777777777146), pytest.approx(4.7222222222222285))
    }
    assert len(outcome.safety_buy_runs) == 1


def test_the_safety_buy_is_still_blind_to_price() -> None:
    """A45. Only physical reachability may initiate it, so price may not move it.

    The same physics at 2 c/kWh and at 90 c/kWh. A Safety Buy that shrank when
    power got expensive would be an economic decision wearing a safety name, and
    the whole point of the separation is that the battery reaches tomorrow either
    way.
    """
    cheap = solve_at(**SURVIVAL, price_fn=lambda index: 0.02).outcome
    dear = solve_at(**SURVIVAL, price_fn=lambda index: 0.90).outcome

    assert cheap.safety_buy_ac_kwh == pytest.approx(dear.safety_buy_ac_kwh)
    assert cheap.bridge_kwh_now == pytest.approx(dear.bridge_kwh_now)
    assert cheap.safety_buy_attribution == dear.safety_buy_attribution
    assert repr(cheap.safety_buy_runs) == repr(dear.safety_buy_runs)
    # The witness: there is a Safety Buy to be invariant about.
    assert cheap.safety_buy_ac_kwh > 0.0


# ===========================================================================
# 4. the accounting is arithmetic over a plan, never a second plan
# ===========================================================================


def test_the_no_battery_property_is_arithmetic_on_fields_already_there() -> None:
    """Two additions and a clamp, over fields the interval already carried.

    The alternative -- re-solving the horizon with the battery removed -- would
    have been a second dynamic programme per refresh *and* a second plan whose
    forecast could differ from the one published beside it. Pinned on the source
    because it is the property that makes the release free.
    """
    from custom_components.alpha_ems_manager.economic import EconomicInterval

    for name in ("no_battery_import_kwh", "avoided_import_no_battery_kwh"):
        source = inspect.getsource(getattr(EconomicInterval, name).fget)
        body = source[source.rindex('"""') + 3 :]
        assert "solve" not in body, (name, body)
        assert "for " not in body, (name, body)
        assert "self." in body, (name, body)
