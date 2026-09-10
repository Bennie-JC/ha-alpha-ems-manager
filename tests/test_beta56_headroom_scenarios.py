"""beta.56: the headroom economics were right, and the payload said otherwise.

The reference installation charged to 100 % on 2026-09-10 while every published
target read ``required_headroom_kwh: null``, ``max_end_energy_kwh: null``,
``headroom_until: null``, ``headroom_constrained: false``. Read as a group that
looks like an optimiser that never protects room for forecast production.

It is not. Two separate things were true at once:

* **The real rule was live and binding.** ``edge_creditable_energy_kwh`` caps how
  much terminal inventory may be *valued* at ``ceiling - forecast_surplus``, in
  the objective's cost term, and on the audited refresh that was
  ``21.6 - 3.2 = 18.4`` -- exactly the ``edge_creditable_kwh`` in the dump.
  Alongside it, every absorbed kilowatt-hour is charged its own foregone export
  revenue per interval, and a full pack simply loses the absorption move.
* **The published triple is a different mechanism entirely.** ``headroom_of``
  computes ``ceiling - landed[run.end_index]``, where ``landed`` is *the plan's
  own solved end energy*. No price, no forecast, no comparison. It can only
  restate a decision already taken -- a Stage B faithfulness cap, which is a real
  and useful job -- and it goes null whenever the plan absorbs nothing further,
  which is exactly the case a reader most wants explained.

So the published ``headroom_rule`` claimed to be the economics and described a
function in another file. beta.56 changes the words and the missing reason, and
changes **no** planner decision. This suite pins both halves: that the economics
are already sufficient, and that the publication now says what it actually is.

The one real defect fixed here is a bound: ``retain_until_dc_kwh`` walked the
lattice in whole buckets and published 21.61 kWh for a 21.6 kWh pack.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_GRID_CHARGE,
    HEADROOM_REASON_CONSTRAINED,
    HEADROOM_REASON_NO_CEILING,
    HEADROOM_REASON_NO_LANDING_ENERGY,
    HEADROOM_REASON_NO_LATER_ABSORPTION,
    HEADROOM_REASONS,
    HEADROOM_RULE,
)
from custom_components.alpha_ems_manager.economic import (
    RetentionGate,
    edge_creditable_energy_kwh,
)

from .beta34_shape import LIMITS, load_29aug, price_29aug
from .solve_cache import cached_solve_at
from .test_beta33_campaign_wiring import multi_segment_targets

pytestmark = pytest.mark.usefixtures("control_surface")


# ---------------------------------------------------------------------------
# the live figures, from the 2026-09-10 diagnostics
# ---------------------------------------------------------------------------

LIVE_CEILING_KWH = 21.6
LIVE_FORECAST_SURPLUS_KWH = 3.2
LIVE_EDGE_CREDITABLE_KWH = 18.4


# ===========================================================================
# 1. the rule that actually does the work
# ===========================================================================


def test_the_live_headroom_rule_reproduces_the_dump_exactly() -> None:
    """**21.6 - 3.2 = 18.4, and the dump published 18.4.**

    This is the assertion the whole Issue 3 audit turned on. The published triple
    was null on every target of the day, and it was tempting to read that as "no
    headroom was protected". The protection was in the objective, arithmetically
    checkable, and binding.
    """
    assert edge_creditable_energy_kwh(
        ceiling_kwh=LIVE_CEILING_KWH,
        forecast_surplus_kwh=LIVE_FORECAST_SURPLUS_KWH,
    ) == pytest.approx(LIVE_EDGE_CREDITABLE_KWH)


def test_more_forecast_surplus_protects_more_room() -> None:
    """Scenario E, as arithmetic rather than as a solve.

    A forecast that rises must reduce the inventory the edge will pay for, kWh for
    kWh -- that is the whole mechanism by which a pack is discouraged from filling
    early against incoming sun. Anything sub-linear would leave part of the
    surplus unpriced.
    """
    base = edge_creditable_energy_kwh(ceiling_kwh=21.6, forecast_surplus_kwh=3.2)
    richer = edge_creditable_energy_kwh(ceiling_kwh=21.6, forecast_surplus_kwh=6.4)

    assert base - richer == pytest.approx(3.2)


def test_no_forecast_surplus_protects_nothing() -> None:
    """Scenario G: strong sun with nothing later to want it is not a constraint.

    A fake headroom requirement here would forbid the pack from filling on the one
    kind of day where filling is free and harmless. The cap is the whole ceiling,
    which is the same as no cap.
    """
    assert edge_creditable_energy_kwh(
        ceiling_kwh=21.6, forecast_surplus_kwh=0.0
    ) == pytest.approx(21.6)
    # And a surplus larger than the pack protects the pack, never a negative.
    assert edge_creditable_energy_kwh(
        ceiling_kwh=21.6, forecast_surplus_kwh=40.0
    ) == pytest.approx(0.0)


# ===========================================================================
# 2. the optimiser's own behaviour, on real solves
# ===========================================================================
#
# Named module-level curves: ``solve_cache`` refuses an anonymous callable,
# because two different lambdas share the qualname ``<lambda>`` and would serve
# one test's plan to another.


def sunny_afternoon_pv(index: int) -> float:
    """Return a strong afternoon production shape, peaking before the evening."""
    hour = (index % 96) / 4.0
    if 9.0 <= hour < 17.0:
        return 1.6
    return 0.0


def no_pv(index: int) -> float:
    """Return no production at all."""
    return 0.0


def evening_peak_price(index: int) -> float:
    """Return a cheap day and an expensive evening, so stored energy has a use."""
    hour = (index % 96) / 4.0
    if 18.0 <= hour < 22.0:
        return 0.62
    return 0.12


def evening_peak_export(index: int) -> float:
    """Return an export tariff that makes the evening genuinely worth selling into.

    ``solve_at``'s default export curve is the reference installation's own
    relation to the import price, which can never make in-horizon arbitrage pay --
    ``0.8265 p - 0.0885 < 0.9 p`` for every positive ``p``. A scenario that needs
    the evening to be worth holding for has to state its own tariff.
    """
    hour = (index % 96) / 4.0
    if 18.0 <= hour < 22.0:
        return 0.58
    return 0.04


def test_forecast_production_is_absorbed_rather_than_spilled() -> None:
    """Scenario A. **And the assertion is on the trajectory, not on the triple.**

    A pack at 70 % with strong afternoon sun and a valuable evening should take the
    surplus in rather than sell it at 0.04. Asserting on ``required_headroom_kwh``
    here would test the inert publication and pass or fail for reasons unrelated
    to the economics -- so this reads the plan's own absorbing intervals, which is
    where the decision lives.
    """
    solved = cached_solve_at(
        head=36,
        end=96,
        stored=LIMITS.energy_for_soc(70.0),
        price_fn=evening_peak_price,
        pv_fn=sunny_afternoon_pv,
        load_fn=load_29aug,
        export_fn=evening_peak_export,
    )

    absorbing = [
        entry
        for entry in solved.desired.intervals
        if entry.absorbing and entry.battery_delta_dc_kwh > 0.0
    ]
    assert absorbing, "a pack with room and free sun above the load absorbed none"
    # And it is genuinely free production being taken, not bought energy.
    assert all(entry.grid_import_kwh <= 0.001 for entry in absorbing)


def test_a_full_pack_cannot_absorb_and_the_plan_shows_it() -> None:
    """Scenario F: no room, so the surplus is forced out at the interval's price.

    This is why the optimiser does not need a separate headroom constraint: losing
    the move *is* the penalty, and the backward pass sees it from the afternoon.
    The comparison is against the same shape with room to spare.
    """
    full = cached_solve_at(
        head=36,
        end=96,
        stored=LIMITS.energy_for_soc(100.0),
        price_fn=evening_peak_price,
        pv_fn=sunny_afternoon_pv,
        load_fn=load_29aug,
        export_fn=evening_peak_export,
    )
    roomy = cached_solve_at(
        head=36,
        end=96,
        stored=LIMITS.energy_for_soc(70.0),
        price_fn=evening_peak_price,
        pv_fn=sunny_afternoon_pv,
        load_fn=load_29aug,
        export_fn=evening_peak_export,
    )

    absorbed_full = sum(
        entry.battery_delta_dc_kwh
        for entry in full.desired.intervals
        if entry.absorbing and entry.battery_delta_dc_kwh > 0.0
    )
    absorbed_roomy = sum(
        entry.battery_delta_dc_kwh
        for entry in roomy.desired.intervals
        if entry.absorbing and entry.battery_delta_dc_kwh > 0.0
    )
    assert absorbed_roomy > absorbed_full


def test_the_reserve_still_outranks_every_headroom_consideration() -> None:
    """Scenario C, and it is the invariant no headroom term may ever cross.

    The objective is lexicographic ``(violation, cost)``: reserve feasibility
    dominates money absolutely. A pack starting near the floor must still be
    brought to safety even on a day whose economics would rather wait for the sun,
    and nothing in beta.56 may put a headroom term into the violation element.
    """
    solved = cached_solve_at(
        head=36,
        end=96,
        stored=LIMITS.energy_for_soc(22.0),
        price_fn=evening_peak_price,
        pv_fn=sunny_afternoon_pv,
        load_fn=load_29aug,
        export_fn=evening_peak_export,
    )

    # The plan is feasible, which is the only acceptable answer at this level.
    assert solved.desired.violation_kwh == pytest.approx(0.0)


def test_a_grid_purchase_does_not_displace_the_production_it_means_to_absorb() -> None:
    """Scenario H: cheap grid and forecast sun inside one charge window.

    The published balance separates what the plan expects from the array from what
    it authorises from the meter, and both must survive together -- a charge that
    bought its way to full before the sun arrived would show the production share
    collapsing to nothing.
    """
    solved = cached_solve_at(
        head=36,
        end=96,
        stored=LIMITS.energy_for_soc(45.0),
        price_fn=evening_peak_price,
        pv_fn=sunny_afternoon_pv,
        load_fn=load_29aug,
        export_fn=evening_peak_export,
    )

    absorbed = sum(
        entry.battery_delta_dc_kwh
        for entry in solved.desired.intervals
        if entry.absorbing and entry.battery_delta_dc_kwh > 0.0
    )
    assert absorbed > 0.0, "the plan bought its way past the production it forecast"


def test_a_day_with_no_sun_needs_no_room_kept() -> None:
    """Scenario B/D's shared premise: nothing forecast, nothing to protect.

    The same shape with the array switched off must not start reserving room, which
    is what a naive "always keep X kWh" rule would do -- and is exactly what this
    release was asked not to implement.
    """
    solved = cached_solve_at(
        head=36,
        end=96,
        stored=LIMITS.energy_for_soc(70.0),
        price_fn=evening_peak_price,
        pv_fn=no_pv,
        load_fn=load_29aug,
        export_fn=evening_peak_export,
    )

    assert not [
        entry
        for entry in solved.desired.intervals
        if entry.absorbing and entry.battery_delta_dc_kwh > 0.0
    ]


# ===========================================================================
# 3. the publication, which is the half beta.56 changes
# ===========================================================================


async def test_every_published_target_names_why_its_headroom_reads_as_it_does(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """**Never null, on any target, including where the triple is populated.**

    ``required_headroom_kwh``, ``max_end_energy_kwh`` and ``headroom_until`` flip
    together, so a null triple was one silence covering three different facts. A
    reason field that were itself null on the interesting branch would just repeat
    the defect, so the constrained case is named too.
    """
    _coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )
    assert targets

    for target in targets:
        reason = target["headroom_reason"]
        assert reason in HEADROOM_REASONS, target["intent"]
        populated = target["required_headroom_kwh"] is not None
        # The reason and the figures must agree, or the field is decoration.
        if populated:
            assert reason == HEADROOM_REASON_CONSTRAINED
            assert target["max_end_energy_kwh"] is not None
            assert target["headroom_until"] is not None
        else:
            assert reason != HEADROOM_REASON_CONSTRAINED
            assert target["max_end_energy_kwh"] is None
            assert target["headroom_until"] is None


async def test_the_reference_shape_names_the_absence_the_audit_had_to_derive(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The live case: the plan absorbs nothing after the run, so nothing is capped.

    Reading that off ``null`` took a source audit. It is now one string, and it is
    the difference between "the optimiser declined to protect anything" -- which
    would be alarming and was the natural reading -- and "there is nothing left to
    protect", which is a fact about the forecast.
    """
    _coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )

    reasons = {target["headroom_reason"] for target in targets}
    assert reasons <= set(HEADROOM_REASONS)
    assert HEADROOM_REASON_NO_LATER_ABSORPTION in reasons


def test_the_three_null_causes_are_distinct_tokens() -> None:
    """Three facts, three words, and none of them a bare ``null``.

    Collapsing them is what made the audit necessary: "no ceiling is known" is a
    configuration or reserve problem, "the plan has no landing energy for that
    index" is a plan-shape problem, and "the plan absorbs nothing later" is
    ordinary and expected.
    """
    assert len({*HEADROOM_REASONS}) == 4
    for token in (
        HEADROOM_REASON_CONSTRAINED,
        HEADROOM_REASON_NO_CEILING,
        HEADROOM_REASON_NO_LANDING_ENERGY,
        HEADROOM_REASON_NO_LATER_ABSORPTION,
    ):
        assert token in HEADROOM_REASONS


def test_the_published_rule_no_longer_claims_to_be_the_economics() -> None:
    """**The false sentence, and the true one that replaces it.**

    beta.55 published "decided here because how much headroom is worth keeping is
    an economic question" beside three fields containing no price, no forecast and
    no comparison. A reader who believed it would conclude a null meant the
    economics had declined to protect anything -- when the economics live in a
    function the payload never named.

    *Mutation: restore the old string and this fails on every assertion below.*
    """
    assert "faithfulness cap, not an economic calculation" in HEADROOM_RULE
    # It names where the economics actually are.
    assert "edge_creditable_energy_kwh" in HEADROOM_RULE
    assert "foregone export" in HEADROOM_RULE
    # And it keeps the two things the old string got right.
    assert "null means unconstrained, NOT zero" in HEADROOM_RULE
    assert "headroom_reason" in HEADROOM_RULE
    # The retired claim must be gone, not merely qualified.
    assert "because how much headroom is worth keeping is an economic" not in (
        HEADROOM_RULE
    )


async def test_stage_b_still_receives_the_cap_it_has_always_consumed(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """**Re-labelled, not removed.** ``max_end_energy_kwh`` has a real consumer.

    ``headroom_ceiling_kw`` reads it to stop Stage B overshooting the plan's own
    landing energy, and that is a legitimate faithfulness guard worth keeping.
    beta.56 corrects the prose and adds the reason; deleting the field would break
    the one part of the mechanism that does something.
    """
    _coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )

    for target in targets:
        assert "max_end_energy_kwh" in target
        assert "required_headroom_kwh" in target
        assert "headroom_until" in target
        assert "headroom_rule" in target


# ===========================================================================
# 4. the one real bug: a published bound the pack cannot reach
# ===========================================================================


def _gate(curve, *, bucket: float, current: int = 0) -> RetentionGate:
    return RetentionGate(
        marginal_value_eur_kwh=curve[current],
        round_trip_efficiency=1.0,
        marginal_curve_eur_kwh=curve,
        current_bucket=current,
        bucket_dc_kwh=bucket,
    )


def test_a_curve_that_pays_all_the_way_up_lands_above_the_pack() -> None:
    """**The defect, reproduced.** 21.61 kWh published for a 21.6 kWh battery.

    The lattice is sized ``ceil(ceiling / bucket)``, so its top level sits up to
    one bucket above the pack, and the walk returns the level at the *top* of the
    last paying step. The gate is forbidden a physical limit -- every physical
    bound in this integration comes out of one clamp, and
    ``test_the_gate_holds_no_physical_limit`` pins its field list against exactly
    the temptation to add one -- so the gate keeps returning the lattice level and
    the caller brings it back inside the hardware.
    """
    # Every step pays, so the walk runs to the top of the curve.
    gate = _gate((0.9, 0.8, 0.7, 0.6), bucket=5.4)
    assert gate.retain_until_dc_kwh(0.10) == pytest.approx(21.6)

    # A pitch that does not divide the pack overshoots it, which is the live case.
    coarse = _gate((0.9, 0.8, 0.7, 0.6), bucket=5.4025)
    assert coarse.retain_until_dc_kwh(0.10) > 21.6


def _published_rows(*, ceiling: float | None):
    """Return the real published rows for a curve that pays to the top."""
    from datetime import timedelta

    from custom_components.alpha_ems_manager.economic import quarter_schedule_for

    from .test_beta40_safety_buy_unchanged import BASE, Interval

    # A pitch that does not divide the pack, which is the live condition: the
    # lattice is sized by ``ceil`` so its top level overshoots the battery.
    gate = RetentionGate(
        marginal_value_eur_kwh=0.9,
        round_trip_efficiency=1.0,
        marginal_curve_eur_kwh=(0.9, 0.8, 0.7, 0.6),
        current_bucket=0,
        bucket_dc_kwh=5.4025,
    )
    return quarter_schedule_for(
        (Interval(0, export_price=0.10),),
        start_index=0,
        end_index=0,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        moment=lambda index: BASE + timedelta(minutes=15 * index),
        retention=gate,
        ceiling_dc_kwh=ceiling,
    )


def test_the_published_bound_is_clamped_where_the_ceiling_is_known() -> None:
    """**The fix, through the real row builder rather than by inspection.**

    ``quarter_schedule_for`` publishes the row and receives the pack ceiling from
    the coordinator, so the clamp happens once, in the only place that both knows
    the limit and is permitted to hold it.

    *Mutation: drop the ``min(retention_until, ceiling_dc_kwh)`` and the
    unclamped level is published again -- which is what the second half asserts.*
    """
    clamped = _published_rows(ceiling=21.6)[0]
    assert clamped["retention_authorised"] is True
    assert clamped["retention_until_dc_kwh"] == pytest.approx(21.6)

    # Without a ceiling the row is byte-for-byte what beta.55 published, rather
    # than a limit invented from nothing -- and it is outside the pack, which is
    # the defect being fixed.
    unclamped = _published_rows(ceiling=None)[0]
    assert unclamped["retention_until_dc_kwh"] > 21.6


def test_the_clamp_never_raises_a_bound_the_economics_set_lower() -> None:
    """It may only reduce. A ceiling is not an authorisation.

    A curve that stops paying partway up must keep its own lower bound: the whole
    point of ``retain_until_dc_kwh`` is that keeping stops paying *before* the pack
    is full, and a clamp that raised it to the ceiling would hand back exactly the
    authority the beta.40 corrective removed.
    """
    from datetime import timedelta

    from custom_components.alpha_ems_manager.economic import quarter_schedule_for

    from .test_beta40_safety_buy_unchanged import BASE, Interval

    # Only the first step clears an export price of 0.10; the rest do not.
    gate = RetentionGate(
        marginal_value_eur_kwh=0.9,
        round_trip_efficiency=1.0,
        marginal_curve_eur_kwh=(0.9, 0.5, 0.05, 0.04),
        current_bucket=0,
        bucket_dc_kwh=5.4,
    )
    row = quarter_schedule_for(
        (Interval(0, export_price=0.10),),
        start_index=0,
        end_index=0,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        moment=lambda index: BASE + timedelta(minutes=15 * index),
        retention=gate,
        ceiling_dc_kwh=21.6,
    )[0]

    assert row["retention_until_dc_kwh"] == pytest.approx(10.8)
    assert row["retention_until_dc_kwh"] < 21.6


def test_the_gate_itself_still_holds_nothing_physical() -> None:
    """**The boundary the fix had to respect, restated where it can be read.**

    A first attempt at this clamp put ``ceiling_dc_kwh`` on ``RetentionGate``. The
    beta.40 neutrality suite rejected it by name, and it was right to: a second
    copy of a physical bound is a second thing to keep in step, and the first time
    the two disagreed it would be the copy that got believed.
    """
    fields = set(RetentionGate.__dataclass_fields__)
    assert "ceiling_dc_kwh" not in fields
    assert "usable_energy_kwh" not in fields


def test_the_gate_verdict_is_untouched_by_any_of_this() -> None:
    """Only the published *level* moved. The economic answer did not.

    The verdict compares the dual at the pack's current level against the export
    price, and beta.56 changes nothing about it -- which matters, because the live
    100 % charge was economically correct and a changed verdict would have made it
    wrong retrospectively.
    """
    gate = _gate((0.9, 0.8, 0.7, 0.6), bucket=5.4)

    keeps, _reason = gate.verdict(0.10)
    assert keeps is True
    sells, _reason = gate.verdict(0.95)
    assert sells is False
    absent, reason = gate.verdict(None)
    assert absent is False
    assert reason is not None


# ===========================================================================
# 5. and the planner is untouched
# ===========================================================================


def test_no_headroom_term_entered_the_violation_element() -> None:
    """**The invariant that keeps money out of the safety class.**

    A headroom rule in the violation element would outrank every price in the
    horizon and could make states unreachable -- it would be a safety constraint
    wearing an economic argument. ``violations`` is built from the reserve alone,
    and a feasible plan must report exactly zero however much room it leaves.
    """
    solved = cached_solve_at(
        head=36,
        end=96,
        stored=LIMITS.energy_for_soc(100.0),
        price_fn=price_29aug,
        pv_fn=sunny_afternoon_pv,
        load_fn=load_29aug,
    )

    assert solved.desired.violation_kwh == pytest.approx(0.0)


async def test_a_charge_target_still_publishes_its_whole_balance(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The beta.54/beta.55 charge contract is not disturbed by the reason field.

    An additive string beside a triple should not be able to move a balance, and
    this is the cheap guard that says so rather than assuming it.
    """
    _coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )

    charges = [
        target for target in targets if target["intent"] == EXECUTION_INTENT_GRID_CHARGE
    ]
    for target in charges:
        for field in (
            "expected_pv_to_battery_kwh",
            "expected_grid_to_battery_kwh",
            "charge_source",
        ):
            assert field in target
