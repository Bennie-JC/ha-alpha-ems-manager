"""beta.32 R5: the battery serves the house at low load, without selling.

**The defect, measured.** With ``allow_battery_export`` off and a house drawing
0.19 kWh in a quarter, beta.31 discharged **nothing** and imported 4.560 kWh at
0.35 EUR/kWh through a dear window -- EUR 2.79 a day worse than necessary. The
setting a user reads as "do not sell my battery" silently meant "do not use my
battery".

**Root cause, and it is arithmetic rather than logic.** The smallest non-zero
discharge the economic state space can express is one bucket: 0.263523 kWh DC,
**0.250 kWh AC**. When the residual load is smaller than that, no discharge move
fits. The solver's only options are to hold -- importing the whole load -- or to
overshoot, and the overshoot lands on the meter as export, which needs a
permission that is switched off. So every discharge became impermissible.

**Why the obvious fixes were rejected, each on evidence.**

* *Widen the permitted spill.* The spill is ``0.250 - load``, bounded by one whole
  bucket, not by the 0.025 kWh actuator step an earlier draft assumed. At 0.19
  it is 0.060 kWh and at 0.05 it is 0.200 -- approaching 6 kWh a day. That is not
  actuator tolerance, it is several kilowatt-hours of metered export against an
  explicit instruction.
* *Refine the lattice.* Measured: a bucket fine enough to serve a 0.05 kWh quarter
  needs 864 states and **9.45 s per solve** against the present 104 ms. Even the
  existing band floor of 0.15 costs 282 ms and still cannot serve 0.05 or 0.10.
* *Prefer under-serving by tie-break.* There is no tie. Over-serving earns
  ``export_price x export_kwh`` and under-serving pays ``import_price x
  import_kwh``, so the two moves have different costs and a preference would have
  to alter cost ordering.

**The fix.** Serving the house was never a lattice decision.
``CONTROL_LIVE_DISPATCH_INTENTS`` is ``{grid_charge, net_export}`` -- Stage B
commands nothing for ``serve_load`` -- so a battery covering residual load is
*ambient inverter behaviour*, exactly as absorbing surplus production already is.
beta.31 modelled the ambient charge direction and the ambient discharge direction
not at all. Now the hold interval carries a continuous ambient discharge, clamped
by the residual load, by discharge power, and by the energy above the floor.

It applies **only** where the lattice cannot express the service, which is what
keeps it surgical: above one discharge bucket the solver's own moves fit and must
keep being chosen, because a load-serving discharge is a real economic decision
with a real published action.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    COUNTERFACTUAL_AMBIENT_SELF_CONSUMPTION,
    COUNTERFACTUAL_IDLE_IMPORT,
    ECONOMIC_ACTION_EXPORT,
)
from custom_components.alpha_ems_manager.economic import _ac_by_delta

from .beta32_harness import FLOOR, LIMITS, flat, solve_shape

#: The dear-then-cheap shape every low-load case is driven against: six hours at
#: 0.35 EUR/kWh, then 0.12. If the battery can serve the house at all, it should.
DEAR_THEN_CHEAP = lambda index: 0.35 if index < 24 else 0.12  # noqa: E731


def smallest_discharge_ac_kwh(table) -> float:
    """Return the least AC energy any single discharge move delivers."""
    found = [
        discharge
        for delta, (_charge, discharge) in _ac_by_delta(table).items()
        if delta < 0 and discharge > 0.0
    ]
    return min(found)


def test_the_lattice_quantum_is_the_root_cause_and_it_is_measured_here() -> None:
    """The arithmetic the whole design rests on, asserted rather than assumed.

    Hand-computed: the bucket is chosen so a full-power charge quarter divides
    exactly, giving 0.263523 kWh DC, and one bucket discharged through
    ``eta_discharge`` = 0.948683 delivers 0.250 kWh AC. Any residual load below
    that cannot be served by a single move without overshooting.
    """
    outcome = solve_shape(
        load_fn=flat(0.19), price_fn=DEAR_THEN_CHEAP, n=4, stored=16.0
    )
    table = outcome.table

    assert table.bucket_kwh == pytest.approx(0.263523, abs=5e-6)
    assert smallest_discharge_ac_kwh(table) == pytest.approx(0.250, abs=5e-4)
    # And the spill at the plan's own worked example is far above the actuator
    # step, which is why "bounded actuator tolerance" was the wrong frame.
    assert pytest.approx(0.060, abs=1e-9) == 0.250 - 0.19


@pytest.mark.parametrize("load_kwh", [0.05, 0.10, 0.19, 0.22, 0.24])
def test_a_load_below_one_bucket_is_served_from_the_battery(load_kwh: float) -> None:
    """**The defect, gone.** Export disabled, dear window, sub-bucket load.

    The battery supplies the load, essentially nothing crosses the meter in either
    direction, and the interval costs nothing. beta.31 discharged 0.000 and bought
    the lot.
    """
    outcome = solve_shape(
        load_fn=flat(load_kwh),
        price_fn=DEAR_THEN_CHEAP,
        n=48,
        stored=16.0,
        allow_export=False,
    )
    window = outcome.desired.intervals[:24]

    served = sum(entry.ambient_self_consumption_ac_kwh for entry in window)
    # Every quarter of the dear window, served from the pack.
    assert served == pytest.approx(load_kwh * 24, rel=0.02)
    assert sum(entry.battery_discharge_ac_kwh for entry in window) == pytest.approx(
        served, rel=0.02
    )
    # Nothing sold, and nothing bought that the battery could have covered.
    assert sum(entry.grid_export_kwh for entry in window) == pytest.approx(
        0.0, abs=1e-9
    )
    assert sum(entry.grid_import_kwh for entry in window) < 0.10
    # And it is labelled for what it is: not a dispatched discharge.
    assert all(
        entry.counterfactual_basis == COUNTERFACTUAL_AMBIENT_SELF_CONSUMPTION
        for entry in window
        if entry.ambient_self_consumption_ac_kwh > 0.0
    )


def test_a_load_above_one_bucket_is_unchanged_from_beta31() -> None:
    """The surgical half: where a move fits, the solver keeps choosing it.

    0.31 kWh a quarter exceeds the 0.250 kWh quantum, so a one-bucket discharge
    under-serves rather than overshooting and beta.31 could already take it. The
    figures are beta.31's, measured: 6.000 kWh discharged, 1.440 kWh imported.
    Ambient service must not displace that -- a load-serving discharge is a real
    decision with a real published action, and replacing it with ``hold`` would
    hide the battery covering an expensive evening.
    """
    outcome = solve_shape(
        load_fn=flat(0.31),
        price_fn=DEAR_THEN_CHEAP,
        n=48,
        stored=16.0,
        allow_export=False,
    )
    window = outcome.desired.intervals[:24]

    assert sum(entry.battery_discharge_ac_kwh for entry in window) == pytest.approx(
        6.000, abs=0.01
    )
    assert sum(entry.grid_import_kwh for entry in window) == pytest.approx(
        1.440, abs=0.01
    )
    # No ambient service at all: the lattice did not need help.
    assert sum(entry.ambient_self_consumption_ac_kwh for entry in window) == 0.0
    assert all(
        entry.counterfactual_basis == COUNTERFACTUAL_IDLE_IMPORT for entry in window
    )


@pytest.mark.parametrize("load_kwh", [0.05, 0.10, 0.19, 0.22, 0.24, 0.31])
def test_export_disabled_never_plans_an_export_at_any_load(load_kwh: float) -> None:
    """The user's instruction, kept. Nothing crosses the meter outward.

    Not "within a tolerance" and not "within a bucket": zero. The ambient model
    reaches the same physical outcome the coarse lattice could not, and it reaches
    it without selling anything.
    """
    outcome = solve_shape(
        load_fn=flat(load_kwh),
        price_fn=DEAR_THEN_CHEAP,
        n=48,
        stored=16.0,
        allow_export=False,
    )
    plan = outcome.desired

    assert sum(entry.grid_export_kwh for entry in plan.intervals) == pytest.approx(
        0.0, abs=1e-9
    )
    assert not [
        entry for entry in plan.intervals if entry.action == ECONOMIC_ACTION_EXPORT
    ]
    # And no campaign claims a sell objective, so none can be announced as one.
    assert not [
        campaign
        for campaign in plan.campaigns
        if campaign.direction == "discharge" and campaign.objective_kwh > 0.0
    ]


@pytest.mark.parametrize("load_kwh", [0.05, 0.19, 0.31])
def test_export_enabled_behaviour_is_untouched(load_kwh: float) -> None:
    """Invariant 10: the fix must not damage ordinary economic export.

    Measured against beta.31 on the same shapes. The ambient model is gated on the
    lattice gap, not on the mode, but with export enabled the solver has a
    profitable overshoot available and takes it -- exactly as before.
    """
    outcome = solve_shape(
        load_fn=flat(load_kwh),
        price_fn=DEAR_THEN_CHEAP,
        n=48,
        stored=16.0,
        allow_export=True,
    )
    window = outcome.desired.intervals[:24]
    expected = {0.05: 9.300, 0.19: 5.440, 0.31: 2.850}

    assert sum(entry.grid_export_kwh for entry in window) == pytest.approx(
        expected[load_kwh], abs=0.01
    )
    # Ambient service never fires here: an overshoot that earns export revenue is
    # a better move than holding, so the solver has no sub-lattice gap to fill.
    assert sum(entry.ambient_self_consumption_ac_kwh for entry in window) == 0.0


@pytest.mark.parametrize("stored_kwh", [16.0, 8.0, 5.0, 4.5])
def test_the_hard_floor_survives_ambient_service(stored_kwh: float) -> None:
    """**Invariant 5, and the reason the forward walk carries a drain offset.**

    The ambient discharge is real energy the bucket cannot represent, so if the
    reported state of charge never fell, the clamp would read a full pack for ever
    and authorise service straight through the floor. The walk therefore
    accumulates the drain and measures every clamp against it.

    Driven from four starting states, the last two already near the floor, with a
    dear price throughout so ambient service is the only relief available.
    """
    outcome = solve_shape(
        load_fn=flat(0.19),
        price_fn=lambda index: 0.35,
        n=48,
        stored=stored_kwh,
        allow_export=False,
    )
    plan = outcome.desired

    assert min(entry.start_energy_dc_kwh for entry in plan.intervals) >= FLOOR - 1e-6
    # And the trajectory is coherent: energy served is energy the pack lost.
    #
    # **Stated as the balance rather than as "the pack ends lower".** Since
    # beta.41 the recursion can *see* this depletion, so on a dear no-production
    # horizon it buys from the first interval to stay above the reserve -- the
    # trajectory rises and a test looking for a fall finds none. What it was
    # really asserting is that service comes out of the pack rather than out of
    # nowhere, and that is an identity, so it is checked as one.
    served = sum(entry.ambient_self_consumption_ac_kwh for entry in plan.intervals)
    if served > 0.0:
        moved = sum(entry.battery_state_service_dc_kwh for entry in plan.intervals)
        assert moved > 0.0, "service that moves no state is the beta.40 defect"
        decided = sum(entry.battery_delta_dc_kwh for entry in plan.intervals)
        opening = plan.intervals[0].start_energy_dc_kwh
        assert opening + decided - moved == pytest.approx(
            plan.end_energy_dc_kwh, abs=1e-9
        )


def test_import_returns_when_the_pack_genuinely_cannot_serve() -> None:
    """Invariant 4: no artificial import, and no pretending either.

    A pack at the floor cannot self-consume, and the model must say so -- otherwise
    it would report that holding at 20 % costs nothing and remove the very pressure
    to buy that keeps the floor safe. Measured: 9.19 kWh imported from a 4.5 kWh
    start against 0.00 from a 16.0 kWh start.
    """
    low = solve_shape(
        load_fn=flat(0.19),
        price_fn=lambda index: 0.35,
        n=48,
        stored=4.5,
        allow_export=False,
    )
    full = solve_shape(
        load_fn=flat(0.19),
        price_fn=lambda index: 0.35,
        n=48,
        stored=16.0,
        allow_export=False,
    )

    assert sum(e.grid_import_kwh for e in full.desired.intervals) == pytest.approx(
        0.0, abs=1e-9
    )
    assert sum(e.grid_import_kwh for e in low.desired.intervals) > 8.0


def test_ambient_service_never_charges_and_never_exceeds_discharge_power() -> None:
    """Invariants 6 and 7, asserted directly on the published figures."""
    quarter_limit = LIMITS.max_discharge_kw * 0.25
    outcome = solve_shape(
        load_fn=flat(0.19),
        price_fn=DEAR_THEN_CHEAP,
        n=48,
        stored=16.0,
        allow_export=False,
        allow_charge=False,
    )
    plan = outcome.desired

    for entry in plan.intervals:
        assert entry.ambient_self_consumption_ac_kwh <= quarter_limit + 1e-9
        # Ambient service is a discharge. It may never appear beside a charge.
        if entry.ambient_self_consumption_ac_kwh > 0.0:
            assert entry.battery_charge_ac_kwh == 0.0


def test_the_gate_defaults_to_off_so_beta31_behaviour_is_the_fallback() -> None:
    """Unknown means not modelled -- the surplus-absorption doctrine, reused.

    With export *enabled* and no measured evidence, the ambient model must not
    fire, so an installation whose inverter genuinely does not self-consume plans
    exactly as it did. The gate is two triggers: measured evidence, or an export
    permission switched off (which makes ambient service the only physical answer
    to a residual load).
    """
    outcome = solve_shape(
        load_fn=flat(0.19),
        price_fn=lambda index: 0.35,
        n=8,
        stored=16.0,
        allow_export=True,
        ambient_self_consumption=False,
    )

    assert (
        sum(
            entry.ambient_self_consumption_ac_kwh for entry in outcome.desired.intervals
        )
        == 0.0
    )
