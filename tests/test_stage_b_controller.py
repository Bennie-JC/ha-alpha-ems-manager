"""Stage B: the physical controller, and the economics it must never acquire.

Driven against the pure module, so every case here is a plain value rather than a
Home Assistant fixture. That is deliberate: the controller's judgement is small
enough to be stated exactly, and a test that needs an inverter to express "the
headroom cap bit" is a test nobody will read twice.

The two rules that look alike and are not
-----------------------------------------

Most of this file exists to keep them apart:

* the **rolling controller** may raise power, because being behind schedule on an
  already-approved target inside its own window is what it is for;
* the **headroom cap** may only lower what the rolling controller asked for.

A single "only reduce" rule would have been simpler to write and wrong. It would
silently under-deliver a plan Stage A chose every time a cloud passed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    EXECUTION_BASIS_ACCUMULATED,
    EXECUTION_BASIS_SOC_DELTA,
    EXECUTION_BASIS_UNAVAILABLE,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_QUALITY_PARTIAL,
    EXECUTION_REDUCTION_HEADROOM,
    EXECUTION_REDUCTION_NONE,
    EXECUTION_REDUCTION_PV_AHEAD,
    EXECUTION_REDUCTION_TARGET_MET,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_IDLE,
    EXECUTION_STATE_INHIBITED,
    EXECUTION_STATE_RUNNING,
    EXECUTION_STATE_STOPPING,
    EXECUTION_STATE_UNPROVEN,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_WINDOW_ENDED,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_UNPROVEN,
)
from custom_components.alpha_ems_manager.execution import (
    OwnershipEvidence,
    Target,
    actionable_target,
    battery_power_for_export_kw,
    decide,
    demand_for,
    headroom_ceiling_kw,
    measure_progress,
    ownership_of,
    parse_target,
    rolling_power_kw,
    serve_load_power_kw,
    stale_marker,
)

OPENS = datetime(2026, 8, 24, 10, 45, tzinfo=UTC)
CLOSES = datetime(2026, 8, 24, 16, 30, tzinfo=UTC)
ISSUED = OPENS - timedelta(minutes=5)


def raw_target(**overrides) -> dict:
    """Return a published charge target, shaped as Stage A publishes one.

    The figures are the worked example from the contract: an 11.94 kWh battery
    target across a sunny afternoon, of which production is expected to supply
    8.94 kWh and the grid at most 3.00 kWh.
    """
    payload = {
        "plan_id": "abc123",
        "revision": 1,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "purpose": "charge",
        "window_start": OPENS.isoformat(),
        "window_end": CLOSES.isoformat(),
        "issued_at": ISSUED.isoformat(),
        # Generous by default so a state-machine case is not accidentally
        # stale; the freshness test sets its own.
        "stale_after": (ISSUED + timedelta(hours=8)).isoformat(),
        "battery_target_kwh": 11.94,
        "grid_target_kwh": None,
        "average_power_kw": 2.08,
        "first_power_kw": 2.1,
        "reserve_floor_kwh": 4.25,
        "expected_pv_production_kwh": 15.2,
        "expected_house_load_kwh": 5.1,
        "expected_pv_to_battery_kwh": 8.94,
        "expected_grid_to_battery_kwh": 3.0,
        "charge_source": "mixed",
        "required_headroom_kwh": 4.0,
        "max_end_energy_kwh": 18.0,
        "headroom_until": CLOSES.isoformat(),
    }
    payload.update(overrides)
    return payload


def target_of(**overrides) -> Target:
    """Return a parsed target."""
    parsed = parse_target(raw_target(**overrides))
    assert parsed is not None
    return parsed


def progress_of(kwh: float) -> object:
    """Return measured progress of ``kwh`` delivered."""
    return measure_progress(accumulated_kwh=kwh, soc_delta_kwh=None)


# ===========================================================================
# A. the contract, read rather than interpreted
# ===========================================================================


def test_the_published_target_parses_into_every_field_it_carries() -> None:
    """Including the balance and the headroom constraint, which are the new part."""
    target = target_of()

    assert target.battery_target_kwh == pytest.approx(11.94)
    assert target.expected_pv_production_kwh == pytest.approx(15.2)
    assert target.expected_house_load_kwh == pytest.approx(5.1)
    assert target.expected_pv_to_battery_kwh == pytest.approx(8.94)
    assert target.expected_grid_to_battery_kwh == pytest.approx(3.0)
    assert target.max_end_energy_kwh == pytest.approx(18.0)
    assert target.constrained is True
    # Production is not production available to the battery. The house takes its
    # share first, and the contract publishes both so Stage B never has to guess.
    assert target.expected_pv_to_battery_kwh < target.expected_pv_production_kwh


def test_an_absent_headroom_constraint_means_unconstrained_not_zero() -> None:
    """**The mutation that would forbid the pack from filling at all.**

    ``None`` and ``0.0`` are opposite instructions here. Absent means Stage A has
    no view on the endpoint; zero would mean it must end empty.
    """
    target = target_of(required_headroom_kwh=None, max_end_energy_kwh=None)

    assert target.required_headroom_kwh is None
    assert target.max_end_energy_kwh is None
    assert target.constrained is False
    assert (
        headroom_ceiling_kw(
            target,
            current_energy_kwh=6.0,
            remaining_expected_pv_kwh=8.94,
            remaining_minutes=180.0,
        )
        is None
    )


def test_the_first_power_and_the_mean_are_different_published_figures() -> None:
    """beta.18 published only the mean, under a name that said "initial"."""
    target = target_of()

    assert target.average_power_kw == pytest.approx(2.08)
    assert target.first_power_kw == pytest.approx(2.1)


def test_a_target_missing_the_honest_power_falls_back_to_the_mean() -> None:
    """A beta.18 document has no ``first_power_kw``. It must still be usable.

    Falling back to the mean rather than to zero: a controller handed no first
    figure should start at the run's average, not refuse to start.
    """
    payload = raw_target()
    del payload["first_power_kw"]
    del payload["average_power_kw"]
    payload["initial_average_power_kw"] = 2.08

    target = parse_target(payload)

    assert target is not None
    assert target.average_power_kw == pytest.approx(2.08)
    assert target.first_power_kw == pytest.approx(2.08)


def test_freshness_is_measured_from_the_issue_instant() -> None:
    """And so a run hours away is stale long before its window opens."""
    target = target_of(stale_after=(ISSUED + timedelta(minutes=30)).isoformat())

    assert target.stale_at(ISSUED + timedelta(minutes=10)) is False
    assert target.stale_at(ISSUED + timedelta(minutes=31)) is True
    # The window is a separate fact and is not consulted for freshness.
    assert target.covers(OPENS + timedelta(hours=1)) is True
    assert target.covers(CLOSES) is False


def test_the_actionable_target_is_the_one_covering_now_and_nothing_else() -> None:
    """Stage B does not rank targets, because ranking them is choosing.

    Two windows, one current. Picking "the more valuable" would be an economic
    decision, and there is deliberately no code that could make one.
    """
    later = raw_target(
        plan_id="def456",
        window_start=(CLOSES + timedelta(hours=1)).isoformat(),
        window_end=(CLOSES + timedelta(hours=2)).isoformat(),
        battery_target_kwh=99.0,
    )

    chosen = actionable_target([raw_target(), later], OPENS + timedelta(minutes=30))

    assert chosen is not None
    assert chosen.plan_id == "abc123"
    assert chosen.battery_target_kwh == pytest.approx(11.94)


# ===========================================================================
# B. progress: measured, never inferred from the setpoint
# ===========================================================================


def test_progress_prefers_the_integral_and_names_its_basis() -> None:
    """Both bases are published rather than reconciled."""
    progress = measure_progress(accumulated_kwh=3.2, soc_delta_kwh=3.0, coverage=1.0)

    assert progress.realized_kwh == pytest.approx(3.2)
    assert progress.accumulated_kwh == pytest.approx(3.2)
    assert progress.soc_delta_kwh == pytest.approx(3.0)
    # Where they disagree the disagreement is the information.
    assert progress.accumulated_kwh != progress.soc_delta_kwh


def test_a_restart_reconstructs_from_the_level_and_says_so() -> None:
    """The state-of-charge difference is the only basis that survives a reboot."""
    progress = measure_progress(
        accumulated_kwh=None, soc_delta_kwh=4.0, reconstructed=True
    )

    assert progress.realized_kwh == pytest.approx(4.0)
    assert progress.basis == EXECUTION_BASIS_SOC_DELTA
    assert progress.quality == "reconstructed"


def test_poor_coverage_is_reported_as_partial_rather_than_measured() -> None:
    """The first quarter after a restart can never reach the threshold.

    Coverage is measured against the whole quarter, so a quarter that began before
    the integration did is structurally short. Calling that ``measured`` would
    dress a gap as a reading.
    """
    progress = measure_progress(
        accumulated_kwh=0.4, soc_delta_kwh=None, coverage=0.3, minimum_coverage=0.8
    )

    assert progress.basis == EXECUTION_BASIS_ACCUMULATED
    assert progress.quality == EXECUTION_QUALITY_PARTIAL


def test_no_evidence_at_all_is_unavailable_and_not_zero_delivered() -> None:
    """Nought delivered and nobody looking are different facts."""
    progress = measure_progress(accumulated_kwh=None, soc_delta_kwh=None)

    assert progress.basis == EXECUTION_BASIS_UNAVAILABLE
    assert progress.available is False


def test_natural_production_charging_counts_toward_the_target() -> None:
    """**The answer to charging the same kilowatt-hour twice.**

    Progress is measured at the battery, so energy the sun put there reduces what
    is left exactly as bought energy does. No attribution is needed, and none is
    attempted.
    """
    target = target_of()
    # Four kilowatt-hours arrived. Where they came from is not asked.
    demand = demand_for(
        target,
        now=OPENS + timedelta(hours=1),
        progress=progress_of(4.0),
        current_energy_kwh=10.0,
        remaining_expected_pv_kwh=5.0,
    )

    assert demand.remaining_kwh == pytest.approx(11.94 - 4.0)


# ===========================================================================
# C. the rolling controller -- which may raise power
# ===========================================================================


def test_being_behind_schedule_raises_the_power_asked_for() -> None:
    """**And that is correct, not a violation of the reduce-only rule.**

    Stage A's figure is the mean over an undisturbed run. A run that lost ground
    needs more than the mean to deliver the same energy in the time that is left,
    and refusing to catch up would quietly under-deliver a plan Stage A chose.

    What may never rise is the target. This raises the rate.
    """
    target = target_of(required_headroom_kwh=None, max_end_energy_kwh=None)
    halfway = OPENS + (CLOSES - OPENS) / 2

    # Half the window gone, nothing delivered.
    behind = demand_for(target, now=halfway, progress=progress_of(0.0))

    assert behind.ahead_kwh < 0.0
    assert behind.required_kw > target.average_power_kw
    assert behind.remaining_kwh == pytest.approx(11.94)
    # The target itself is untouched.
    assert target.battery_target_kwh == pytest.approx(11.94)


def test_being_ahead_of_schedule_lowers_the_power_asked_for() -> None:
    """The same arithmetic in the other direction, and no cap needed for it."""
    target = target_of(required_headroom_kwh=None, max_end_energy_kwh=None)
    halfway = OPENS + (CLOSES - OPENS) / 2

    ahead = demand_for(target, now=halfway, progress=progress_of(9.0))

    assert ahead.ahead_kwh > 0.0
    assert ahead.required_kw < target.average_power_kw


def test_the_rolling_power_is_remaining_energy_over_remaining_time() -> None:
    """Stated directly, because everything else is built on it."""
    assert rolling_power_kw(
        remaining_kwh=6.0, remaining_minutes=120.0
    ) == pytest.approx(3.0)
    assert rolling_power_kw(remaining_kwh=0.0, remaining_minutes=60.0) == 0.0
    assert rolling_power_kw(remaining_kwh=5.0, remaining_minutes=0.0) == 0.0


def test_reaching_the_target_stops_immediately() -> None:
    """Whatever the device's own countdown still says.

    A dispatch left armed because a timer has not expired is how a target gets
    exceeded, and the timer is a dead-man rather than a delivery window.
    """
    target = target_of()

    demand = demand_for(
        target,
        now=OPENS + timedelta(minutes=30),
        progress=progress_of(11.94),
        current_energy_kwh=18.0,
    )

    assert demand.required_kw == 0.0
    assert demand.finished is True
    assert demand.reduction == EXECUTION_REDUCTION_TARGET_MET


# ===========================================================================
# D. the headroom cap -- which may only lower
# ===========================================================================


def test_the_headroom_cap_is_stage_a_arithmetic_and_nothing_else() -> None:
    """Allowance over remaining time, from three published numbers.

    Stage A decided the pack should land on 18.00 kWh knowing what production was
    forecast afterwards. Six kilowatt-hours are stored and 8.94 are still expected
    from the sun, so 3.06 remain for the grid to supply -- over three hours, a
    1.02 kW cap.
    """
    target = target_of()

    ceiling = headroom_ceiling_kw(
        target,
        current_energy_kwh=6.0,
        remaining_expected_pv_kwh=8.94,
        remaining_minutes=180.0,
    )

    assert ceiling == pytest.approx((18.0 - 6.0 - 8.94) / 3.0)
    assert ceiling == pytest.approx(1.02)


def test_production_ahead_of_forecast_shrinks_the_cap_to_zero() -> None:
    """**The canonical case, and the reason the constraint exists.**

    An old cheap-grid charge target is still live while Stage A expects
    substantial production before a later window. If the sun is filling the pack
    faster than forecast, the room left for bought energy shrinks -- and reaches
    nothing, which means stop. The production Stage A meant to absorb is not
    displaced by charging the pack full early.
    """
    target = target_of()

    # The pack is already at 15 kWh and 8.94 more are still expected: together
    # that overshoots the 18.00 kWh Stage A chose to land on.
    ceiling = headroom_ceiling_kw(
        target,
        current_energy_kwh=15.0,
        remaining_expected_pv_kwh=8.94,
        remaining_minutes=180.0,
    )

    assert ceiling == 0.0


def test_the_cap_can_only_lower_what_the_rolling_controller_asked_for() -> None:
    """It is applied afterwards, and it is one-directional.

    Proven by construction: whatever the cap says, the request is the lesser of
    the two. A cap above the rolling figure changes nothing.
    """
    target = target_of()
    halfway = OPENS + (CLOSES - OPENS) / 2

    # Behind schedule, and plenty of headroom left: the cap must not bite.
    generous = demand_for(
        target,
        now=halfway,
        progress=progress_of(1.0),
        current_energy_kwh=5.0,
        remaining_expected_pv_kwh=0.0,
    )
    assert generous.ceiling_kw is not None
    assert generous.required_kw == pytest.approx(generous.rolling_kw)
    assert generous.reduction == EXECUTION_REDUCTION_NONE

    # The same moment with the pack nearly at its cap: now it bites, and the
    # request is the cap rather than the rolling figure.
    tight = demand_for(
        target,
        now=halfway,
        progress=progress_of(1.0),
        current_energy_kwh=17.0,
        remaining_expected_pv_kwh=0.5,
    )
    assert tight.ceiling_kw is not None
    assert tight.required_kw < tight.rolling_kw
    assert tight.required_kw == pytest.approx(tight.ceiling_kw)
    assert tight.reduced is True


def test_the_reduction_reason_distinguishes_overshoot_from_design() -> None:
    """A cap that bit because production ran ahead is a different event.

    Both reduce charging. Only one of them means the forecast was wrong, and a
    reader chasing a deviation needs to know which.
    """
    target = target_of()
    halfway = OPENS + (CLOSES - OPENS) / 2

    # Ahead of schedule and capped: production overshot.
    overshot = demand_for(
        target,
        now=halfway,
        progress=progress_of(10.0),
        current_energy_kwh=17.5,
        remaining_expected_pv_kwh=0.4,
    )
    # Behind schedule and capped: the plan always meant to stop here.
    by_design = demand_for(
        target,
        now=halfway,
        progress=progress_of(1.0),
        current_energy_kwh=17.5,
        remaining_expected_pv_kwh=0.4,
    )

    assert overshot.reduction == EXECUTION_REDUCTION_PV_AHEAD
    assert by_design.reduction == EXECUTION_REDUCTION_HEADROOM


def test_production_below_forecast_does_not_buy_more() -> None:
    """**Stage B never invents an economic decision.**

    The sun disappointed, so the target will not be met from production. Stage B
    does not make up the difference at the grid: buying more is a fresh economic
    decision, and the only thing here that may decide one is Stage A.

    What it does instead is deliver the rolling figure for the *approved* target
    and report the shortfall, which is what a replan is built from.
    """
    target = target_of()
    halfway = OPENS + (CLOSES - OPENS) / 2

    demand = demand_for(
        target,
        now=halfway,
        progress=progress_of(2.0),
        current_energy_kwh=7.0,
        # Production has collapsed: almost nothing more is coming.
        remaining_expected_pv_kwh=0.1,
    )

    # The request is the rolling figure for the approved target, and no more.
    assert demand.required_kw == pytest.approx(demand.rolling_kw)
    assert demand.remaining_kwh == pytest.approx(11.94 - 2.0)
    # And the cap, computed from the same published numbers, has not been raised
    # to compensate for the missing sun.
    assert demand.ceiling_kw is not None
    assert demand.required_kw <= demand.ceiling_kw + 1e-9


# ===========================================================================
# E. ownership -- two factors, both required
# ===========================================================================


def matching_record(plan_id: str = "abc123", start: float = 1000.0) -> dict:
    """Return a causal record that ties to a dispatch starting at ``start``."""
    return {
        "plan_id": plan_id,
        "dispatch_start": datetime(2026, 8, 24, 10, 46, tzinfo=UTC).isoformat(),
        "power_kw": 2.1,
    }


DISPATCH_START = datetime(2026, 8, 24, 10, 46, tzinfo=UTC)


@pytest.mark.parametrize(
    ("active", "marker", "record", "expected"),
    [
        (False, False, None, OWNERSHIP_NONE),
        (False, True, None, OWNERSHIP_NONE),
        (True, False, None, OWNERSHIP_FOREIGN),
        (True, False, matching_record(), OWNERSHIP_FOREIGN),
        (True, True, None, OWNERSHIP_UNPROVEN),
        (True, True, {}, OWNERSHIP_UNPROVEN),
        (True, True, {"plan_id": "other"}, OWNERSHIP_UNPROVEN),
        (True, True, matching_record(), OWNERSHIP_OWNED),
    ],
)
def test_the_ownership_matrix(active, marker, record, expected) -> None:
    """Every combination of dispatch, marker and record, stated once.

    The two that matter most: a marker alone is ``unproven``, and a record alone
    is ``foreign``. Neither is ``owned``, because either on its own is exactly the
    inference the control surface makes untrustworthy.
    """
    evidence = OwnershipEvidence(
        dispatch_active=active,
        marker_on=marker,
        record=record,
        dispatch_start=DISPATCH_START if active else None,
        plan_id="abc123",
    )

    assert ownership_of(evidence) == expected


def test_a_marker_alone_never_proves_ownership() -> None:
    """Stated on its own because it is the tempting shortcut."""
    evidence = OwnershipEvidence(
        dispatch_active=True, marker_on=True, record=None, dispatch_start=DISPATCH_START
    )

    assert ownership_of(evidence) == OWNERSHIP_UNPROVEN
    assert ownership_of(evidence) != OWNERSHIP_OWNED


def test_a_record_alone_never_proves_ownership() -> None:
    """The other half, and the one that is parameter matching in disguise."""
    evidence = OwnershipEvidence(
        dispatch_active=True,
        marker_on=False,
        record=matching_record(),
        dispatch_start=DISPATCH_START,
    )

    assert ownership_of(evidence) == OWNERSHIP_FOREIGN


def test_a_record_for_a_different_plan_is_contradictory_not_merely_old() -> None:
    """So a stale record cannot be stretched over whatever is running now."""
    evidence = OwnershipEvidence(
        dispatch_active=True,
        marker_on=True,
        record=matching_record(plan_id="something_else"),
        dispatch_start=DISPATCH_START,
        plan_id="abc123",
    )

    assert ownership_of(evidence) == OWNERSHIP_UNPROVEN


def test_a_dispatch_that_started_far_from_the_record_is_not_ours() -> None:
    """The corroborating condition has to actually corroborate something."""
    evidence = OwnershipEvidence(
        dispatch_active=True,
        marker_on=True,
        record=matching_record(),
        dispatch_start=DISPATCH_START + timedelta(hours=2),
        plan_id="abc123",
    )

    assert ownership_of(evidence) == OWNERSHIP_UNPROVEN


def test_a_missing_device_start_is_not_read_as_agreement() -> None:
    """Silence is not a match. The settle window genuinely reports nothing."""
    evidence = OwnershipEvidence(
        dispatch_active=True,
        marker_on=True,
        record=matching_record(),
        dispatch_start=None,
        plan_id="abc123",
    )

    assert ownership_of(evidence) == OWNERSHIP_UNPROVEN


def test_a_marker_with_nothing_running_is_stale_and_clearable() -> None:
    """Clearing it is not an ownership claim -- there is nothing to claim."""
    evidence = OwnershipEvidence(dispatch_active=False, marker_on=True)

    assert stale_marker(evidence) is True
    assert ownership_of(evidence) == OWNERSHIP_NONE


# ===========================================================================
# F. the state machine
# ===========================================================================


def decision_at(
    now: datetime,
    *,
    mode_executes: bool = False,
    mode_off: bool = False,
    evidence: OwnershipEvidence | None = None,
    delivered: float = 0.0,
    stored: float = 8.0,
    pv_left: float = 4.0,
    targets: list[dict] | None = None,
    running_plan_id: str | None = None,
) -> object:
    """Return a decision for one refresh."""
    return decide(
        mode_executes=mode_executes,
        mode_off=mode_off,
        targets=raw_target() if targets is None else targets,
        now=now,
        evidence=evidence or OwnershipEvidence(dispatch_active=False, marker_on=False),
        progress=progress_of(delivered),
        current_energy_kwh=stored,
        remaining_expected_pv_kwh=pv_left,
        running_plan_id=running_plan_id,
    )


def test_a_foreign_dispatch_is_never_touched() -> None:
    """**The one case with no discretion at all.**

    Somebody armed the inverter by hand. Alpha EMS stands down: no command, no
    reset, no claim. This is the promise the project has made since Phase 4 and
    the reason the marker exists at all.
    """
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        targets=[raw_target()],
        evidence=OwnershipEvidence(dispatch_active=True, marker_on=False),
    )

    assert decision.state == EXECUTION_STATE_INHIBITED
    assert decision.ownership == OWNERSHIP_FOREIGN
    assert decision.reset_required is False
    assert decision.request_kw == 0.0
    assert decision.inhibit_reason == "foreign_dispatch"


def test_unproven_ownership_also_touches_nothing_but_reports_differently() -> None:
    """It might be ours, and that is a fault rather than a normal condition.

    Same restraint, different report. Calling it foreign would hide a problem
    worth investigating; acting on it would be worse.
    """
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        targets=[raw_target()],
        evidence=OwnershipEvidence(
            dispatch_active=True, marker_on=True, record=None, dispatch_start=None
        ),
    )

    assert decision.state == EXECUTION_STATE_UNPROVEN
    assert decision.ownership == OWNERSHIP_UNPROVEN
    assert decision.reset_required is False
    assert decision.request_kw == 0.0


def test_shadow_computes_a_request_and_is_never_running() -> None:
    """Armed rather than running, because nothing was ever armed."""
    decision = decision_at(OPENS + timedelta(minutes=30), targets=[raw_target()])

    assert decision.state == EXECUTION_STATE_ARMED
    assert decision.ownership == OWNERSHIP_NONE
    assert decision.request_kw > 0.0
    assert decision.wants_command is True


def test_an_owned_run_reports_running() -> None:
    """And is controllable, which is the whole reason ownership was needed."""
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_executes=True,
        targets=[raw_target()],
        evidence=OwnershipEvidence(
            dispatch_active=True,
            marker_on=True,
            record=matching_record(),
            dispatch_start=DISPATCH_START,
            plan_id="abc123",
        ),
    )

    assert decision.ownership == OWNERSHIP_OWNED
    assert decision.state == EXECUTION_STATE_RUNNING


def owned_evidence() -> OwnershipEvidence:
    """Return evidence that establishes ownership."""
    return OwnershipEvidence(
        dispatch_active=True,
        marker_on=True,
        record=matching_record(),
        dispatch_start=DISPATCH_START,
        plan_id="abc123",
    )


def test_switching_to_shadow_stops_an_owned_run_and_keeps_planning() -> None:
    """The run stops. The planning does not."""
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_executes=False,
        targets=[raw_target()],
        evidence=owned_evidence(),
    )

    assert decision.state == EXECUTION_STATE_STOPPING
    assert decision.stop_reason == EXECUTION_STOP_SWITCHED_TO_SHADOW
    assert decision.reset_required is True


def test_switching_off_stops_an_owned_run() -> None:
    """And leaves a foreign one alone, which the matrix above already showed."""
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_off=True,
        targets=[raw_target()],
        evidence=owned_evidence(),
    )

    assert decision.state == EXECUTION_STATE_STOPPING
    assert decision.stop_reason == EXECUTION_STOP_SWITCHED_OFF
    assert decision.reset_required is True


def test_switching_off_never_resets_a_foreign_dispatch() -> None:
    """**The transition must not become a licence to touch someone else's run.**"""
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_off=True,
        targets=[raw_target()],
        evidence=OwnershipEvidence(dispatch_active=True, marker_on=False),
    )

    assert decision.ownership == OWNERSHIP_FOREIGN
    assert decision.reset_required is False


def test_stage_a_withdrawing_the_target_stops_an_owned_run() -> None:
    """Hold means stop, and the reason says which kind of stop it was."""
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_executes=True,
        targets=[],
        evidence=owned_evidence(),
    )

    assert decision.state == EXECUTION_STATE_STOPPING
    assert decision.stop_reason == "stage_a_hold"
    assert decision.reset_required is True


def test_a_different_plan_ends_the_old_run_before_the_new_one_starts() -> None:
    """A direction change is never expressed as a parameter edit."""
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_executes=True,
        targets=[raw_target()],
        evidence=owned_evidence(),
        running_plan_id="an_older_plan",
    )

    assert decision.state == EXECUTION_STATE_STOPPING
    assert decision.stop_reason == EXECUTION_STOP_PLAN_REPLACED
    assert decision.reset_required is True


def test_a_stale_target_may_not_start_and_stops_a_running_one() -> None:
    """Freshness is enforced from beta.19; beta.18 enforced nothing."""
    late = ISSUED + timedelta(hours=1)
    soon_stale = [raw_target(stale_after=(ISSUED + timedelta(minutes=30)).isoformat())]

    idle = decision_at(late, targets=soon_stale)
    running = decision_at(
        late, mode_executes=True, targets=soon_stale, evidence=owned_evidence()
    )

    assert idle.state == EXECUTION_STATE_INHIBITED
    assert idle.request_kw == 0.0
    assert running.state == EXECUTION_STATE_STOPPING
    assert running.stop_reason == EXECUTION_STOP_STALE_PLAN


def test_the_window_ending_short_stops_without_extending_it() -> None:
    """Stage A decides whether the rest is still worth having."""
    decision = decide(
        mode_executes=True,
        mode_off=False,
        targets=[raw_target(window_end=(OPENS + timedelta(minutes=10)).isoformat())],
        now=OPENS + timedelta(minutes=20),
        evidence=owned_evidence(),
        progress=progress_of(3.0),
        current_energy_kwh=9.0,
        running_plan_id="abc123",
    )

    assert decision.state == EXECUTION_STATE_STOPPING
    assert decision.stop_reason == EXECUTION_STOP_WINDOW_ENDED
    # The window is not moved to accommodate the shortfall.
    assert decision.target is None or decision.target.window_end <= OPENS + timedelta(
        minutes=10
    )


def test_reaching_the_target_early_stops_the_owned_run() -> None:
    """Immediately, and the device's own countdown is irrelevant."""
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_executes=True,
        targets=[raw_target()],
        evidence=owned_evidence(),
        delivered=11.94,
        stored=18.0,
    )

    assert decision.state == EXECUTION_STATE_STOPPING
    assert decision.stop_reason == EXECUTION_STOP_TARGET_REACHED
    assert decision.reset_required is True


def test_the_cap_reducing_to_nothing_stops_and_waits_for_stage_a() -> None:
    """**It does not decide the remaining energy is worth buying anyway.**

    Reduced to zero by the published constraint, so the run holds and Stage A is
    left to make the economic decision that would be required to continue.
    """
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_executes=True,
        targets=[raw_target()],
        evidence=owned_evidence(),
        delivered=2.0,
        # Already at the cap, with production still to come.
        stored=18.0,
        pv_left=2.0,
    )

    assert decision.request_kw == 0.0
    assert decision.state == EXECUTION_STATE_STOPPING
    assert any("awaiting a fresh economic decision" in n for n in decision.notes)


def test_no_target_and_nothing_running_is_simply_idle() -> None:
    """The common case, and it is silent."""
    decision = decision_at(OPENS + timedelta(minutes=30), targets=[])

    assert decision.state == EXECUTION_STATE_IDLE
    assert decision.stop_reason is None
    assert decision.reset_required is False


# ===========================================================================
# G. the other two intents, computed and never actuated in beta.19
# ===========================================================================


def test_a_net_export_needs_the_house_load_added_at_the_battery() -> None:
    """The live case: 1.3 kW of export against 0.9 kW of load needs 2.2 kW.

    The *meter* is the target, so the battery must cover the house as well. This
    is the mirror image of the charge rule, and confusing the two is how 1.3 kW of
    intended export becomes a 1.3 kW command that delivers 0.4.
    """
    assert battery_power_for_export_kw(
        grid_target_kw=1.3, house_load_kw=0.9
    ) == pytest.approx(2.2)


def test_load_avoidance_never_discharges_past_the_house() -> None:
    """Past the load it becomes an export, which is a different intent."""
    assert serve_load_power_kw(house_load_kw=0.9, remaining_kw=5.0) == pytest.approx(
        0.9
    )
    assert serve_load_power_kw(house_load_kw=3.0, remaining_kw=1.2) == pytest.approx(
        1.2
    )
    assert serve_load_power_kw(house_load_kw=0.0, remaining_kw=5.0) == 0.0


def test_the_charge_setpoint_never_includes_the_house_load() -> None:
    """**The pinned live physics, stated as a property of the controller.**

    3.7 kW of battery charging against 1.1 kW of house and 0.63 kW of production
    draws 4.17 kW at the meter. The meter figure is a consequence; the command is
    the battery figure. A controller that added the load would ask for 4.81 kW and
    overcharge by the whole house.
    """
    target = target_of(
        battery_target_kwh=3.7 * 1.0,
        required_headroom_kwh=None,
        max_end_energy_kwh=None,
    )

    demand = demand_for(target, now=OPENS, progress=progress_of(0.0))

    # One hour of window remaining would give exactly the battery figure. The
    # window here is longer, so assert the invariant rather than a number: the
    # request is derived from battery energy alone, and no load term appears.
    assert demand.rolling_kw == pytest.approx(
        target.battery_target_kwh / ((CLOSES - OPENS).total_seconds() / 3600.0)
    )
    # Adding 1.1 kW of house load would be visible immediately.
    assert demand.rolling_kw < target.battery_target_kwh + 1.1
