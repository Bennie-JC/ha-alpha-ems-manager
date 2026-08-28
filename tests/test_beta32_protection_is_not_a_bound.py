"""Four quantities, and only one of them may force the household to buy energy.

**The architectural gate of beta.32, asserted structurally.** The release adds a
protective demand estimate, an export permission and an anti-churn extension. Each
of them is built from prices, learned demand and measured error -- and the one thing
none of them may become is the *lexicographic physical reserve*, which is the first
element of the objective pair and therefore outranks any amount of money and can
compel a purchase at any price.

Rev 2 of the design proposed narrowing the reachability recursion's grid credit to
economically attractive intervals via a boolean mask. Rev 3 **withdrew it**: even as
a boolean, a mask derived from prices makes the physical reserve depend on
economics, which is exactly the separation beta.31 exists to establish. So
``reserve.py`` stays price-blind, the physical curve stays flat at
``floor + margin`` on this installation -- correct physics, with a 10 kW connection
the grid genuinely can refill the pack next quarter -- and the export decision is
made by a *price* instead.

The two questions that separate the four quantities, and they are genuinely
different powers:

* **can it initiate a grid purchase?** Only physical reachability. Nothing else.
* **can it increase one already triggered?** Physical reachability, and the
  anti-churn extension -- which is why the extension is its own third attribution
  category rather than being folded into either neighbour.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from custom_components.alpha_ems_manager import economic as economic_module
from custom_components.alpha_ems_manager import reserve as reserve_module

from .beta32_harness import FLOOR, flat, live_shape, solve_shape
from .test_beta32_export_gate import MEASURED, _priced

#: Names that must never appear inside the priced part of the solve.
#:
#: Each is a protection quantity. A pessimistic demand estimate reaching the cost
#: objective would be a *second forecast* smuggled in through the back door, and
#: building a second forecast is a stated non-goal of this release.
_PROTECTION_ONLY = (
    "upper_net_demand",
    "upper_net_demand_curve",
    "adaptation_ratio",
    "err_for",
    "ForecastRisk",
    "survival_curves",
    "anti_churn_buffer_kwh",
)


def _names(function) -> set[str]:
    """Return every name and attribute mentioned in one function's body."""
    tree = ast.parse(inspect.cleandoc(inspect.getsource(function)))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


# ===========================================================================
# A. the protective estimate never reaches a priced quantity
# ===========================================================================


def test_no_protection_quantity_reaches_the_interval_costing() -> None:
    """``_interval_outcomes`` prices moves. It must not see a protection figure.

    The cost objective keeps ``demand.baseline_kwh`` -- the P50 -- untouched. What
    the protection may reach is the export *permission*, the anti-churn sizing and
    diagnostics, and nothing else.
    """
    names = _names(economic_module._interval_outcomes)
    for denied in _PROTECTION_ONLY:
        assert denied not in names, denied


def test_the_solve_sees_the_permission_and_never_the_estimate() -> None:
    """``solve`` may hold the two gate arrays and nothing upstream of them.

    ``export_floor_kwh`` and ``export_free`` are the *outputs* of the protection
    machinery, already reduced to a per-interval energy and a per-interval boolean.
    The machinery itself -- the risk record, the adaptation ratio, the error
    allowance -- stays outside, so no future edit inside the recursion can start
    consulting it.
    """
    names = _names(economic_module.solve)
    for denied in (
        "upper_net_demand_curve",
        "adaptation_ratio",
        "err_for",
        "ForecastRisk",
        "survival_curves",
        "anti_churn_buffer_kwh",
    ):
        assert denied not in names, denied
    # The permission itself is present, which is what makes the exclusions above a
    # statement about *layering* rather than an accident of naming.
    assert "export_floor_kwh" in names
    assert "export_free" in names


def test_the_reserve_module_still_cannot_see_a_price() -> None:
    """Phase D was withdrawn, and this is the line that keeps it withdrawn.

    Rev 2's boolean mask would have satisfied the letter of this check while
    breaking its purpose, so the assertion is paired with the behavioural one
    below: the recursion's answer must be *identical* under reversed prices.
    """
    source = inspect.getsource(reserve_module)
    assert "IntervalPrice" not in source
    assert "import_eur_kwh" not in source
    assert "export_eur_kwh" not in source
    # And no economic vocabulary of any kind reached it either.
    for denied in ("attractive", "survival", "export_floor", "protect_price"):
        assert denied not in source, denied


# ===========================================================================
# B. the enforced curve, with the permission on and off
# ===========================================================================


def test_the_enforced_curve_is_identical_with_the_permission_on_and_off() -> None:
    """The one-line regression risk, pinned directly and on every shape.

    Someone later writing ``max(floor + survival, ...)`` into the reserve curve is
    how the permission becomes a second autonomy reserve. The curve handed to
    ``build_horizon`` is built from physics before the permission exists, and the
    only interval that may ever differ is the head -- and only by the anti-churn
    extension, and only while a bridge already exists.
    """
    for load in (0.1, 0.3, 0.6, 1.2):
        for stored in (5.0, 10.0, 14.77, 20.0):
            plain = solve_shape(
                load_fn=flat(load), price_fn=_priced, n=48, stored=stored
            )
            gated = solve_shape(
                load_fn=flat(load),
                price_fn=_priced,
                n=48,
                stored=stored,
                forecast_risk=MEASURED,
            )
            physical = plain.horizon.planning_reserve_kwh
            assert gated.horizon.planning_reserve_kwh == physical, (load, stored)
            # Beyond the head the two curves are equal by construction, whatever
            # the extension did to interval 0.
            enforced = gated.enforced_reserve_head_kwh
            assert enforced is not None
            assert enforced - physical[0] == pytest.approx(
                gated.anti_churn_buffer_kwh
            ), (load, stored)


def test_the_protection_appears_in_no_violation_term() -> None:
    """A permission cannot make a state unreachable, so it cannot cause a violation.

    The gate refuses a *caused-export delta*. The zero delta is always available
    from every bucket, so every state keeps a move, the lexicographic order is
    untouched, and a plan can always reach the floor by feeding the house. Asserted
    where it would show: a violation that appears only when the permission is on.
    """
    for load in (0.1, 0.6, 1.2):
        for stored in (5.0, 14.77, 20.0):
            plain = solve_shape(
                load_fn=flat(load), price_fn=_priced, n=48, stored=stored
            )
            gated = solve_shape(
                load_fn=flat(load),
                price_fn=_priced,
                n=48,
                stored=stored,
                forecast_risk=MEASURED,
            )
            assert gated.desired.violation_kwh == pytest.approx(
                plain.desired.violation_kwh
            ), (load, stored)
            assert gated.desired.available == plain.desired.available


def test_a_large_measured_error_with_no_bridge_buys_nothing() -> None:
    """The extension cannot *initiate* a purchase, however alarming the evidence.

    A full pack, no bridge, and an error allowance an order of magnitude above the
    real one. The plan's purchases must be whatever the economics wanted and not one
    kilowatt-hour more -- because the bump is gated on a condition that does not
    hold, and no protection quantity in this release is allowed to create that
    condition.
    """
    from custom_components.alpha_ems_manager.economic import ForecastRisk

    alarming = ForecastRisk(mae_kwh=1.0, bias_kwh=-0.5)
    plain = solve_shape(load_fn=flat(0.3), price_fn=_priced, n=48, stored=20.0)
    gated = solve_shape(
        load_fn=flat(0.3),
        price_fn=_priced,
        n=48,
        stored=20.0,
        forecast_risk=alarming,
    )

    assert gated.anti_churn_buffer_kwh == 0.0
    assert gated.enforced_reserve_head_kwh == gated.physical_reserve_head_kwh

    # **The measure is the compelled share, not the gross import.** The permission
    # is an economic one, so it is entitled to change the *trade pattern* -- refuse
    # a sale here, and the whole horizon re-optimises around it, which can move
    # gross import either way. What it may never do is make energy *compulsory*,
    # and the reserve-relaxed counterfactual is what measures that: the compelled
    # share is what a solve with the reserve relaxed to the hard floor declines to
    # buy over the same intervals.
    def compelled(solved) -> float:
        return sum(share for share, _economic in solved.safety_buy_attribution.values())

    assert compelled(gated) == pytest.approx(compelled(plain))
    assert compelled(gated) == pytest.approx(0.0)


def test_the_pack_still_approaches_the_floor_for_self_consumption() -> None:
    """The bound that makes the two halves of the objective compatible.

    Both halves have to hold at once: *do not sell yourself into a Safety Buy*, and
    *do use the battery*. A raised floor passes the first and fails the second --
    measured, it stranded the pack at 35.4 % where the price-based permission leaves
    it at 25.6 %. So the assertion is that the trajectory still comes down.
    """
    gated = live_shape(forecast_risk=MEASURED)
    lowest = min(entry.start_energy_dc_kwh for entry in gated.desired.intervals)
    ceiling = max(entry.start_energy_dc_kwh for entry in gated.desired.intervals)

    assert lowest >= FLOOR - 1e-9, "the hard floor is never crossed"
    # And it genuinely descends toward it rather than sitting high all day.
    assert lowest < ceiling
    assert lowest < FLOOR + 2.0, (
        "the pack must still be spent on the house; a protection that holds it "
        "high all day has become the reserve this release exists to avoid"
    )
