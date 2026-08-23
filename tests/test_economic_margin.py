"""The per-kWh margin on marginal grid-caused charging.

**Why this exists.** ``minimum_trade_gain_eur`` is a fixed amount a discretionary
run must earn before it is worth starting. It gates *thin* trades and it does not
scale: measured on the released beta.17 optimizer, once a trade cleared the fixed
gain the volume behind it was unconstrained, and a **14.230 kWh** round trip was
planned while earning **0.0371 EUR per grid-caused kWh**. The fixed gain was
cleared once, by the trade as a whole.

``grid_charge_margin_eur_per_kwh`` is the additional requirement that scales. The
two are different quantities and both remain.

Where it is charged, and why that is the only place it can be
-------------------------------------------------------------

The value table is ``value[bucket][run_state]``. There is no accumulated-run-energy
axis, so at the moment the per-run fee is charged the search does not yet know how
large the run will become -- a cost proportional to a whole run's energy cannot be
expressed there without adding an axis and multiplying the state space.

A cost proportional to **this interval's own** marginal grid-caused charging can,
and needs nothing new. Adding ``margin * kWh`` to an interval's cost is exactly the
requirement "this energy must earn at least ``margin`` per kWh beyond what it costs
to buy": the search takes the move only when its benefit clears purchase cost plus
margin.

The four exemptions are structural, not written
------------------------------------------------

Nothing in the implementation names an exemption, and these tests prove each one
falls out of the basis or the objective:

* **ambient production absorption** causes no import beyond the idle baseline, so
  the basis is zero;
* **the sun's share of a mixed quarter** is not marginal import, so only the grid
  share is charged;
* **load-serving discharge and export** are not charging at all;
* **reserve feasibility** outranks every cost, because the objective compares
  ``(violation, cost)`` lexicographically -- so no margin, however absurd, can
  make the house go short.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    MAX_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    MIN_GRID_CHARGE_MARGIN_EUR_PER_KWH,
)
from custom_components.alpha_ems_manager.economic import IntervalPrice, solve
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_economic_model import (
    EVERYTHING,
    FLOOR_PERCENT,
    horizon_for,
    reference_table,
)

TABLE = reference_table()
FLOOR = TABLE.limits.energy_for_soc(FLOOR_PERCENT)
CEILING = TABLE.limits.energy_for_soc(100.0)

#: The arbitrage shape the pathology was measured on: six cheap quarters, six
#: dearer ones, and enough room to move a lot of energy between them.
BUY = 0.20


def arbitrage(
    *,
    spread: float,
    margin: float,
    gain: float = 0.10,
    production: float = 0.0,
    load: float = 0.10,
    reserve: float | None = None,
    start: float | None = None,
):
    """Return a plan over a cheap block followed by a dearer one."""
    count = 12
    sell = BUY + spread
    horizon = horizon_for(
        TABLE,
        demands=[
            IntervalDemand(
                index=index,
                baseline_kwh=load,
                pv_kwh=production if index < 6 else 0.0,
            )
            for index in range(count)
        ],
        prices=[
            IntervalPrice(
                import_eur_kwh=BUY if index < 6 else sell,
                export_eur_kwh=(BUY if index < 6 else sell) * 0.9,
            )
            for index in range(count)
        ],
        reserve_kwh=[FLOOR if reserve is None else reserve] * count,
    )
    return solve(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=FLOOR if start is None else start,
        terminal_floor_kwh=FLOOR,
        minimum_trade_gain_eur=gain,
        permitted=EVERYTHING,
        grid_charge_margin_eur_per_kwh=margin,
    )


def traded(plan) -> bool:
    """Return whether a real arbitrage trade survived, not just load serving."""
    return plan.marginal_grid_charge_kwh > 1.0


# ===========================================================================
# A. the pathology, and its elimination
# ===========================================================================


def test_the_fixed_gain_alone_lets_a_large_thin_trade_through() -> None:
    """The defect, reproduced. This is what the new margin exists for.

    The fixed gain is cleared once by the trade as a whole, so the volume behind
    it is unconstrained: fourteen kilowatt-hours move for under four cents each.
    """
    plan = arbitrage(spread=0.10, margin=0.0)

    assert plan.marginal_grid_charge_kwh == pytest.approx(14.230, abs=0.01)
    earned_per_kwh = -plan.cost_eur / plan.marginal_grid_charge_kwh
    assert earned_per_kwh < 0.02
    # And the fixed gain was charged, which is precisely why it did not help.
    assert plan.switching_cost_eur > 0.0


def test_a_margin_of_ten_cents_a_kwh_eliminates_it() -> None:
    """The same shape at the candidate default. The trade does not survive."""
    plan = arbitrage(spread=0.10, margin=0.10)

    assert not traded(plan)
    assert plan.marginal_grid_charge_kwh < 1.0


def test_a_genuinely_profitable_trade_still_happens() -> None:
    """The counterexample. A margin that refused everything would be useless."""
    plan = arbitrage(spread=0.40, margin=0.10)

    assert traded(plan)
    assert plan.cost_eur < 0.0


# ===========================================================================
# B. the boundary, stated exactly
# ===========================================================================

#: Measured by bisection on the shape above: the trade survives while the margin
#: is below this and dies at it. Pinned so the boundary cannot drift silently.
FLIP_EUR_PER_KWH = 0.037129


def test_the_boundary_is_where_net_benefit_per_kwh_meets_the_margin() -> None:
    """Below it the trade happens, above it the trade does not.

    The rule is **strict**: the search prefers a strictly better candidate, so a
    trade whose net benefit exactly equals the margin is not taken -- it is
    indifferent, and indifference loses to doing nothing. Documented here because
    "at the threshold" has to mean something definite.
    """
    assert traded(arbitrage(spread=0.10, margin=FLIP_EUR_PER_KWH - 0.001))
    assert not traded(arbitrage(spread=0.10, margin=FLIP_EUR_PER_KWH + 0.001))


def test_the_margin_charged_is_the_energy_times_the_rate() -> None:
    """The reported figure is arithmetic over the basis, not an estimate."""
    plan = arbitrage(spread=0.40, margin=0.05)

    assert plan.grid_charge_margin_eur == pytest.approx(
        0.05 * plan.marginal_grid_charge_kwh, abs=1e-9
    )


def test_a_zero_margin_is_exactly_the_previous_release() -> None:
    """The default changes nothing, which is what makes the upgrade safe."""
    without = arbitrage(spread=0.10, margin=0.0)
    defaulted = arbitrage(spread=0.10, margin=DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH)

    assert DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH == 0.0
    assert defaulted.cost_eur == pytest.approx(without.cost_eur)
    assert defaulted.grid_charge_margin_eur == 0.0
    assert [e.action for e in defaulted.intervals] == [
        e.action for e in without.intervals
    ]


def test_the_configured_range_is_sane() -> None:
    """A margin larger than any plausible price would only ever mean 'never'."""
    assert MIN_GRID_CHARGE_MARGIN_EUR_PER_KWH == 0.0
    assert MAX_GRID_CHARGE_MARGIN_EUR_PER_KWH >= 1.0


# ===========================================================================
# C. the four exemptions, none of them written as an exemption
# ===========================================================================


def test_ambient_production_absorption_is_never_charged() -> None:
    """An absurd margin does not stop the battery taking free sunshine.

    Absorption causes no import beyond the idle baseline, so the basis is zero.
    Nothing in the implementation says "except absorption".
    """
    plan = arbitrage(spread=0.10, margin=5.0, production=2.0)

    absorbed = sum(entry.battery_charge_ac_kwh for entry in plan.intervals[:6])
    assert absorbed > 1.0
    assert plan.marginal_grid_charge_kwh < 0.3
    assert plan.grid_charge_margin_eur < 0.3 * 5.0 + 1e-9


def test_only_the_grid_share_of_a_mixed_quarter_is_charged() -> None:
    """Sun and grid in the same interval: the sun's share owes nothing.

    The basis is marginal grid import, so a quarter part-supplied by production is
    charged on the part that actually came from the meter -- strictly less than the
    battery movement, which is what makes multiplying total charge by the margin
    the wrong implementation.
    """
    plan = arbitrage(spread=0.40, margin=0.02, production=0.8, load=0.10)

    charged = sum(entry.battery_charge_ac_kwh for entry in plan.intervals[:6])
    assert charged > 0.0
    assert plan.marginal_grid_charge_kwh < charged
    assert plan.grid_charge_margin_eur == pytest.approx(
        0.02 * plan.marginal_grid_charge_kwh, abs=1e-9
    )


def test_load_serving_discharge_is_never_charged() -> None:
    """An absurd margin does not stop the battery supplying the house.

    A discharge is not charging. Load avoidance is the most valuable thing the
    battery does and it is outside this margin by construction.
    """
    plan = arbitrage(spread=0.0, margin=99.0, load=1.0, start=FLOOR + 6.0)

    discharged = sum(entry.battery_discharge_ac_kwh for entry in plan.intervals)
    assert discharged > 1.0


def test_the_reserve_still_wins_at_an_absurd_margin() -> None:
    """99 EUR/kWh does not make the house go short. **The safety property.**

    Not an exemption: the objective compares ``(violation, cost)``
    lexicographically, so no cost term can outrank reserve feasibility. It can
    only order paths that violate the requirement equally.
    """
    for margin in (0.0, 1.0, 99.0, 10_000.0):
        plan = arbitrage(spread=0.0, margin=margin, reserve=15.5, start=5.0)
        peak = max(
            entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
            for entry in plan.intervals
        )
        assert peak == pytest.approx(15.5, abs=1e-9), margin
        assert plan.available, margin


def test_the_reserve_buy_is_still_minimal_under_an_absurd_margin() -> None:
    """It buys to the requirement and stops -- not to the ceiling, and not less."""
    plan = arbitrage(spread=0.0, margin=99.0, reserve=12.0, start=5.0)

    peak = max(
        entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
        for entry in plan.intervals
    )
    assert peak == pytest.approx(12.0, abs=1e-9)
    assert peak < CEILING


# ===========================================================================
# D. awkward prices
# ===========================================================================


def test_a_negative_import_price_is_not_taxed_into_refusal() -> None:
    """Being paid to consume is not arbitrage, and the margin must not block it.

    With a negative import price the charge earns money directly. The margin is a
    cost per kWh, so a large enough one *can* outweigh a small negative price --
    that is arithmetic, not a defect. What matters is that a strongly negative
    price still wins, which is what this pins.
    """
    count = 8
    horizon = horizon_for(
        TABLE,
        demands=[
            IntervalDemand(index=index, baseline_kwh=0.10, pv_kwh=0.0)
            for index in range(count)
        ],
        prices=[
            IntervalPrice(
                import_eur_kwh=-0.50 if index < 4 else 0.30,
                export_eur_kwh=-0.55 if index < 4 else 0.25,
            )
            for index in range(count)
        ],
        reserve_kwh=[FLOOR] * count,
    )
    plan = solve(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=FLOOR,
        terminal_floor_kwh=FLOOR,
        minimum_trade_gain_eur=0.10,
        permitted=EVERYTHING,
        grid_charge_margin_eur_per_kwh=0.10,
    )

    assert plan.marginal_grid_charge_kwh > 1.0
    assert plan.cost_eur < 0.0


def test_a_zero_price_interval_is_still_charged_the_margin() -> None:
    """Free energy is not free of the requirement to be worth storing.

    A known zero is a price, not an absence, and storing at zero still occupies
    headroom and costs a cycle. The margin applies, which is the point of it.
    """
    count = 8
    horizon = horizon_for(
        TABLE,
        demands=[
            IntervalDemand(index=index, baseline_kwh=0.10, pv_kwh=0.0)
            for index in range(count)
        ],
        prices=[
            IntervalPrice(
                import_eur_kwh=0.0 if index < 4 else 0.02,
                export_eur_kwh=0.0 if index < 4 else 0.01,
            )
            for index in range(count)
        ],
        reserve_kwh=[FLOOR] * count,
    )
    common = {
        "table": TABLE,
        "horizon": horizon,
        "start_energy_kwh": FLOOR,
        "terminal_floor_kwh": FLOOR,
        "minimum_trade_gain_eur": 0.0,
        "permitted": EVERYTHING,
    }
    free = solve(grid_charge_margin_eur_per_kwh=0.0, **common)
    charged = solve(grid_charge_margin_eur_per_kwh=0.10, **common)

    assert free.marginal_grid_charge_kwh >= charged.marginal_grid_charge_kwh


def test_the_margin_never_reaches_the_reported_euro_figure() -> None:
    """``cost_eur`` stays reconcilable to grid energy at the interval's prices.

    The margin is notional -- nobody pays it -- so folding it into the reported
    cost would break the invariant that every euro in the payload can be checked
    against the flows beside it. Reported separately, exactly like the switching
    cost.
    """
    plan = arbitrage(spread=0.40, margin=0.05)

    for entry in plan.intervals:
        expected = entry.grid_import_kwh * (
            entry.import_price_eur_kwh or 0.0
        ) - entry.grid_export_kwh * (entry.export_price_eur_kwh or 0.0)
        assert entry.cost_eur == pytest.approx(expected, abs=1e-9)
    assert plan.grid_charge_margin_eur > 0.0


def test_the_margin_does_not_fragment_a_run() -> None:
    """A charged campaign stays one campaign; the margin is not a run boundary.

    A per-interval cost could in principle make the search prefer gaps. It must
    not: the fixed per-run gain still discourages chattering, and the margin only
    scales what a run has to earn.
    """
    plan = arbitrage(spread=0.40, margin=0.05)

    charging = [
        index
        for index, entry in enumerate(plan.intervals)
        if entry.battery_charge_ac_kwh > 0.01
    ]
    assert charging
    # Contiguous: no interior gap in the charging block.
    assert charging == list(range(charging[0], charging[-1] + 1))
