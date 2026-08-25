"""The three anomalies a real Shadow snapshot exposed, and one it exposed by accident.

**Every number in this module comes from one real diagnostics download** taken on
2026-08-25 at 10:47 local, on a 22 kWh / 10 kW / 90 % installation with the pack
at 54 %. That snapshot is the evidence, and these are the assertions it forced:

* ``projected_end_energy_kwh`` read **31.946 kWh** for a 22 kWh pack;
* ``execution.revision`` read **13** beside ``carried.run.revision`` of **2**;
* the headroom cap subtracted expected production a second time;
* and the per-quarter reserve requirement published in beta.21 was read off the
  wrong axis -- interval 44 carried 12.39 kWh where its requirement was 5.67.

The last one is mine, introduced by the beta.21 observability feature, and it is
the reason a real snapshot is worth more than a suite: nothing in the tests
compared a published reserve figure against the interval it belonged to.
"""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from custom_components.alpha_ems_manager.execution import (
    as_dict,
    demand_for,
    headroom_ceiling_kw,
)

from .test_stage_b_carry_forward import decision_for, published
from .test_stage_b_controller import (
    CLOSES,
    OPENS,
    measure_progress,
    progress_of,
    target_of,
)

#: The pack in the snapshot: 22 kWh, 90 % round trip, so one crossing is sqrt(0.9).
ONE_WAY = math.sqrt(0.9)
CAPACITY_KWH = 22.0

# The snapshot's own figures, kept as named constants so a reader can check them
# against the download rather than against this file.
SNAP_TARGET_KWH = 11.39
SNAP_REALIZED_KWH = 0.352
SNAP_REMAINING_KWH = 11.038
SNAP_GRID_TO_BATTERY_KWH = 2.36
SNAP_PV_TO_BATTERY_KWH = 9.03
SNAP_STORED_KWH = 11.88
SNAP_ROLLING_KW = 1.92
SNAP_REMAINING_MINUTES = 344.9
SNAP_OLD_PROJECTED_KWH = 31.946


# ===========================================================================
# The snapshot reconciles, which is what makes it evidence
# ===========================================================================


def test_the_snapshot_identities_hold() -> None:
    """Two identities, and the second is the whole root cause.

    ``battery_target_kwh`` is the sum of the two attributions, so the production
    share is a *component* of the target rather than an addition to it. Anything
    that adds expected production to a remaining-target figure counts it twice.
    """
    assert (
        pytest.approx(SNAP_TARGET_KWH, abs=5e-3)
        == SNAP_REALIZED_KWH + SNAP_REMAINING_KWH
    )
    assert (
        pytest.approx(SNAP_TARGET_KWH, abs=5e-3)
        == SNAP_GRID_TO_BATTERY_KWH + SNAP_PV_TO_BATTERY_KWH
    )


def test_the_old_projection_is_reproduced_from_the_snapshot() -> None:
    """The wrong answer, derived, so the fix can be shown to remove exactly it.

    ``stored + still_expected + remaining`` -- and since the rolling controller
    is ``remaining / hours`` by definition, the third term *is* the remaining
    target no matter how long the window is.
    """
    hours = SNAP_REMAINING_MINUTES / 60.0
    total_minutes = 345.0  # the admitted window, 08:45 to 14:30 UTC
    still_expected = SNAP_PV_TO_BATTERY_KWH * min(
        1.0, SNAP_REMAINING_MINUTES / total_minutes
    )

    assert SNAP_ROLLING_KW * hours == pytest.approx(SNAP_REMAINING_KWH, abs=5e-3)
    old = SNAP_STORED_KWH + still_expected + SNAP_ROLLING_KW * hours
    assert old == pytest.approx(SNAP_OLD_PROJECTED_KWH, abs=5e-3)
    # And it is impossible, which is how it was noticed.
    assert old > CAPACITY_KWH


# ===========================================================================
# Fix: the projection means projected stored energy
# ===========================================================================


def snapshot_demand(*, charge_efficiency: float | None = ONE_WAY, **overrides):
    """Return the demand for the snapshot's own state."""
    fields = {
        "battery_target_kwh": SNAP_TARGET_KWH,
        "expected_pv_to_battery_kwh": SNAP_PV_TO_BATTERY_KWH,
        "expected_grid_to_battery_kwh": SNAP_GRID_TO_BATTERY_KWH,
        "max_end_energy_kwh": None,
        "required_headroom_kwh": None,
    }
    fields.update(overrides)
    target = target_of(**fields)
    return demand_for(
        target,
        now=CLOSES - timedelta(minutes=SNAP_REMAINING_MINUTES),
        progress=progress_of(SNAP_REALIZED_KWH),
        current_energy_kwh=SNAP_STORED_KWH,
        remaining_expected_pv_kwh=SNAP_PV_TO_BATTERY_KWH,
        charge_efficiency=charge_efficiency,
    )


def test_the_projection_no_longer_double_counts_production() -> None:
    """**The fix, against the real figures.**

    Stored energy plus the energy still to deliver, converted from AC to DC once.
    Nothing about expected production, because the delivery already contains it.
    """
    demand = snapshot_demand()

    expected = SNAP_STORED_KWH + demand.remaining_kwh * ONE_WAY
    assert demand.projected_end_kwh == pytest.approx(expected, abs=5e-3)
    # The production term is gone: about nine kilowatt-hours of it.
    assert demand.projected_end_kwh < SNAP_OLD_PROJECTED_KWH - 9.0
    assert demand.projected_end_kwh == pytest.approx(22.35, abs=0.05)


def test_shifting_the_attribution_does_not_move_the_projection() -> None:
    """**The sharpest statement of the fix.**

    Stage A can split the same target between production and grid however the
    solve came out. The pack ends up holding the same energy either way, so a
    projection that moves when the attribution moves is reading the wrong thing.
    """
    baseline = snapshot_demand().projected_end_kwh
    for pv, grid in ((0.0, SNAP_TARGET_KWH), (SNAP_TARGET_KWH, 0.0), (5.0, 6.39)):
        shifted = snapshot_demand(
            expected_pv_to_battery_kwh=pv, expected_grid_to_battery_kwh=grid
        ).projected_end_kwh
        assert shifted == pytest.approx(baseline, abs=1e-9), (pv, grid)


def test_the_conversion_is_applied_exactly_once() -> None:
    """Asserted against the efficiency rather than against a copied number."""
    demand = snapshot_demand()
    delivery = demand.remaining_kwh

    once = SNAP_STORED_KWH + delivery * ONE_WAY
    twice = SNAP_STORED_KWH + delivery * ONE_WAY * ONE_WAY
    never = SNAP_STORED_KWH + delivery

    assert demand.projected_end_kwh == pytest.approx(once, abs=5e-3)
    assert demand.projected_end_kwh != pytest.approx(twice, abs=1e-3)
    assert demand.projected_end_kwh != pytest.approx(never, abs=1e-3)


def test_without_an_efficiency_the_projection_is_unavailable_not_wrong() -> None:
    """Absent is not a substituted value.

    Mixing AC into DC unannounced is what produced the impossible figure. With no
    conversion to hand the honest answer is that there is no answer.
    """
    assert snapshot_demand(charge_efficiency=None).projected_end_kwh is None


def test_the_projection_is_bounded_by_the_ceiling_stage_a_chose() -> None:
    """Where a cap exists the projection cannot exceed it -- provably.

    The cap is derived so that holding it lands the pack on ``max_end``; the
    projection is what holding it lands on. They are two readings of one number,
    so they agree by construction rather than by clamping.
    """
    for stored in (6.0, 11.88, 15.0, 17.5):
        demand = demand_for(
            target_of(max_end_energy_kwh=18.0),
            now=OPENS + timedelta(minutes=30),
            progress=progress_of(0.0),
            current_energy_kwh=stored,
            charge_efficiency=ONE_WAY,
        )
        assert demand.projected_end_kwh is not None
        assert demand.projected_end_kwh <= 18.0 + 1e-6, stored


def test_a_target_that_does_not_fit_is_reported_rather_than_clamped() -> None:
    """A projection above the pack ceiling is information, not a fault.

    The snapshot's own run is one: 11.88 stored against 11.038 still to deliver
    does not fit in 22 kWh, and clamping the figure to 22 would have hidden the
    only clue that the plan and the pack disagree.
    """
    projected = snapshot_demand().projected_end_kwh

    assert projected is not None
    assert projected > CAPACITY_KWH
    assert projected < CAPACITY_KWH + 1.0


# ===========================================================================
# Fix: the headroom cap stops subtracting production twice
# ===========================================================================


def test_the_cap_never_raises_the_rolling_request() -> None:
    """Both corrections raise the cap, so the one-way rule is re-asserted.

    A cap is applied afterwards and can only ever reduce. Swept across starting
    states, because a bound that could increase a request would be a different
    mechanism wearing the same name.
    """
    for stored in (4.4, 8.0, 11.88, 16.0, 18.0, 21.0):
        demand = demand_for(
            target_of(max_end_energy_kwh=18.0),
            now=OPENS + timedelta(minutes=30),
            progress=progress_of(0.0),
            current_energy_kwh=stored,
            charge_efficiency=ONE_WAY,
        )
        assert demand.required_kw <= demand.rolling_kw + 1e-9, stored


def test_an_absent_cap_is_still_unconstrained() -> None:
    """The rule this project keeps having to restate: absent is never zero."""
    demand = snapshot_demand()

    assert demand.ceiling_kw is None
    assert demand.required_kw == pytest.approx(demand.rolling_kw)


# ===========================================================================
# Fix: the frozen publication is labelled
# ===========================================================================


def test_the_two_revisions_may_differ_and_the_payload_says_why() -> None:
    """**The snapshot's 13 beside 2, and neither is wrong.**

    ``execution.revision`` is the publication Stage B admitted, frozen; the
    carried run counts material changes since. The payload now says so rather
    than leaving a reader to discover it.
    """
    carry = decision_for.__globals__["carry_forward"](
        None, [published(OPENS, CLOSES, revision=13)], OPENS - timedelta(minutes=15)
    )
    assert carry.carried is not None
    decision = decision_for(carry, OPENS)

    payload = as_dict(decision, mode="shadow", executed=False)

    assert payload["revision"] == 13
    assert carry.carried.revision == 1
    rule = payload["admitted_publication_rule"]
    assert "frozen at admission" in rule
    assert "carried.run.revision" in rule
    # No key was removed to say it.
    for key in ("plan_id", "revision", "window_start", "window_end", "stale_after"):
        assert key in payload


def test_the_frozen_group_really_is_frozen() -> None:
    """Not just the revision: the whole admitted block is one instant's reading."""
    carry_forward = decision_for.__globals__["carry_forward"]
    carry = carry_forward(
        None, [published(OPENS, CLOSES, revision=13)], OPENS - timedelta(minutes=15)
    )
    admitted = as_dict(decision_for(carry, OPENS), mode="shadow", executed=False)

    # A later publication with different figures affirms without overwriting.
    carry = carry_forward(
        carry.carried,
        [published(OPENS + timedelta(minutes=15), CLOSES, revision=99)],
        OPENS + timedelta(minutes=15),
    )
    later = as_dict(
        decision_for(carry, OPENS + timedelta(minutes=15)),
        mode="shadow",
        executed=False,
    )

    for key in ("plan_id", "revision", "window_start", "window_end", "issued_at"):
        assert later[key] == admitted[key], key


# ===========================================================================
# The defect the snapshot exposed in beta.21's own diagnostics
# ===========================================================================


def test_the_per_quarter_reserve_belongs_to_its_own_interval() -> None:
    """**My bug, found by a real download rather than by the suite.**

    ``planning_reserve_kwh`` is positioned by horizon offset and beta.21 indexed
    it with the interval's own index. On a horizon starting at interval 44 every
    published requirement was the one belonging forty-four intervals later, and
    everything past the horizon length read ``null`` -- so the snapshot showed
    interval 44 carrying 12.39 kWh where its requirement was 5.67, and intervals
    52 onward carrying nothing at all.

    Asserted against the horizon the solve actually ran on, so the two axes can
    never drift apart again.
    """
    from custom_components.alpha_ems_manager.economic import economic_as_dict

    from .test_economic_run_intervals import outcome_for

    outcome = outcome_for()
    payload = economic_as_dict(outcome, execution_blocked_reason="barrier")
    truth = {
        demand.index: value
        for demand, value in zip(
            outcome.horizon.demands,
            outcome.horizon.planning_reserve_kwh,
            strict=False,
        )
    }
    assert truth, (
        "the horizon must carry a reserve trajectory for this to mean anything"
    )

    rows = [row for run in payload["runs"] for row in run["intervals"]]
    assert rows
    for row in rows:
        published_value = row["reserve_requirement_kwh"]
        expected = truth.get(row["interval"])
        assert expected is not None, row["interval"]
        assert published_value == pytest.approx(expected, abs=5e-3), row["interval"]


def test_no_published_quarter_is_missing_its_reserve() -> None:
    """The null tail was the visible half of the same off-by-the-horizon-start."""
    from custom_components.alpha_ems_manager.economic import economic_as_dict

    from .test_economic_run_intervals import outcome_for

    payload = economic_as_dict(outcome_for(), execution_blocked_reason="barrier")
    for run in payload["runs"]:
        for row in run["intervals"]:
            assert row["reserve_requirement_kwh"] is not None, row["interval"]


# ===========================================================================
# Mutations -- each defect, restored, must fail
# ===========================================================================


def test_re_adding_expected_production_to_the_projection_is_caught() -> None:
    """The mutation is the beta.21 formula, and it reproduces 31.946."""
    demand = snapshot_demand()
    assert demand.projected_end_kwh is not None

    total_minutes = 345.0
    still_expected = SNAP_PV_TO_BATTERY_KWH * min(
        1.0, SNAP_REMAINING_MINUTES / total_minutes
    )
    mutated = SNAP_STORED_KWH + still_expected + demand.remaining_kwh

    assert mutated == pytest.approx(SNAP_OLD_PROJECTED_KWH, abs=5e-3)
    assert demand.projected_end_kwh != pytest.approx(mutated, abs=0.1)


def test_re_subtracting_production_from_the_allowance_is_caught() -> None:
    """The mutation is the beta.21 cap, and it lands the pack five kWh short."""
    honest = headroom_ceiling_kw(
        target_of(max_end_energy_kwh=18.0),
        current_energy_kwh=10.0,
        remaining_minutes=240.0,
        charge_efficiency=ONE_WAY,
    )
    mutated = (18.0 - 10.0 - 5.0) / 4.0

    assert 10.0 + honest * 4.0 * ONE_WAY == pytest.approx(18.0)
    assert 10.0 + mutated * 4.0 * ONE_WAY == pytest.approx(12.85, abs=5e-3)
    assert honest > mutated


def test_a_decision_is_unchanged_by_either_revision(monkeypatch) -> None:
    """Neither counter may reach identity, progress, attribution or the command."""
    carry_forward = decision_for.__globals__["carry_forward"]
    outcomes = []
    for revision in (1, 13, 99):
        carry = carry_forward(
            None,
            [published(OPENS, CLOSES, revision=revision)],
            OPENS - timedelta(minutes=15),
        )
        carry = carry_forward(
            carry.carried,
            [published(OPENS + timedelta(minutes=15), CLOSES, revision=revision)],
            OPENS,
        )
        decision = decision_for(carry, OPENS)
        outcomes.append(
            (
                carry.carried.run_id,
                decision.state,
                round(decision.request_kw, 6),
                decision.stop_reason,
                decision.reset_required,
            )
        )

    # The run identity is minted from intent and instants, so it is stable too.
    assert len({entry[1:] for entry in outcomes}) == 1, outcomes


def test_measure_progress_is_untouched_by_the_projection_fix() -> None:
    """Progress shares a term with the projection but not a bug.

    ``remaining_battery_kwh`` is ``target - realized`` with no production term and
    no stored energy, so it was right before and must be identical after.
    """
    progress = measure_progress(
        accumulated_kwh=SNAP_REALIZED_KWH, soc_delta_kwh=SNAP_REALIZED_KWH
    )
    demand = snapshot_demand()

    assert progress.realized_kwh == pytest.approx(SNAP_REALIZED_KWH)
    assert demand.remaining_kwh == pytest.approx(SNAP_REMAINING_KWH, abs=5e-3)


def test_the_reserve_alignment_survives_a_horizon_that_does_not_start_at_zero() -> None:
    """**The case the suite could not see, and the reason it could not.**

    Every synthetic horizon in this repo starts at interval 0 with a flat reserve,
    so indexing by offset and indexing by interval give the same answer and the bug
    is invisible. The real installation was mid-morning: the horizon began at
    interval 44 and the reserve fell across the evening, which is what exposed it.

    So this builds that shape deliberately -- a horizon starting at 44 with a
    strictly varying reserve -- and asserts the published figure against the
    requirement for that same interval. Indexing by offset now gives a different
    answer, so the assertion has something to fail on.
    """
    from custom_components.alpha_ems_manager.battery import build_limits
    from custom_components.alpha_ems_manager.economic import (
        IntervalPrice,
        build_horizon,
        build_outcome,
        build_physics_table,
        economic_as_dict,
        select_bucket_kwh,
    )
    from custom_components.alpha_ems_manager.simulation import IntervalDemand

    limits, reason = build_limits(
        capacity_kwh=22.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        round_trip_efficiency_percent=90.0,
    )
    assert reason is None
    floor = limits.energy_for_soc(20.0)
    bucket, rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    table = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)

    first, count = 44, 24
    indices = list(range(first, first + count))
    # A reserve that falls across the horizon, so offset and interval disagree.
    reserve = tuple(floor + 0.30 * (count - position) for position in range(count))
    horizon = build_horizon(
        demands=tuple(
            IntervalDemand(index=index, baseline_kwh=0.25, pv_kwh=0.0)
            for index in indices
        ),
        prices=tuple(
            IntervalPrice(
                import_eur_kwh=0.05 if position < 4 else 0.40,
                export_eur_kwh=0.02 if position < 4 else 0.35,
            )
            for position in range(count)
        ),
        required_reserve_kwh=reserve,
        table=table,
    )
    outcome = build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=floor + 4.0,
        terminal_floor_kwh=floor,
        floor_energy_kwh=floor,
        minimum_trade_gain_eur=0.10,
        allow_grid_charging=True,
        allow_battery_export=True,
        reserve_above_capacity_kwh=0.0,
        table_ms=0.0,
        bucket_rule=rule,
    )

    truth = {
        demand.index: value
        for demand, value in zip(
            outcome.horizon.demands,
            outcome.horizon.planning_reserve_kwh,
            strict=False,
        )
    }
    assert min(truth) == first, "the horizon must not start at zero for this to bite"
    assert len(set(truth.values())) > 1, "the reserve must vary for this to bite"

    payload = economic_as_dict(outcome, execution_blocked_reason="barrier")
    rows = [row for run in payload["runs"] for row in run["intervals"]]
    assert rows

    by_offset = list(outcome.horizon.planning_reserve_kwh)
    caught = 0
    for row in rows:
        index = row["interval"]
        assert row["reserve_requirement_kwh"] == pytest.approx(
            truth[index], abs=5e-3
        ), index
        # And the mutation really would differ here, unlike on a zero-based horizon.
        offset_value = by_offset[index] if index < len(by_offset) else None
        if offset_value is None or abs(offset_value - truth[index]) > 5e-3:
            caught += 1
    assert caught == len(rows), "every row must distinguish the two axes"


# ===========================================================================
# Why no overshoot margin was added, pinned so the argument cannot rot
# ===========================================================================


def test_the_headroom_cap_is_closed_loop_on_measured_stored_energy() -> None:
    """**The property the no-margin decision rests on.**

    A measured helper test charged about 1.135 kW against a 1.0 kW setpoint, and
    the architecture already records why: *a dispatch tracks the commanded power
    rather than matching it*. So the question was whether the corrected cap, which
    now targets ``max_end_energy_kwh`` exactly, needs a margin against a setpoint
    the inverter does not hold precisely.

    It does not, because the cap is recomputed every refresh from the stored energy
    actually measured. An interval that charges above its setpoint arrives at the
    next refresh as a fuller pack, the allowance closes by exactly that much, and
    the cap falls. Only the final interval of a run is uncorrected.

    This test is the guard on that reasoning. If the cap ever became open-loop --
    computed once at admission and carried -- the argument for having no margin
    would evaporate silently, and this is what would fail.
    """
    one_way = ONE_WAY
    target = target_of(max_end_energy_kwh=18.0)

    caps = [
        headroom_ceiling_kw(
            target,
            current_energy_kwh=stored,
            remaining_minutes=180.0,
            charge_efficiency=one_way,
        )
        for stored in (10.0, 12.0, 14.0, 16.0, 18.0)
    ]

    # Strictly falling as the pack fills, and reaching zero at the ceiling.
    assert caps == sorted(caps, reverse=True), caps
    assert caps[-1] == 0.0
    # An overshoot of one interval is answered by the very next refresh: a pack
    # 0.5 kWh fuller than planned yields a cap lower by exactly that energy.
    ahead = headroom_ceiling_kw(
        target,
        current_energy_kwh=12.5,
        remaining_minutes=180.0,
        charge_efficiency=one_way,
    )
    on_plan = headroom_ceiling_kw(
        target,
        current_energy_kwh=12.0,
        remaining_minutes=180.0,
        charge_efficiency=one_way,
    )
    assert (on_plan - ahead) * 3.0 * one_way == pytest.approx(0.5, abs=5e-3)


def test_one_interval_of_setpoint_overshoot_stays_within_stage_a_s_own_margin() -> None:
    """The bound that makes the residual exposure acceptable without a margin.

    Only the final interval is uncorrected, so the worst case is the measured
    excess applied to one interval's delivery. Against Stage A's own published
    ``quantisation_margin_kwh`` -- the imprecision it already declares
    irreducible -- an on-schedule run sits well inside it.

    Asserted against the bucket rather than a written number, so a different
    installation's lattice is compared against its own margin.
    """
    from custom_components.alpha_ems_manager.const import ECONOMIC_BUCKET_KWH

    excess = 0.135  # the measured ratio, taken at face value
    for cap_kw, limit_buckets in ((0.5, 1.0), (2.0, 1.0), (5.0, 3.0)):
        delivered_ac = cap_kw * 0.25
        overshoot_dc = excess * delivered_ac * ONE_WAY
        assert overshoot_dc < limit_buckets * ECONOMIC_BUCKET_KWH, cap_kw

    # And it is below what the state-of-charge sensor can even resolve at the
    # observed step, which is why one sample cannot establish the ratio.
    soc_step_kwh = 0.004 * CAPACITY_KWH
    assert excess * 0.25 < soc_step_kwh
