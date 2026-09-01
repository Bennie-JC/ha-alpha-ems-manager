"""beta.38: a lifecycle release, proven to have moved no decision.

**The load-bearing claim of the release.** beta.38 changes when a run *stops*. If it
also changed what Stage A *chooses*, the fix would be indistinguishable from a tuning
change and the live evidence that motivated it would no longer interpret.

Four layers, each catching what the others cannot:

1. **The change surface.** The carry state machine reads no price and imports no
   optimiser. Asserted by AST rather than by reading, so a future import is a failure
   instead of a diff nobody looked at.
2. **The decision surface, frozen.** Four horizon shapes -- a high-SoC evening where
   export dominates, a low-SoC night where purchase does, a mixed horizon with
   production alongside an authorised charge, and a zero-production horizon where
   nothing is free -- reduced to a canonical per-interval projection and hashed. The
   figures below were taken **against the beta.37 release commit and reproduced
   unchanged here**, which is the part that makes them a proof rather than a snapshot
   of the present.
3. **Solve count.** Published for exactly this reason, and asserted per shape.
4. **Safety Buy.** Unchanged and still price-blind, asserted on a horizon that
   actually issues one -- an invariance test on a quantity that is always zero proves
   nothing.

The canonical projection deliberately avoids ``repr``: an ``EconomicPlan`` carries a
``frozenset`` of permitted actions whose text order follows ``PYTHONHASHSEED``, so a
repr hash differs between two runs of *identical* code. That false positive was
observed while building this file, and the projection names its fields instead.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib

import pytest

from .beta34_shape import solve_at

PACKAGE = pathlib.Path("custom_components/alpha_ems_manager")

# ===========================================================================
# 1. the change surface
# ===========================================================================


def test_the_carry_state_machine_still_reads_no_price() -> None:
    """``execution.py`` decides lifecycle, and lifecycle is not an economic question.

    beta.38's first fix lives here, and the temptation it had to avoid was making the
    keep-or-withdraw decision partly economic -- "keep it if it is still worth doing".
    An opened row is kept because it has begun, full stop; whether it is still the
    best available trade is a question the row is past.
    """
    tree = ast.parse((PACKAGE / "execution.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    forbidden = {"economic", "policy", "reserve", "forecast", "frank"}
    assert not (imported & forbidden), sorted(imported & forbidden)


def test_the_new_predicates_name_no_price() -> None:
    """The three functions beta.38 added, read for what they reference.

    An import test alone would miss a predicate reaching a price through ``self``.
    These are small enough to check by name, and small enough that checking by name
    means something.
    """
    tree = ast.parse((PACKAGE / "coordinator.py").read_text(encoding="utf-8"))
    wanted = {"_opened_row_owns", "_lifecycle_state_from", "_note_campaign_started"}
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            found[node.name] = {
                child.attr if isinstance(child, ast.Attribute) else child.id
                for child in ast.walk(node)
                if isinstance(child, (ast.Attribute, ast.Name))
            }

    assert set(found) == wanted, sorted(set(wanted) - set(found))
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
# 2. the decision surface, frozen against the beta.37 release
# ===========================================================================

#: ``(kwargs, canonical plan digest, solve count, interval count)``.
#:
#: Recorded by solving each shape twice -- once at ``v1.0.0-beta.37`` (commit
#: ``75eb291``) in a detached worktree, once in the beta.38 tree -- and asserting the
#: two agreed before they were written down.
SHAPES: dict[str, tuple[dict, str, int, int]] = {
    "sell": (
        {"head": 28, "end": 96, "stored": 8.294},
        "2fbaf998b184e6085262e7c2f3e95c310dbf93f9201dd345205d070955c69594",
        4,
        68,
    ),
    "buy": (
        {"head": 8, "end": 96, "stored": 1.2, "allow_export": False},
        "9a4f7d24658d7803b71da6554d7ae7a44b0bd4002cb3fb6f0da0a72d7a2fbfbf",
        5,
        88,
    ),
    "mixed": (
        {"head": 36, "end": 96, "stored": 4.0},
        "09b2e9bd13766f40678ee0da046479edc014e2190a98b5f6a7e2252568cfda39",
        5,
        60,
    ),
    "zero_pv": (
        {"head": 20, "end": 96, "stored": 6.0, "pv_fn": lambda index: 0.0},
        "7161c616461caa1f4727523c97d7a13d63477cf6baa7d1d6cc048cbd6a9987cf",
        4,
        76,
    ),
}


def canonical_plan(outcome) -> str:
    """Return a digest of everything the plan decides, and nothing about set order."""
    rows = [
        (
            interval.index,
            interval.action,
            round(interval.battery_delta_dc_kwh, 9),
            round(interval.grid_import_kwh, 9),
            round(interval.grid_export_kwh, 9),
            round(interval.cost_eur, 9),
            bool(interval.run_start),
            interval.run_state,
        )
        for interval in outcome.desired.intervals
    ]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_selected_plan_is_the_plan_beta_thirty_seven_selected(shape: str) -> None:
    """Every interval's action, both energies, its cost and its run state.

    *Mutation: any change to the DP objective, the terminal value, the export gate or
    the reserve policy fails at least one of these four.*
    """
    kwargs, digest, _solves, intervals = SHAPES[shape]
    outcome = solve_at(**kwargs).outcome

    assert len(outcome.desired.intervals) == intervals
    assert canonical_plan(outcome) == digest


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_beta_thirty_eight_adds_no_dynamic_programming_solve(shape: str) -> None:
    """Counted per shape, not argued once.

    beta.38 adds one predicate on the execution path, a projection of booleans onto a
    diagnostics string, and one accounting term read off the value curve the refresh
    already holds. None of them solves anything.
    """
    kwargs, _digest, solves, _intervals = SHAPES[shape]

    assert solve_at(**kwargs).outcome.solve_count == solves


# ===========================================================================
# 3. Safety Buy, on a horizon that actually issues one
# ===========================================================================

#: Low stored energy late in the day, where survival -- not economics -- decides.
SURVIVAL = {"head": 68, "end": 96, "stored": 0.3}


def test_the_safety_buy_is_the_one_beta_thirty_seven_would_have_made() -> None:
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

    The same physics at 2 c/kWh and at 90 c/kWh. A Safety Buy that shrank when power
    got expensive would be an economic decision wearing a safety name, and the whole
    point of the separation is that the battery reaches tomorrow either way.
    """
    cheap = solve_at(**SURVIVAL, price_fn=lambda index: 0.02).outcome
    dear = solve_at(**SURVIVAL, price_fn=lambda index: 0.90).outcome

    assert cheap.safety_buy_ac_kwh == pytest.approx(dear.safety_buy_ac_kwh)
    assert cheap.bridge_kwh_now == pytest.approx(dear.bridge_kwh_now)
    assert cheap.safety_buy_attribution == dear.safety_buy_attribution
    assert repr(cheap.safety_buy_runs) == repr(dear.safety_buy_runs)
    # The witness: there is a Safety Buy to be invariant about.
    assert cheap.safety_buy_ac_kwh > 0.0
