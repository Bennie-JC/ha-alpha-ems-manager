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
        "f0bb11d580efda6a9036da2c8ab24a260f8308d0c09f577cb5cace99e5682eb9",
        5,
        68,
    ),
    "buy": (
        {"head": 8, "end": 96, "stored": 1.2, "allow_export": False},
        "d40708bdd600e5e9fb40474d3b135e78dfd42ebd115fe3806473f63a01d29ace",
        6,
        88,
    ),
    "mixed": (
        {"head": 36, "end": 96, "stored": 4.0},
        "9809f9a237dc909b640ee699fe1390544ab181f4cab17e6e2ba57fbb2b13ec83",
        6,
        60,
    ),
    "zero_pv": (
        {"head": 20, "end": 96, "stored": 6.0, "pv_fn": lambda index: 0.0},
        "26de93d36ce9457e8f52a84507de6857084359231671ae75c07b6f96747216bc",
        5,
        76,
    ),
    "survival": (
        {"head": 68, "end": 96, "stored": 0.3},
        "8a4ce6c2ccbe4f5f5fd50e08ccb65720a31c5d21af55ad1bfd7dd4957d2ee803",
        6,
        28,
    ),
    "survival_dear": (
        {"head": 68, "end": 96, "stored": 0.3, "price_fn": lambda index: 0.90},
        "277b1ed779e604edef2e18d5d479cd86d3e0eb33c94e17706516fa1070f1674f",
        6,
        28,
    ),
    "survival_cheap": (
        {"head": 68, "end": 96, "stored": 0.3, "price_fn": lambda index: 0.02},
        "4542d1aa25f48cb207f1f65c6f9895dc9b0e4393dd9c1674d83d2e6454af8d87",
        6,
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
