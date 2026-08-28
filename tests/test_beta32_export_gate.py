"""The export permission: measured evidence, two passes, and an audit figure.

Every expectation here is hand-computed from stated inputs. Nothing calls the code
under test to obtain an expected value -- the project's anti-tautology rule -- and
where a figure came from a measurement on the live installation, the measurement is
written into the assertion rather than recomputed.

The permission exists because beta.31 had exactly one thing standing between a
discretionary export and the 20 % floor: a **constant** 0.42 kWh margin, on a
reachability curve that is a flat line on every live refresh. Measured failure --
a quiet forecast let 3.950 kWh go at 0.29, took the pack to 22.0 %, and reality
arrived at 3.4x load: **0.833 kWh of compelled Safety Buy**.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import ADAPT_PROTECTION_CEILING
from custom_components.alpha_ems_manager.economic import (
    ForecastRisk,
    IntervalPrice,
    anti_churn_buffer_kwh,
    err_for,
    survival_curves,
    upper_net_demand_curve,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .beta32_harness import FLOOR, LIMITS, flat, live_shape, solve_shape

#: 0.948683 on this pack, and the square of it is 0.900000 exactly.
ETA = LIMITS.discharge_efficiency

#: The measured evidence a mature installation actually has.
MEASURED = ForecastRisk(mae_kwh=0.06, bias_kwh=-0.02, error_persistence=0.7)


def _priced(index: int) -> float:
    """Return a two-hour square wave between 0.10 and 0.30 EUR/kWh."""
    return 0.10 + 0.20 * ((index // 8) % 2)


# ===========================================================================
# A. err(k) -- one-sided, measured, and never zero on thin history
# ===========================================================================


def test_the_error_allowance_adds_only_measured_under_prediction() -> None:
    """``max(0, -bias) + rho * mae``, and the sign of bias is load bearing.

    ``bias_kwh`` is the mean *signed* error and is **positive when the model
    over-predicts**. Only under-prediction can strand the pack, so only a negative
    bias may widen the allowance -- a model that habitually over-predicts does not
    earn a discount and does not earn a penalty either.
    """
    demand = IntervalDemand(index=0, baseline_kwh=0.5, pv_kwh=0.0)

    # rho absent, so the conservative rung: rho = 1.
    assert err_for(demand, ForecastRisk(mae_kwh=0.06)) == pytest.approx(0.06)
    # A measured systematic under-prediction of 0.02 is added on top.
    assert err_for(demand, ForecastRisk(mae_kwh=0.06, bias_kwh=-0.02)) == pytest.approx(
        0.08
    )
    # Over-prediction changes nothing at all.
    assert err_for(demand, ForecastRisk(mae_kwh=0.06, bias_kwh=0.02)) == pytest.approx(
        0.06
    )
    # Half the errors cancel across a day, so half the MAE accumulates:
    # 0.02 + 0.5 * 0.06 = 0.05.
    assert err_for(
        demand, ForecastRisk(mae_kwh=0.06, bias_kwh=-0.02, error_persistence=0.5)
    ) == pytest.approx(0.05)


def test_sparse_history_yields_more_protection_and_never_zero() -> None:
    """The cascade's direction, which rev 1 of the design had backwards.

    At 11 learned days and 29.6 % confidence the rolling window has an MAE and no
    usable persistence ratio. An allowance that fell to zero there would remove all
    protection exactly where it is most needed, so a missing rho means **rho = 1**
    -- the conservative end -- and the allowance is therefore *larger* on thin
    history than on mature history.
    """
    demand = IntervalDemand(index=0, baseline_kwh=0.5, pv_kwh=0.0)
    thin = err_for(demand, ForecastRisk(mae_kwh=0.06))
    mature = err_for(demand, ForecastRisk(mae_kwh=0.06, error_persistence=0.4))

    assert thin > mature
    assert thin == pytest.approx(0.06)
    assert mature == pytest.approx(0.024)
    # And with nothing measured at all there is no claim, not a zero pretending to
    # be one: the *allowance* is zero, and the anti-churn floor of one lattice
    # bucket is what stops protection from being literally nothing.
    assert err_for(demand, ForecastRisk()) == 0.0
    assert anti_churn_buffer_kwh(
        (demand,),
        ForecastRisk(),
        window_end=1,
        bucket_kwh=0.263523,
        discharge_efficiency=ETA,
    ) == pytest.approx(0.263523)


# ===========================================================================
# B. the protective demand curve, and today's adaptation
# ===========================================================================


def test_todays_adaptation_is_one_sided_capped_and_today_only() -> None:
    """Three restrictions, each with a reason, each asserted separately.

    Four intervals of 0.5 kWh baseline against 0.1 kWh production, with the first
    two belonging to today. The unadapted protective demand is
    ``0.5 - 0.1 + 0.06 = 0.46`` per interval.
    """
    demands = tuple(
        IntervalDemand(index=i, baseline_kwh=0.5, pv_kwh=0.1) for i in range(4)
    )

    def curve(ratio: float | None):
        return upper_net_demand_curve(
            demands,
            ForecastRisk(mae_kwh=0.06, adaptation_ratio=ratio, today_interval_count=2),
            adaptation_ceiling=ADAPT_PROTECTION_CEILING,
        )

    # **Today only.** 0.5 * 1.2 - 0.1 + 0.06 = 0.56 for the two intervals that
    # belong to today; tomorrow keeps 0.46, because a ratio measured against today's
    # elapsed hours says nothing about tomorrow.
    values, applied, clipped = curve(1.2)
    assert values == pytest.approx((0.56, 0.56, 0.46, 0.46))
    assert applied == pytest.approx(1.2)
    assert clipped is False

    # **One-sided.** A quiet morning does not license selling more, so a ratio below
    # one is clamped to one and the curve is unchanged.
    values, applied, clipped = curve(0.5)
    assert values == pytest.approx((0.46, 0.46, 0.46, 0.46))
    assert applied == pytest.approx(1.0)

    # **Capped, and the clip is published.** A ratio of 3.0 is better explained by a
    # sensor fault than by occupancy: 0.5 * 1.5 - 0.1 + 0.06 = 0.71.
    values, applied, clipped = curve(3.0)
    assert values == pytest.approx((0.71, 0.71, 0.46, 0.46))
    assert applied == pytest.approx(ADAPT_PROTECTION_CEILING)
    assert clipped is True


def test_the_error_allowance_is_capped_by_the_forecast_it_corrects() -> None:
    """An allowance larger than the P50 it corrects is a different forecast.

    Building a second forecast is a stated non-goal, so the cumulative allowance is
    scaled back to the summed P50 rather than truncated -- scaling keeps the shape
    of the protection following the shape of the demand.
    """
    # 0.05 kWh of net demand per interval against a 0.30 kWh allowance: the
    # allowance would otherwise be six times the forecast.
    demands = tuple(
        IntervalDemand(index=i, baseline_kwh=0.05, pv_kwh=0.0) for i in range(4)
    )
    values, _applied, _clipped = upper_net_demand_curve(
        demands,
        ForecastRisk(mae_kwh=0.30),
        adaptation_ceiling=ADAPT_PROTECTION_CEILING,
    )

    p50 = sum(max(0.0, d.baseline_kwh - (d.pv_kwh or 0.0)) for d in demands)
    # Each interval is P50 plus its scaled share of the allowance, and the total
    # allowance is exactly the total P50: 0.05 + 0.05 = 0.10 per interval.
    assert values == pytest.approx((0.10, 0.10, 0.10, 0.10))
    assert sum(values) == pytest.approx(2 * p50)


# ===========================================================================
# C. the two curves, on the boundaries the audit named
# ===========================================================================


def test_the_survival_energy_is_dc_and_the_protect_price_carries_no_efficiency():
    """The one place where two boundaries meet, hand-computed at eta = 0.948683.

    Four intervals, 0.4 kWh AC of protective demand each, import prices 0.20 /
    0.30 / 0.40 / 0.10.

    * **Energy is DC.** The house is served at AC, so the pack must hold
      ``AC / eta_discharge`` to deliver it, and the floor is added because survival
      means reaching the refill *without crossing the floor*:
      ``4.32 + 4 * 0.4 / 0.948683 = 6.006548``.
    * **The price carries no efficiency at all**, and this is the correction that
      matters most. One DC kWh held to serve the house later avoids
      ``p_import * eta_d``; the same DC kWh exported now earns ``p_export * eta_d``.
      Same energy, same single conversion -- **it cancels**. An earlier draft divided
      by the round trip, which made the gate about 11 % too strict and would have
      refused genuinely good trades.
    """
    upper = (0.4, 0.4, 0.4, 0.4)
    prices = tuple(
        IntervalPrice(import_eur_kwh=p, export_eur_kwh=p - 0.13)
        for p in (0.20, 0.30, 0.40, 0.10)
    )

    energy, protect = survival_curves(
        upper,
        prices,
        window_end=4,
        floor_energy_kwh=FLOOR,
        discharge_efficiency=ETA,
    )

    assert energy[0] == pytest.approx(4.32 + 4 * 0.4 / 0.9486832980505138)
    assert energy[0] == pytest.approx(6.006548, abs=1e-6)
    # Demand-weighted, and with equal weights that is the arithmetic mean of the
    # four import prices -- the prices the source published, with no efficiency and
    # no guess.
    assert protect[0] == pytest.approx(0.25)
    # And the window shortens as it advances: from interval 1 the mean of
    # 0.30 / 0.40 / 0.10 is 0.266667.
    assert protect[1] == pytest.approx(0.8 / 3.0)
    assert energy[3] == pytest.approx(4.32 + 0.4 / 0.9486832980505138)


def test_the_round_trip_form_would_refuse_a_trade_the_correct_one_permits() -> None:
    """The fixture that separates the correct rule from the plausible wrong one.

    An import price of 0.30 protected, and an export price of 0.285. The round-trip
    rule would demand ``0.30 * 0.9 = 0.27``... no: it would *divide* the protected
    price by the round trip, demanding ``0.30 / 0.9 = 0.3333``, and refuse. The
    correct rule compares the two prices directly and permits, because 0.285
    genuinely beats 0.30 net of nothing -- the conversion is paid once either way.
    """
    upper = (1.0,)
    prices = (IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=0.285),)
    _energy, protect = survival_curves(
        upper,
        prices,
        window_end=1,
        floor_energy_kwh=FLOOR,
        discharge_efficiency=ETA,
    )

    assert protect[0] == pytest.approx(0.30)
    correct = protect[0] <= 0.285
    round_trip = protect[0] / (ETA * ETA) <= 0.285
    assert correct is False
    assert round_trip is False
    # And the discriminating case: at 0.31 the correct rule permits and the
    # round-trip rule still refuses, which is the 11 % of good trades it would cost.
    assert (protect[0] <= 0.31) is True
    assert (protect[0] / (ETA * ETA) <= 0.31) is False


def test_no_protection_exists_where_no_demand_is_spoken_for() -> None:
    """A zero denominator is no claim, not a zero price."""
    _energy, protect = survival_curves(
        (0.0, 0.0),
        (IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=0.17),) * 2,
        window_end=2,
        floor_energy_kwh=FLOOR,
        discharge_efficiency=ETA,
    )
    assert protect[0] is None


# ===========================================================================
# D. the anti-churn buffer -- monotone in distance, floored, capped
# ===========================================================================


def test_the_buffer_is_monotone_in_distance_floored_at_one_bucket() -> None:
    """Rev 1 had a decay constant, applied backwards. There is no constant now.

    The distance lives in the *quantity*: both sums run to the refill the plan
    expects to use, so a refill next quarter yields essentially the bucket floor and
    a refill four hours out yields four hours of the smaller of the two terms.
    Monotone by construction -- both sums are of non-negative terms, and the minimum
    of two non-decreasing sequences is non-decreasing.
    """
    bucket = 0.263523
    demands = tuple(
        IntervalDemand(index=i, baseline_kwh=0.5, pv_kwh=0.1) for i in range(64)
    )
    risk = ForecastRisk(mae_kwh=0.06, bias_kwh=-0.01)

    sizes = [
        anti_churn_buffer_kwh(
            demands,
            risk,
            window_end=window,
            bucket_kwh=bucket,
            discharge_efficiency=ETA,
        )
        for window in (0, 1, 4, 16, 48)
    ]

    assert sizes == sorted(sizes)
    # The floor: below one lattice bucket the state space cannot represent a
    # difference, so a purchase flip driven by less than a bucket is noise.
    assert sizes[0] == pytest.approx(bucket)
    # One quarter out: bucket + min(0.4 P50, 0.07 err) / eta = bucket + 0.07/eta.
    assert sizes[1] == pytest.approx(bucket + 0.07 / ETA)
    # Sixteen quarters: the *error* term still binds, not the load term --
    # 16 * 0.07 = 1.12 against 16 * 0.4 = 6.4.
    assert sizes[3] == pytest.approx(bucket + 16 * 0.07 / ETA)


def test_the_buffer_is_capped_by_the_demand_it_protects() -> None:
    """The bound is the P50 term, deliberately not the immediate bridge.

    Bounding it by the head deficit would defeat the point: the requirement is to
    cover the household until the *meaningful* refill, which is generally more than
    the bridge. What it may never exceed is the demand being protected.
    """
    bucket = 0.263523
    # Tiny demand, enormous measured error: the load term binds.
    demands = tuple(
        IntervalDemand(index=i, baseline_kwh=0.02, pv_kwh=0.0) for i in range(8)
    )
    size = anti_churn_buffer_kwh(
        demands,
        ForecastRisk(mae_kwh=1.0),
        window_end=8,
        bucket_kwh=bucket,
        discharge_efficiency=ETA,
    )
    assert size == pytest.approx(bucket + 8 * 0.02 / ETA)


# ===========================================================================
# E. the gate inside the solve -- and the four bounds that keep it a permission
# ===========================================================================


def test_no_evidence_leaves_the_permission_off_and_the_plan_untouched() -> None:
    """Every pre-beta.32 caller plans exactly as it did. No evidence, no gate."""
    plain = live_shape()
    assert plain.export_gate_cost_eur is None
    assert plain.export_floor_kwh == ()
    assert plain.export_free == ()
    assert plain.anti_churn_buffer_kwh == 0.0


def test_the_permission_never_moves_the_enforced_curve_beyond_the_head() -> None:
    """The one-line regression risk, pinned directly.

    Someone later writing ``max(floor + survival, ...)`` into the reserve curve is
    the way this becomes a second autonomy reserve. The physical curve is handed to
    ``build_horizon`` before the permission exists, and every interval past the head
    is equal in both -- only interval 0 can differ, and only by the anti-churn
    buffer while a bridge already exists.
    """
    gated = live_shape(forecast_risk=MEASURED)
    plain = live_shape()

    assert gated.horizon.planning_reserve_kwh == plain.horizon.planning_reserve_kwh
    assert gated.anti_churn_buffer_kwh == 0.0
    assert gated.enforced_reserve_head_kwh == gated.physical_reserve_head_kwh


def test_the_buffer_cannot_initiate_a_purchase() -> None:
    """No bridge, no bump -- over a sweep of shapes with real measured error.

    This is the property that separates the anti-churn extension from a reserve. It
    can enlarge a Safety Buy the physics already compelled; it can never create one.
    """
    for load in (0.1, 0.3, 0.6, 1.2):
        for stored in (10.0, 14.77, 20.0):
            solved = solve_shape(
                load_fn=flat(load),
                price_fn=_priced,
                n=48,
                stored=stored,
                forecast_risk=MEASURED,
            )
            bridge = max(0.0, (solved.physical_reserve_head_kwh or 0.0) - stored)
            if bridge <= 0.0:
                assert solved.anti_churn_buffer_kwh == 0.0, (load, stored)


def test_a_compelled_safety_buy_is_enlarged_and_the_head_says_so() -> None:
    """Starting at the floor: the bridge exists, so the extension applies.

    Measured on this shape -- floor 4.32, physical head 5.533986, stored 4.32, so a
    bridge of 1.21 kWh. The buffer is one lattice bucket, 0.263523, because the
    enforced head is quantised up to a bucket boundary and one bucket is what the
    quantisation of ``5.533986 + buffer`` lands on.
    """
    solved = solve_shape(
        load_fn=flat(0.5),
        price_fn=lambda index: 0.30,
        n=24,
        stored=FLOOR,
        forecast_risk=MEASURED,
    )

    assert solved.bridge_kwh_now is not None and solved.bridge_kwh_now > 0.0
    assert solved.anti_churn_buffer_kwh > 0.0
    assert solved.enforced_reserve_head_kwh == pytest.approx(
        (solved.physical_reserve_head_kwh or 0.0) + solved.anti_churn_buffer_kwh
    )
    # And it is on the head alone: the physical curve is untouched everywhere else.
    plain = solve_shape(
        load_fn=flat(0.5), price_fn=lambda index: 0.30, n=24, stored=FLOOR
    )
    assert solved.horizon.planning_reserve_kwh == plain.horizon.planning_reserve_kwh


def test_the_gate_cost_is_never_negative_on_any_shape() -> None:
    """The audit figure, over 48 shapes. A restriction cannot save money.

    ``export_gate_cost_eur`` is measured on ``EconomicPlan.objective_eur`` -- the
    scalar the recursion minimises -- and not on ``cost_eur``, which is the metered
    cash flow alone. Two earlier formulas here published a *negative* figure: one
    omitted the switching fee (the live horizon: cost fell 0.022 while the fee rose
    0.20), and one omitted the grid-charge margin (a 96-interval shape at
    1.2 kWh/quarter imported 4.6 kWh more at 0.05/kWh). An audit figure that says a
    protection paid for itself when it did not is worse than none.
    """
    for load in (0.1, 0.3, 0.6, 1.2):
        for stored in (5.0, 10.0, 14.77, 20.0):
            for count in (24, 48, 96):
                solved = solve_shape(
                    load_fn=flat(load),
                    price_fn=_priced,
                    n=count,
                    stored=stored,
                    forecast_risk=MEASURED,
                )
                cost = solved.export_gate_cost_eur
                assert cost is None or cost > -1e-9, (load, stored, count, cost)


def test_the_permission_costs_a_measurable_and_small_amount() -> None:
    """On the live 17:45 horizon, and the figure is stated rather than recomputed.

    The gate refused 2.297 of 11.271 kWh of export and cost 0.178 EUR on the
    objective the solver minimises. Compare what it bought: the raised floor rev 1
    of the design proposed cost 0.547 EUR on the invariant scenario, and stranded
    the pack at 35.4 % where the price-based permission leaves it at 25.6 %.
    """
    gated = live_shape(forecast_risk=MEASURED)
    plain = live_shape()

    plain_export = sum(entry.grid_export_kwh for entry in plain.desired.intervals)
    gated_export = sum(entry.grid_export_kwh for entry in gated.desired.intervals)

    assert plain_export == pytest.approx(11.271, abs=0.001)
    assert gated_export == pytest.approx(8.974, abs=0.001)
    assert gated.export_gate_cost_eur == pytest.approx(0.178, abs=0.001)
    # Small against what it protects, which is the whole argument for pricing the
    # export rather than raising the floor.
    assert 0.0 <= gated.export_gate_cost_eur < 0.30


def test_the_window_comes_from_the_plans_own_refill() -> None:
    """Not the first tolerable price -- the refill the optimiser chose.

    Every price-only rule fails the design's counter-example: with ``[0.30 now,
    0.24 tonight, 0.35 x n, 0.12 tomorrow]`` a relative test picks tonight's
    mediocre 0.24, because it genuinely beats everything seen so far, and the
    household would be far better off surviving to 0.12. The only definition that
    cannot drift from the plan is the plan's own choice, which is what
    ``plan_charge_campaign`` names.
    """
    gated = live_shape(forecast_risk=MEASURED)
    assert gated.survival_window_basis == "plan_charge_campaign"
    assert gated.survival_window_end == 64
    # And the window is inside the priced horizon by construction.
    assert 0 < gated.survival_window_end <= gated.horizon.intervals


def test_self_consumption_is_never_gated_so_the_floor_stays_reachable() -> None:
    """The bound that makes the two halves of the objective compatible.

    The permission refuses a *caused-export* delta and nothing else, so a pack can
    always reach 20 % feeding the house. That is not a claim about intent -- the zero
    delta is always available from every bucket, so no state becomes unreachable and
    the lexicographic order is untouched.
    """
    solved = solve_shape(
        load_fn=flat(0.6),
        price_fn=_priced,
        n=48,
        stored=14.77,
        allow_export=False,
        forecast_risk=MEASURED,
    )
    assert solved.desired.violation_kwh == 0.0
    assert solved.desired.available
    # No export at all, so the permission had nothing to refuse and cost nothing.
    assert sum(e.grid_export_kwh for e in solved.desired.intervals) == pytest.approx(
        0.0, abs=0.001
    )
