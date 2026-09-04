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
#: **beta.41 moves all seven, and the cause is a state model rather than a
#: tuning change.** Until beta.41 the household service the inverter takes from
#: the pack was priced into the cost of holding but moved no solver state, so the
#: recursion believed it still held energy the house had already consumed -- 9.75
#: kWh against 4.32 on the live 2026-09-03 horizon. Every shape with ambient
#: service reachable in it therefore re-plans, and that is all of them.
#:
#: What did **not** move is the tighter half of the proof, and each is asserted
#: separately below:
#:
#:   * all seven **solve counts** -- no dynamic programme was added
#:   * all seven **interval counts** -- a function of prices and demands only
#:   * all seven digests with ambient service **switched off**, byte-identical to
#:     beta.40, which confines the change to the ambient model rather than
#:     asserting that it is confined
#:   * ``edge_energy_kwh == end_energy_dc_kwh`` on every shape -- one endpoint,
#:     where beta.40 had to publish two with a basis apiece
#:
#: The effect is not uniform and is not netted off here. ``sell`` and ``mixed``
#: cost slightly more metered cash and end holding more; ``zero_pv`` costs 0.62
#: EUR more and ends 3.46 kWh higher; the three ``survival`` shapes buy 6.39 kWh
#: where they bought 5.00, because the reserve genuinely binds once the pack is
#: modelled as depleting. Safety Buy appears on ``buy`` and ``zero_pv`` where it
#: was zero, for the same reason -- see the Safety Buy section.
#:
#: **beta.41 Phase 2 adds one solve and moves one of these.** The coverage
#: counterfactual runs on every refresh, so every solve count below is one higher.
#: Only ``survival`` re-plans: there the pack is empty, the household will import
#: 15.3 kWh whatever happens, and buying part of it at the horizon's cheapest
#: quarters -- 0.262 against a range topping 0.386 -- saves 0.711 EUR that the
#: user's own 0.20 EUR and 0.05 EUR/kWh gates would have refused. That is the band
#: coverage exists for.
#:
#: The other six are **unchanged**, which is the more important half: where
#: ordinary economics already buys the useful energy, coverage contributes nothing
#: and cannot.
SHAPES: dict[str, tuple[dict, str, int, int]] = {
    "sell": (
        {"head": 28, "end": 96, "stored": 8.294},
        "799cf8ba159e7ef09e639a0c1eb40463fe5adae2f7c25b6e447c11f2b5b26f5e",
        5,
        68,
    ),
    "buy": (
        {"head": 8, "end": 96, "stored": 1.2, "allow_export": False},
        "0dd47d62d19ef6e5117dbee76ef869434921ba6b4c7cbcfe8be46d6e2b07f628",
        6,
        88,
    ),
    "mixed": (
        {"head": 36, "end": 96, "stored": 4.0},
        "861b0c4bad4f6c22ada8f1fa9c9a2703bdd22b233fec15e1900270e5db64d565",
        6,
        60,
    ),
    "zero_pv": (
        {"head": 20, "end": 96, "stored": 6.0, "pv_fn": lambda index: 0.0},
        "4b0dae1524240ab49e09b6066c3a038fb1c62bf0a43bb7360ed4979153ea437f",
        5,
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

    # **beta.41: the run grew, the compelled part did not.** ``bridge_kwh_now``
    # is unchanged at 4.5458 and the safety component is unchanged at 4.7917 --
    # identical at 2 c/kWh and at 90 c/kWh, which the test below proves. What grew
    # is the *economic* share, from 0.208 to 1.597, because once the pack is
    # modelled as depleting the optimiser buys more on this horizon than it used
    # to. The quantity that is compulsory is the invariant here; the size of the
    # run that contains it is not.
    # **Phase 2 grew the run and left the compelled part untouched.** The
    # compelled quantity is still 4.7917 and still identical at 2 c/kWh and at
    # 90 c/kWh -- coverage cannot make a purchase compulsory, and does not try to.
    # What grew is the run around it: on this horizon the pack is empty, the
    # household will import 15.3 kWh regardless, and coverage buys part of it at
    # the cheapest quarters available. Three further single-interval runs appear
    # for the same reason, carrying no compelled share at all.
    assert outcome.safety_buy_ac_kwh == pytest.approx(7.777777777777779)
    assert outcome.bridge_kwh_now == pytest.approx(4.545823529332798)
    # **beta.41 re-records the split, not the quantity.** ``safety_buy_ac_kwh``
    # and ``bridge_kwh_now`` above are unchanged, and they are the invariants: how
    # much was bought, and how much was compulsory. What moved is the *reason*
    # attached to it.
    #
    # The old split came from differencing this solve against one whose reserve is
    # relaxed to the hard floor. At 0.3 kWh the pack is below that floor, so the
    # relaxed solve is in violation too, both solves are minimising violation
    # rather than cost, and their difference reflects tie-breaks instead of
    # motives. Below the floor the compelled quantity is the bridge itself --
    # ``max(0, reachability_now - stored)``, converted to the AC boundary runs are
    # measured at -- and anything beyond it is economic even down here, because at
    # a dear price the plan legitimately holds more than the bridge so the inverter
    # can self-consume rather than import.
    #
    # That is what makes the figure price-blind again: 4.7917 at 2 c/kWh and at
    # 90 c/kWh alike, which the test below now proves on two solves that are no
    # longer identical.
    # **Three headings, and the run adds up under them.** The published pair is
    # compelled and *discretionary*, so the coverage share is subtracted out of the
    # second rather than left sitting in it: 4.7917 compelled, 1.3889 coverage and
    # 1.5972 discretionary make the run's 7.7778 exactly. The three single-interval
    # runs are coverage entire, which is why their discretionary share is zero and
    # not 0.2778.
    #
    # 1.5972 is the figure beta.40 published for the discretionary half of this run
    # before any of this, and it returning unchanged is the useful part: the energy
    # Phase 2 added to the run is coverage, none of it was a trade, and none of it
    # was compulsory.
    assert outcome.safety_buy_attribution == {
        68: (pytest.approx(4.791718731292308), pytest.approx(1.5971701575965849)),
        88: (pytest.approx(0.0), pytest.approx(0.0)),
        90: (pytest.approx(0.0), pytest.approx(0.0)),
        92: (pytest.approx(0.0), pytest.approx(0.0)),
    }
    assert outcome.coverage_buy_attribution == {
        68: pytest.approx(1.3888888888888928),
        88: pytest.approx(0.2777777777777785),
        90: pytest.approx(0.2777777777777785),
        92: pytest.approx(0.2777777777777785),
    }
    for run in outcome.desired.runs:
        if run.action != "charge":
            continue
        compelled, discretionary = outcome.safety_buy_attribution[run.start_index]
        covered = outcome.coverage_buy_attribution[run.start_index]
        assert compelled + covered + discretionary == pytest.approx(
            run.battery_charge_ac_kwh, abs=1e-9
        ), run.start_index
    # And it is coverage rather than arbitrage, structurally: nothing is exported.
    assert outcome.desired.planned_grid_export_kwh == pytest.approx(0.0, abs=1e-9)
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
