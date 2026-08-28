"""What beta.32 publishes, and the arithmetic a reader must be able to check.

Three of these figures were wrong in a design draft and were caught by working the
numbers rather than by reading the prose, which is why the numbers are in the
assertions:

* the exportable surplus **double-counted the hard floor**, understating it by
  exactly 4.32 kWh on the live installation;
* the campaign self-consumption figure was named ``_dc_kwh`` while computing an
  **AC** subtraction, and the draft it came from computed ``discharge_ac -
  export/eta``, which subtracts a DC quantity from an AC one;
* the export-gate audit was measured on ``cost_eur``, which is the metered cash
  flow alone, and reported a **negative** cost for a protection that had cost money.
"""

from __future__ import annotations

import itertools

import pytest

from custom_components.alpha_ems_manager.const import (
    COUNTERFACTUAL_AMBIENT_SELF_CONSUMPTION,
    COUNTERFACTUAL_IDLE_IMPORT,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
)
from custom_components.alpha_ems_manager.economic import ForecastRisk, economic_as_dict

from .beta32_harness import FLOOR, LIMITS, live_shape
from .test_beta32_export_gate import MEASURED

ETA = LIMITS.discharge_efficiency
#: The pack at the instant the live diagnostic was taken.
STORED_DC_KWH = 14.77


def payload(**overrides):
    """Return the published economic payload for the live 17:45 horizon."""
    solved = live_shape(**overrides)
    return (
        economic_as_dict(
            solved.outcome,
            execution_blocked_reason=ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
            reachability=solved.outcome.reachability,
            uncertainty=solved.outcome.uncertainty,
            floor_energy_kwh=FLOOR,
            stored_dc_kwh=STORED_DC_KWH,
            discharge_efficiency=ETA,
        ),
        solved,
    )


# ===========================================================================
# A. the surplus, with the floor counted exactly once
# ===========================================================================


def test_the_exportable_surplus_counts_the_floor_exactly_once() -> None:
    """14.77 stored, 4.32 floor, 5.13 reachable -> 9.64 DC and 9.15 AC.

    ``reserve.py`` computes ``required = floor_energy_kwh + deficit``, and
    production calls it **twice**: a probe at the bare floor to size the margin,
    then the real projection at ``floor + margin``. So ``reachability_now`` already
    contains the hard floor *and* the margin, and the surplus is one subtraction:
    ``stored - reachability_now``.

    The draft form ``stored - floor - reachability_now`` gave 5.32 kWh -- understated
    by exactly the floor. Both figures are asserted, so restoring the wrong one
    fails here rather than quietly halving what the plan thinks it may sell.
    """
    published, _solved = payload()
    planning = published["planning"]

    reachable = planning["reachability_now_dc_kwh"]
    assert reachable is not None
    surplus_dc = planning["exportable_surplus_dc_kwh"]
    surplus_ac = planning["exportable_surplus_ac_kwh"]

    assert surplus_dc == pytest.approx(STORED_DC_KWH - reachable, abs=0.001)
    # And the meter boundary is one conversion, never two.
    assert surplus_ac == pytest.approx(surplus_dc * ETA, abs=0.001)

    # The wrong form, stated so it cannot come back silently.
    double_counted = STORED_DC_KWH - FLOOR - reachable
    assert surplus_dc != pytest.approx(double_counted, abs=0.001)
    assert surplus_dc - double_counted == pytest.approx(FLOOR, abs=0.001)


def test_the_deliverable_figure_is_published_separately_and_named() -> None:
    """It ignores the margin, so it is *not* the surplus, and it says so.

    ``(14.77 - 4.32) * 0.948683 = 9.914`` -- which is the figure the live diagnostic
    published as "deliverable above floor", and a reader who subtracted it from the
    surplus would be subtracting the uncertainty margin twice. Both appear, both
    named, and the rule string states the relationship.
    """
    published, _solved = payload()
    planning = published["planning"]

    deliverable = planning["deliverable_above_floor_ac_kwh"]
    # Tolerance is the published precision, not the arithmetic's: the payload
    # rounds energies to BATTERY_KWH_PRECISION, and a test tighter than the figure
    # it reads would be asserting against a number nobody publishes.
    assert deliverable == pytest.approx((STORED_DC_KWH - FLOOR) * ETA, abs=0.01)
    assert deliverable == pytest.approx(9.914, abs=0.01)
    # Strictly larger than the surplus, by the margin -- which is what makes them
    # different quantities rather than two names for one number.
    assert deliverable > planning["exportable_surplus_ac_kwh"]
    assert "double counts" in planning["surplus_rule"]

    # And the margin is labelled as provenance, never as a further subtraction.
    assert planning["forecast_uncertainty_protection_kwh"] is not None
    assert "provenance" in planning["forecast_uncertainty_role"]


# ===========================================================================
# B. the campaign layer, dimensionally
# ===========================================================================


def test_the_campaign_count_equals_the_direction_changes() -> None:
    """The proof that grouping changed no decision, published as a figure.

    Campaigns are grouped on the DP's own contiguous run state, which reproduces
    exactly the transitions the switching fee was charged against -- so
    ``len(campaigns) == direction_changes`` by construction. On the live 17:45
    horizon that is **three**, against fifteen published label slices.
    """
    published, solved = payload()
    counts = published["campaign_counts"]

    assert counts["economic_campaign_count"] == len(solved.desired.campaigns)
    assert counts["economic_campaign_count"] == solved.desired.direction_changes
    assert counts["economic_campaign_count"] == 3
    assert len(solved.desired.runs) == 15

    # buy + sell does not sum to the total, and the payload says why rather than
    # leaving a reader to file a bug about it.
    total = counts["economic_campaign_count"]
    parts = (
        counts["buy_campaign_count"]
        + counts["sell_campaign_count"]
        + counts["serve_load_campaign_count"]
    )
    assert parts == total
    assert "does not sum" in counts["campaign_count_rule"]


def test_the_self_consumption_figures_are_labelled_by_boundary() -> None:
    """AC and DC, each named, and the ratio between them is the efficiency.

    ``battery_discharge_ac - grid_export`` is a difference of two **AC** figures, so
    it is AC and no efficiency belongs anywhere near it. A draft called this
    ``_dc_kwh`` while computing exactly that subtraction, and the plan it came from
    computed ``discharge_ac - export/eta`` -- subtracting a DC quantity from an AC
    one. Both were the boundary error this project forbids elsewhere.
    """
    published, _solved = payload()
    selling = [
        campaign
        for campaign in published["campaigns"]
        if campaign["direction"] == ECONOMIC_DIRECTION_DISCHARGE
    ]
    assert selling, "the live horizon sells twice; the fixture must reach one"

    for campaign in selling:
        ac = campaign["self_consumption_ac_kwh"]
        dc = campaign["self_consumption_dc_kwh"]
        assert ac == pytest.approx(
            campaign["battery_discharge_ac_kwh"] - campaign["grid_export_kwh"],
            abs=0.001,
        )
        if ac > 0.0:
            # One conversion, in the direction that makes the DC figure larger:
            # delivering AC to the house costs the pack ``AC / eta``.
            assert dc == pytest.approx(ac / ETA, abs=0.01)
            assert dc > ac


def test_each_campaign_names_the_boundary_its_objective_is_paid_at() -> None:
    """A charge is judged at the battery, a sale at the meter. Never chosen.

    Choosing is what put ``Tracking 0.25 kWh`` -- a battery ceiling -- beside
    ``Planned ... 0.11 kWh`` -- a meter objective -- on one run's lifecycle.
    """
    published, _solved = payload()
    for campaign in published["campaigns"]:
        if campaign["direction"] == ECONOMIC_DIRECTION_CHARGE:
            assert campaign["objective_boundary"] == "battery"
            assert campaign["objective_kwh"] == pytest.approx(
                campaign["battery_charge_ac_kwh"], abs=0.001
            )
        else:
            assert campaign["objective_boundary"] == "meter"
            # A sale's objective is the sum over its *export* segments, so it never
            # exceeds the meter energy the campaign moved.
            assert campaign["objective_kwh"] <= campaign["grid_export_kwh"] + 0.001


def test_every_campaign_publishes_its_segments_with_their_intents() -> None:
    """The layer Stage B is handed, and the one that emits nothing.

    ``net_export`` and ``grid_charge`` segments become execution targets;
    ``serve_load`` segments emit nothing at all -- no target, no command, no Activity
    line. Ordinary inverter behaviour, and beta.31 made the largest quantity in a
    live discharge campaign invisible by having no layer to put it in.
    """
    published, _solved = payload()
    for campaign in published["campaigns"]:
        assert campaign["segments"], campaign
        # Contiguous and exhaustive over the campaign's intervals.
        spans = [
            (segment["start_index"], segment["end_index"])
            for segment in campaign["segments"]
        ]
        assert spans[0][0] == campaign["start_index"]
        assert spans[-1][1] == campaign["end_index"]
        for left, right in itertools.pairwise(spans):
            assert right[0] == left[1] + 1
        for segment in campaign["segments"]:
            if segment["intent"] == "serve_load":
                assert segment["executable"] is False


# ===========================================================================
# C. the permission, the powers, and the evidence
# ===========================================================================


def test_the_permission_publishes_both_halves_and_its_own_cost() -> None:
    """A protection nobody can price is a protection nobody can challenge."""
    published, _solved = payload(forecast_risk=MEASURED)
    permission = published["planning"]["export_permission"]

    assert permission["active"] is True
    assert permission["survival_window_basis"] == "plan_charge_campaign"
    assert permission["survival_window_end"] == 64
    assert permission["export_gate_cost_eur"] == pytest.approx(0.178, abs=0.001)
    assert permission["export_gate_cost_eur"] >= 0.0
    # Both halves of the gate, per interval, so a flip is explainable.
    assert permission["export_floor_dc_kwh"]
    assert permission["protect_price_eur_per_kwh"]
    assert permission["export_free"]
    assert len(permission["export_free"]) == len(permission["export_floor_dc_kwh"])
    # And the efficiency argument, stated where a reader will check it.
    assert "cancels" in permission["rule"]


def test_only_physical_reachability_may_initiate_a_purchase() -> None:
    """Two booleans per quantity, because they are two different powers.

    "Can it force a purchase?" conflated them. Physical reachability can *initiate*
    a Safety Buy; the anti-churn extension cannot, but while it sits in the enforced
    head the solver must buy it -- and it becomes free household inventory the moment
    the buy lands. Calling that discretionary would misdescribe what the enforced
    head actually requires.
    """
    published, _solved = payload(forecast_risk=MEASURED)
    powers = published["planning"]["purchase_powers"]

    initiators = [
        name
        for name, value in powers.items()
        if isinstance(value, dict) and value.get("can_initiate_grid_purchase")
    ]
    assert sorted(initiators) == [
        "physical_reachability_now_dc_kwh",
        "safety_bridge_kwh",
    ]

    extension = powers["safety_anti_churn_buffer_kwh"]
    assert extension["can_initiate_grid_purchase"] is False
    assert extension["can_increase_triggered_grid_purchase"] is True
    assert extension["released_for_household_use_after_buy"] is True

    survival = powers["economic_survival_to_refill_kwh"]
    assert survival["can_initiate_grid_purchase"] is False
    assert survival["can_increase_triggered_grid_purchase"] is False

    assert powers["enforced_reserve_equals_physical_beyond_head"] is True


def test_the_measured_evidence_is_published_with_the_rung_it_used() -> None:
    """Every input, and which fallback the allowance actually took.

    A reader has to be able to tell a mature installation from a thin one without
    inferring it from the size of the number -- and the provenance-split MAEs are
    null on the refresh path *by design*, because they need a partition load and the
    refresh must not touch disk. The payload says so rather than leaving two nulls
    looking like a fault.
    """
    published, _solved = payload(forecast_risk=MEASURED)
    evidence = published["planning"]["forecast_evidence"]

    assert evidence["mae_kwh"] == pytest.approx(0.06)
    assert evidence["bias_kwh"] == pytest.approx(-0.02)
    assert evidence["error_persistence"] == pytest.approx(0.7)
    assert evidence["allowance_basis"] == "bias_and_persistent_mae"
    assert "must not touch disk" in evidence["provenance_split_rule"]
    assert "sparse history yields" in evidence["rule"]

    # Thin history takes a different rung, and says which.
    thin, _ = payload(forecast_risk=ForecastRisk(mae_kwh=0.06))
    assert thin["planning"]["forecast_evidence"]["allowance_basis"] == "mae_only"

    # No evidence at all is no block, not a block of nulls.
    plain, _ = payload()
    assert plain["planning"]["forecast_evidence"] is None
    assert plain["planning"]["export_permission"]["active"] is False
    assert plain["planning"]["export_permission"]["export_gate_cost_eur"] is None


def test_the_counterfactual_basis_is_named_on_every_payload() -> None:
    """Which idle counterfactual the euros were measured against.

    Without it, two installations publishing different marginal figures for the
    same shape are indistinguishable from one of them being wrong.
    """
    unmodelled, _ = payload()
    assert (
        unmodelled["planning"]["counterfactual"]["basis"] == COUNTERFACTUAL_IDLE_IMPORT
    )
    assert (
        unmodelled["planning"]["counterfactual"]["ambient_self_consumption_modelled"]
        is False
    )

    modelled, _ = payload(ambient_self_consumption=True)
    assert (
        modelled["planning"]["counterfactual"]["basis"]
        == COUNTERFACTUAL_AMBIENT_SELF_CONSUMPTION
    )
    # And the deferral is recorded rather than left implicit.
    assert "not representable" in modelled["planning"]["counterfactual"]["deferred"]
