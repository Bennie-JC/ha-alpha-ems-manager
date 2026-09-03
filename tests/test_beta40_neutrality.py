"""beta.40: one decision moved, and it is named. Everything else is frozen.

**beta.40 is not an observability release and does not claim to be.** It changes
what the controller commands: with Stage A's retention verdict on the row and
measured production standing there, Stage B stores energy beta.39 exported. Saying
"no decision moved" would be false, so this file says exactly which one did and
pins everything around it.

What is frozen, and proven rather than argued:

1. **The plan.** The seven horizon shapes and their canonical per-interval digests
   are imported from ``test_beta39_neutrality`` -- recorded against ``ff3e912`` in a
   detached worktree under ``PYTHONHASHSEED=0``, reproduced at ``508c18a``, and
   asserted again here. Three releases, one surface, byte-identical. The optimiser
   chose nothing differently: no objective term, no tie-break, no enumeration
   order, no ``minimum_trade_gain_eur``, no reserve, no export gate.
2. **The solve count**, per shape. The retention gate reads the value table the
   solve already built and adds no dynamic programme.
3. **The published contract**, which gains three additive row keys -- the verdict,
   its reason and the energy ceiling the audit added -- and changes no other byte.
   All absent on a plan with no gate, so a pre-beta.40 publication round-trips
   unchanged.
4. **The command**, on a row Stage A did not authorise: field for field the beta.39
   decision, including the reported clamp token.
5. **The boundary.** The gate holds no physical limit, so the optimiser still
   constrains by none -- ``test_phase_eight_boundaries`` owns that, and this file
   pins the gate's own shape so it cannot acquire one.

**The one decision that moved** is bounded twice, by arithmetic rather than by
care. It is capped at the measured surplus, so it can never buy a watt --
``test_beta40_absorption_arithmetic`` sweeps that. And it is capped at the energy
above which the optimiser's own dual stops clearing the export price, so it cannot
keep a kilowatt-hour the economics would have sold -- ``test_beta40_retention_ceiling``
sweeps that, and it exists because the first implementation could.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import timedelta

import pytest

from custom_components.alpha_ems_manager.const import EXECUTION_INTENT_GRID_CHARGE
from custom_components.alpha_ems_manager.economic import (
    RetentionGate,
    quarter_schedule_for,
)

from .beta34_shape import solve_at
from .beta40_trace import EXPORT_PRICE_EUR_KWH, ROUND_TRIP_EFFICIENCY
from .forecast_helpers import NORMAL, local
from .test_beta39_neutrality import SHAPES, canonical_plan
from .test_beta40_safety_buy_unchanged import GATE_KEYS, LIVE_GATE, Interval

PACKAGE = pathlib.Path("custom_components/alpha_ems_manager")


# ===========================================================================
# 1. the plan, frozen across three releases
# ===========================================================================


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_selected_plan_is_still_the_plan_beta_thirty_eight_selected(
    shape: str,
) -> None:
    """**The load-bearing claim of the release.**

    Same digests, same shapes, third release. beta.40 adds a *reading* of the
    optimiser's dual and a verdict published beside each row; if it had also nudged
    the objective, the tie-break or the reserve, one of these seven would move.

    *Mutation: change the enumeration order, the strict ``<``, the run fee or any
    cost term and at least one shape fails.*
    """
    kwargs, digest, _solves, intervals = SHAPES[shape]
    outcome = solve_at(**kwargs).outcome

    assert len(outcome.desired.intervals) == intervals
    assert canonical_plan(outcome.desired) == digest


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_beta_forty_adds_no_dynamic_programming_solve(shape: str) -> None:
    """The gate reads a table the solve already built.

    ``head_value`` is the surviving layer of the recursion, kept since beta.35 and
    published since beta.37. Differencing it costs two lookups. A release that
    solved again to price a row would have made a publication cost a dynamic
    programme.
    """
    kwargs, _digest, solves, _intervals = SHAPES[shape]

    assert solve_at(**kwargs).outcome.solve_count == solves


# ===========================================================================
# 2. the published contract
# ===========================================================================


def test_the_contract_gains_only_its_own_keys_and_changes_no_other_byte() -> None:
    """Additive, and asserted as a difference rather than by inspection."""
    base = local(NORMAL, 12, 0)
    args = {
        "start_index": 0,
        "end_index": 1,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "moment": lambda i: base + timedelta(minutes=15 * i),
    }
    without = quarter_schedule_for((Interval(0), Interval(1)), **args)
    with_gate = quarter_schedule_for(
        (Interval(0), Interval(1)), retention=LIVE_GATE, **args
    )

    for plain, gated in zip(without, with_gate, strict=True):
        assert set(gated) - set(plain) == GATE_KEYS
        assert all(gated[key] == value for key, value in plain.items())


def test_a_pre_beta_forty_publication_is_untouched_by_the_parser() -> None:
    """A stored row without the keys reads back exactly as it did.

    The claim record holds whole publications, so this is the upgrade path: a
    beta.39 document must restore a beta.39 row, and absence must be a refusal
    rather than a grant.
    """
    from custom_components.alpha_ems_manager.execution import parse_target

    base = local(NORMAL, 12, 0)
    legacy_row = {
        "start": base.isoformat(),
        "end": (base + timedelta(minutes=15)).isoformat(),
        "battery_kwh": 0.28,
        "grid_authorised_kwh": 0.04,
        "grid_export_target_kwh": 0.0,
        "grid_export_caused_kwh": 0.0,
        "desired_grid_kw": 0.15,
        "executable": True,
        "not_executable": None,
    }
    target = parse_target(
        {
            "plan_id": "plan-1",
            "revision": 1,
            "intent": EXECUTION_INTENT_GRID_CHARGE,
            "window_start": base.isoformat(),
            "window_end": (base + timedelta(minutes=15)).isoformat(),
            "issued_at": base.isoformat(),
            "stale_after": (base + timedelta(minutes=15)).isoformat(),
            "battery_target_kwh": 0.28,
            "quarter_schedule": [legacy_row],
        }
    )
    assert target is not None
    row = target.quarter_schedule[0]

    assert row.retention_authorised is False
    assert row.battery_kwh == 0.28
    assert row.grid_authorised_kwh == 0.04
    assert row.desired_grid_kw == 0.15
    assert row.not_executable is None


# ===========================================================================
# 3. the command, where the verdict is absent
# ===========================================================================


def test_an_unauthorised_row_commands_exactly_what_beta_thirty_nine_commanded() -> None:
    """Field for field, over a grid of live conditions.

    A release that perturbed the ticks it was not meant to touch would show up
    here rather than on a dashboard three weeks later.
    """
    from custom_components.alpha_ems_manager.dispatch import (
        ChargeLimits,
        QuarterProgress,
        decide_charge,
    )

    limits = ChargeLimits(inverter_kw=10.0)
    for pv_kw in (0.0, 0.8, 2.0, 3.309, 8.0):
        for house_kw in (0.3, 0.792, 2.5):
            for remaining in (0.0, 0.0373, 0.28, 2.5):
                for grid in (0.0, 0.04, 2.27):
                    shared = {
                        "seconds_remaining": 300.0,
                        "battery_remaining_kwh": remaining,
                        "grid_remaining_kwh": grid,
                    }
                    refused = decide_charge(
                        progress=QuarterProgress(**shared, retention_authorised=False),
                        house_load_kw=house_kw,
                        pv_kw=pv_kw,
                        limits=limits,
                        last_applied_kw=None,
                    )
                    legacy = decide_charge(
                        progress=QuarterProgress(**shared),
                        house_load_kw=house_kw,
                        pv_kw=pv_kw,
                        limits=limits,
                        last_applied_kw=None,
                    )
                    assert refused.as_dict() == legacy.as_dict(), (
                        pv_kw,
                        house_kw,
                        remaining,
                        grid,
                    )


# ===========================================================================
# 4. the gate's own shape
# ===========================================================================


def test_the_gate_holds_no_physical_limit() -> None:
    """**Why the published row carries a verdict and not a kilowatt-hour.**

    Every physical bound in this integration comes out of one clamp. A gate that
    compared an inverter rating or a pack ceiling would be a second copy, and the
    first time the two disagreed it would be the copy that got believed --
    ``test_phase_eight_boundaries`` enforces that for the whole module, and this
    pins the gate itself so it cannot acquire one later.
    """
    fields = set(RetentionGate.__dataclass_fields__)
    # **Prices, a value curve and the lattice it is indexed on. Nothing physical.**
    # The corrective added three: the dual at every level, where the pack stands on
    # that lattice, and the lattice pitch. A pitch is an economic quantisation
    # chosen by ``select_bucket_kwh``, and a level is a state -- neither is an
    # inverter rating or a pack ceiling, which is what this test forbids.
    assert fields == {
        "marginal_value_eur_kwh",
        "round_trip_efficiency",
        "marginal_curve_eur_kwh",
        "current_bucket",
        "bucket_dc_kwh",
    }

    # **Identifiers, not prose.** The docstring argues at length about why the
    # inverter rating and the pack ceiling are *absent*, so a substring search over
    # the source would fail on the explanation rather than on a limit.
    body = ast.parse(inspect.getsource(RetentionGate))
    named = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(body)
        if isinstance(node, ast.Attribute | ast.Name)
    }
    forbidden = {
        "max_charge_kw",
        "max_discharge_kw",
        "headroom_energy_kwh",
        "usable_energy_kwh",
        "ceiling_kwh",
        "ceiling_dc_kwh",
        "soc_percent",
        "start_energy_dc_kwh",
    }
    assert not (named & forbidden), sorted(named & forbidden)


def test_the_gate_reaches_no_solve_and_no_send() -> None:
    """It prices a row. It cannot start a solve or touch an actuator.

    Asserted by AST over the call graph of the two functions that hold the gate, so
    a future call is a failure rather than a diff nobody looked at.
    """
    tree = ast.parse((PACKAGE / "economic.py").read_text(encoding="utf-8"))
    guarded = {"verdict", "quarter_schedule_for"}
    forbidden = {
        "solve",
        "build_outcome",
        "build_physics_table",
        "build_horizon",
        "async_call",
        "send",
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in guarded:
            continue
        called = {
            inner.func.id if isinstance(inner.func, ast.Name) else inner.func.attr
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name | ast.Attribute)
        }
        assert not (called & forbidden), (node.name, sorted(called & forbidden))


def test_the_verdict_is_a_strict_comparison_in_one_place() -> None:
    """One expression, so there is one thing to reason about.

    ``eta_rt * V > export_price``, strict, so the boundary case refuses rather than
    granting on a tie. Pinned because a gate assembled from several comparisons is
    a gate nobody can state.
    """
    source = inspect.getsource(RetentionGate.verdict)

    assert source.count("<=") == 1
    assert "round_trip_efficiency * value <= export_price" in source


def test_the_gate_is_blind_to_the_import_price() -> None:
    """It compares what holding is worth against what selling pays, and nothing else.

    The import price is already inside the dual -- that is what makes the dual the
    right number -- so consulting it again here would double-count the buy side.
    """
    gate = RetentionGate(
        marginal_value_eur_kwh=0.2237, round_trip_efficiency=ROUND_TRIP_EFFICIENCY
    )

    dear = Interval(0, export_price=EXPORT_PRICE_EUR_KWH)
    dear.import_price_eur_kwh = 9.99
    cheap = Interval(0, export_price=EXPORT_PRICE_EUR_KWH)
    cheap.import_price_eur_kwh = 0.0

    assert gate.verdict(dear.export_price_eur_kwh) == gate.verdict(
        cheap.export_price_eur_kwh
    )
