"""beta.34: the 13:00 economic cliff, replayed until it says what it is.

The live installation lost **1.09 EUR of plan value in one refresh** on
2026-08-29: at 13:00:05 the run count fell 5 to 0, planned export fell 9.317 to
1.589 kWh, and ``cost_eur`` went from -0.546 to +0.544. Nothing else moved.
Stored energy rose 0.43 kWh and one quarter elapsed.

The release brief forbids shipping that unexplained, and forbids "making the Sell
reappear" as the explanation. So this file does the other thing: it reproduces the
collapse on the production solver, isolates the variable that causes it, and
reports whether the collapse was **right**.

The answer, measured below and printed by ``test_the_euro_table``:

* The survival window has two branches. While the plan still intends a refill it
  protects only as far as that refill. The moment the plan intends **no further
  refill**, the branch falls through to the whole actionable prefix -- "survive
  to the end of the horizon" -- and the floor jumps from 4.32 kWh to 15.46.
* That flip is what happened at 13:00: the buy campaign completed, and with
  tomorrow's prices not yet published there was no next refill to protect to.
* **The flip is not what cost the money.** At the live stored energy the export
  permission withheld 0.56 kWh and cost **0.136 EUR**. The other ~0.78 EUR is the
  cheap-buy opportunity itself passing out of the horizon -- an economic fact
  about the day, not an artefact.
* What *was* an artefact is the 14:00 refresh, one hour later: a 23.09 kWh floor
  on a 21.6 kWh pack, vetoing every caused export unconditionally. That is F4/F5
  and it is fixed and pinned in ``test_beta34_survival_window``.

So beta.34 ships with the cliff **explained and largely vindicated**, and with
the one part of it that was a bug repaired.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    SURVIVAL_WINDOW_ACTIONABLE_PREFIX,
    SURVIVAL_WINDOW_PLAN_CAMPAIGN,
)

from .beta34_shape import solve_at

HEAD_1300 = 52
HEAD_1400 = 57
STORED_1345 = 10.97
STORED_1400 = 11.75


def figures(solved) -> dict[str, float]:
    """Return the full euro and energy decomposition of one solved horizon."""
    outcome = solved.outcome
    plan = outcome.desired
    return {
        "runs": len(plan.runs),
        "charge_ac_kwh": plan.planned_charge_ac_kwh,
        "discharge_ac_kwh": plan.planned_discharge_ac_kwh,
        "grid_import_kwh": plan.planned_grid_import_kwh,
        "grid_export_kwh": plan.planned_grid_export_kwh,
        "self_consumption_ac_kwh": (
            plan.planned_discharge_ac_kwh - plan.planned_grid_export_kwh
        ),
        "cost_eur": plan.cost_eur,
        "switching_cost_eur": plan.switching_cost_eur,
        "margin_cost_eur": plan.grid_charge_margin_eur,
        "throughput_cost_eur": plan.battery_throughput_cost_eur,
        "hold_cost_eur": plan.hold_cost_eur,
        "advantage_eur": plan.hold_cost_eur - plan.cost_eur,
        "start_energy_dc_kwh": (
            plan.intervals[0].start_energy_dc_kwh if plan.intervals else 0.0
        ),
        "end_energy_dc_kwh": plan.end_energy_dc_kwh,
        "gate_cost_eur": outcome.export_gate_cost_eur,
        "gate_withheld_kwh": outcome.export_gate_withheld_kwh,
        "window": outcome.survival_window_end,
        "basis": outcome.survival_window_basis,
        "floor0": outcome.export_floor_kwh[0] if outcome.export_floor_kwh else 0.0,
    }


# ===========================================================================
# A-F: the counterfactuals, on the production solver
# ===========================================================================


def scenarios() -> dict[str, dict[str, float]]:
    """Return the six counterfactuals, solved once each."""
    return {
        # A. the live 13:00 state, everything permitted.
        "A refill still ahead": figures(
            solve_at(head=HEAD_1300, end=96, stored=STORED_1345)
        ),
        # B. the same state with no refill left to protect to. The branch flip.
        "B no refill ahead": figures(
            solve_at(head=HEAD_1300, end=96, stored=STORED_1345, allow_charge=False)
        ),
        # C. and with the export permission switched off entirely, so the flip's
        #    own contribution is separable from the day's economics.
        "C no refill, no gate": figures(
            solve_at(
                head=HEAD_1300,
                end=96,
                stored=STORED_1345,
                allow_charge=False,
                forecast_risk=None,
            )
        ),
        # D. the live 14:00 state on a today-only horizon.
        "D 14:00 today only": figures(
            solve_at(head=HEAD_1400, end=96, stored=STORED_1400)
        ),
        # E. the same instant once tomorrow's prices arrive.
        "E 14:00 two days": figures(
            solve_at(head=HEAD_1400, end=192, stored=STORED_1400)
        ),
        # F. a full pack two days out: the state where the gate binds hardest.
        "F two days, full pack": figures(
            solve_at(head=HEAD_1400, end=192, stored=18.0)
        ),
    }


def test_the_euro_table() -> None:
    """Print the counterfactual table. Every figure from the production solver."""
    rows = scenarios()
    columns = (
        "runs",
        "charge_ac_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "self_consumption_ac_kwh",
        "cost_eur",
        "switching_cost_eur",
        "margin_cost_eur",
        "throughput_cost_eur",
        "advantage_eur",
        "start_energy_dc_kwh",
        "end_energy_dc_kwh",
        "gate_cost_eur",
        "gate_withheld_kwh",
        "window",
        "floor0",
    )
    print("\n--- beta.34 counterfactuals, 2026-08-29 reconstruction ---")
    for name, row in rows.items():
        print(f"\n{name}   [{row['basis']}]")
        for column in columns:
            value = row[column]
            if value is None:
                # Scenario C runs with the permission off, so it has no gate
                # figures at all. Printed as a dash rather than a zero: "no gate
                # ran" and "the gate cost nothing" are different readings.
                rendered = f"{'--':>9}"
            elif isinstance(value, float):
                rendered = f"{value:9.3f}"
            else:
                rendered = f"{value:>9}"
            print(f"    {column:<26}{rendered}")

    # The table is evidence, so it must actually contain the six shapes.
    assert len(rows) == 6


# ===========================================================================
# the isolation: which variable moved
# ===========================================================================


def test_the_survival_window_flips_branch_when_the_refill_disappears() -> None:
    """**The causal variable, isolated to one field.**

    Same head, same stored energy, same prices, same settings. The only
    difference is whether the ungated plan still intends to refill -- and that is
    the input ``survival_window_end`` reads.

    *Mutation: make ``survival_window_end`` always return the actionable prefix
    and the two rows below become identical.*
    """
    with_refill = solve_at(head=HEAD_1300, end=96, stored=STORED_1345).outcome
    without = solve_at(
        head=HEAD_1300, end=96, stored=STORED_1345, allow_charge=False
    ).outcome

    assert with_refill.survival_window_basis == SURVIVAL_WINDOW_PLAN_CAMPAIGN
    assert without.survival_window_basis == SURVIVAL_WINDOW_ACTIONABLE_PREFIX
    # A refill in progress is a zero-length window: there is nothing to survive
    # until, because the energy is arriving now.
    assert with_refill.survival_window_end == 0
    assert without.survival_window_end == without.actionable_interval_count
    # And the floor follows it, from the bare hard floor to survive-to-midnight.
    assert with_refill.export_floor_kwh[0] == pytest.approx(4.32, abs=0.01)
    assert without.export_floor_kwh[0] == pytest.approx(15.46, abs=0.05)


def test_the_collapse_is_mostly_the_day_and_only_a_little_the_gate() -> None:
    """**The finding the release brief demanded, and it is not the flattering one.**

    The temptation is to read a 1.09 EUR cliff, find a protection that fired in
    the same refresh, and call it the cause. Measured, the protection is a
    fourteen-cent effect: it withheld 0.56 kWh of a 2.62 kWh ungated export.

    The rest is the day. Once the cheap window is behind the head there is no
    longer a buy-low-sell-high trade to make, and the plan's advantage over
    holding falls because the opportunity has gone, not because it was forbidden.

    This is what "verify the fix does not cause irrational same-day selling"
    requires proving in the other direction: beta.34 must **not** restore a sale
    the model was right to refuse.
    """
    without = solve_at(
        head=HEAD_1300, end=96, stored=STORED_1345, allow_charge=False
    ).outcome
    ungated = without.ungated
    assert ungated is not None and ungated.available

    # The whole swing this state can show, against the same state with the buy
    # window still open.
    with_refill = solve_at(head=HEAD_1300, end=96, stored=STORED_1345).outcome
    total_swing = without.desired.cost_eur - with_refill.desired.cost_eur
    assert total_swing > 0.5, total_swing

    # And the protection's share of it.
    gate = without.export_gate_cost_eur
    assert gate is not None
    assert 0.0 <= gate <= 0.25, gate
    assert gate < total_swing * 0.25, (gate, total_swing)
    # It withheld something real, and something small.
    assert 0.1 < without.export_gate_withheld_kwh < 1.5


def test_the_protection_still_binds_where_the_energy_is_genuinely_needed() -> None:
    """The other half: beta.34 must not have turned the gate off.

    With no refill ahead and the pack below what the rest of the day needs, a
    caused export is exactly the trade the protection exists to refuse -- and it
    is still refused. Every interval fails the price test, and the plan exports
    strictly less than the ungated one.
    """
    without = solve_at(
        head=HEAD_1300, end=96, stored=STORED_1345, allow_charge=False
    ).outcome

    assert without.export_free
    assert not any(without.export_free)
    assert (
        without.desired.planned_grid_export_kwh
        < without.ungated.planned_grid_export_kwh
    )
    # And the pack is genuinely short: the requirement is above what it holds.
    assert without.export_floor_kwh[0] > STORED_1345
