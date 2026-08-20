"""Walking a battery through a day, and the policies that drive it.

Two rules dominate: the simulator decides nothing, and an interval nobody
forecast is not an interval of zero load. Everything else here is arithmetic,
asserted at exact values wherever the arithmetic is exact.

Daylight saving is covered at all three real day lengths, because the count of
intervals in a civil day changes and their duration never does -- which is
precisely the confusion this design exists to avoid.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.alpha_ems_manager.battery import (
    INTERVAL_HOURS,
    BatteryRequest,
    advance,
    apply_request,
)
from custom_components.alpha_ems_manager.const import (
    CONSTRAINT_MIN_SOC,
    MODE_DISCHARGE,
    MODE_IDLE,
    REASON_AT_RESERVE,
    REASON_BELOW_RESERVE,
    REASON_COVER_FORECAST_LOAD,
    REASON_FORECAST_UNAVAILABLE,
    REASON_POLICY_HOLD,
)
from custom_components.alpha_ems_manager.policy import (
    DEFAULT_POLICY,
    POLICY_HOLD,
    POLICY_RESERVE_GUARD,
    SHIPPED_POLICIES,
    HoldPolicy,
    ReserveGuardPolicy,
    emits_charge,
)
from custom_components.alpha_ems_manager.simulation import (
    IntervalDemand,
    compare,
    constant_provider,
    demands_from_forecast,
    mode_counts,
    sequence_provider,
    simulate,
)
from custom_components.alpha_ems_manager.storage import expected_quarters_for

from .test_battery_model import limits_for, state_for

TZ = ZoneInfo("Europe/Amsterdam")
NORMAL = date(2026, 8, 19)
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)

#: A flat quarter-hourly demand of 0.125 kWh, which is 0.5 kW average.
FLAT = 0.125


def flat_demands(count: int, value: float | None = FLAT) -> tuple[IntervalDemand, ...]:
    """Return ``count`` intervals of identical demand."""
    return tuple(
        IntervalDemand(index=index, baseline_kwh=value) for index in range(count)
    )


# -- the simulator decides nothing -------------------------------------------


def test_a_hold_trajectory_moves_no_energy_at_all() -> None:
    """The reference trajectory: what happens if the battery is left alone."""
    state = state_for(80.0)
    trajectory = simulate(
        state, flat_demands(96), constant_provider(BatteryRequest.idle())
    )

    assert trajectory.intervals == 96
    assert trajectory.end_soc_percent == 80.0
    assert trajectory.discharged_ac_kwh == 0.0
    assert trajectory.charged_ac_kwh == 0.0
    # Every kilowatt-hour of load comes from the grid.
    assert trajectory.grid_import_kwh == pytest.approx(12.0)
    assert trajectory.grid_export_kwh == 0.0
    assert mode_counts(trajectory)[MODE_IDLE] == 96


def test_covering_the_load_drains_exactly_to_the_floor() -> None:
    """Eighty per cent down to twenty of a 10 kWh pack is 6 kWh DC.

    At 90 % round trip that delivers ``6 * sqrt(0.9)`` = 5.6921 kWh AC, and the
    rest of the day's 12 kWh comes from the grid. Both halves are asserted, so
    energy cannot go missing between them.
    """
    state = state_for(80.0, min_soc=20.0)
    trajectory = simulate(state, flat_demands(96), ReserveGuardPolicy().provider())

    delivered = 6.0 * math.sqrt(0.9)
    assert trajectory.discharged_ac_kwh == pytest.approx(delivered)
    assert trajectory.end_soc_percent == pytest.approx(20.0)
    assert trajectory.minimum_soc_percent == pytest.approx(20.0)
    assert trajectory.grid_import_kwh == pytest.approx(12.0 - delivered)
    # Nothing is lost: what the battery gave plus what the grid gave is the load.
    assert trajectory.discharged_ac_kwh + trajectory.grid_import_kwh == pytest.approx(
        12.0
    )


def test_the_minimum_is_reported_with_the_interval_it_happens_at() -> None:
    """A projected minimum nobody can locate in time is half a fact."""
    trajectory = simulate(
        state_for(80.0), flat_demands(96), ReserveGuardPolicy().provider()
    )

    assert trajectory.minimum_soc_index is not None
    assert 0 < trajectory.minimum_soc_index < 96
    # 6 kWh DC at 0.125 kWh AC per interval, so the floor arrives well before the
    # end of the day but not immediately.
    assert trajectory.constraint_counts[CONSTRAINT_MIN_SOC] > 0


def test_a_fixed_sequence_of_requests_is_replayed_by_index() -> None:
    """A plan can be handed over wholesale, which is what what-if needs."""
    requests = [BatteryRequest.discharge(2.0)] * 4 + [BatteryRequest.idle()] * 4
    trajectory = simulate(state_for(80.0), flat_demands(8), sequence_provider(requests))

    assert mode_counts(trajectory)[MODE_DISCHARGE] == 4
    assert mode_counts(trajectory)[MODE_IDLE] == 4


def test_an_index_outside_the_sequence_is_idle_rather_than_a_neighbour() -> None:
    """An out-of-range index must not escape, and must not silently reuse."""
    trajectory = simulate(
        state_for(80.0),
        flat_demands(8),
        sequence_provider([BatteryRequest.discharge(2.0)]),
    )

    assert mode_counts(trajectory)[MODE_DISCHARGE] == 1
    assert mode_counts(trajectory)[MODE_IDLE] == 7


# -- a missing forecast is not a quiet house ---------------------------------


def test_an_unpredicted_interval_contributes_to_no_grid_total() -> None:
    """It is not an interval of zero load, and must not be counted as one.

    The battery still advances -- the request is applied -- but the grid residual
    for an interval nobody forecast does not exist, so it is ``None`` rather than
    a zero that would flatter the totals.
    """
    demands = (
        IntervalDemand(index=0, baseline_kwh=FLAT),
        IntervalDemand(index=1, baseline_kwh=None),
        IntervalDemand(index=2, baseline_kwh=FLAT),
    )
    trajectory = simulate(
        state_for(80.0), demands, constant_provider(BatteryRequest.idle())
    )

    assert trajectory.intervals == 3
    assert trajectory.intervals_with_demand == 2
    assert trajectory.grid[1] is None
    assert trajectory.grid_import_kwh == pytest.approx(2 * FLAT)
    assert trajectory.demand_kwh == pytest.approx(2 * FLAT)


def test_the_neighbour_filled_mask_survives_into_the_trajectory() -> None:
    """A consumer that wants to widen its margin has to be able to tell."""
    filled = [False] * 96
    for index in range(8):
        filled[index] = True
    demands = demands_from_forecast([FLAT] * 96, filled)

    trajectory = simulate(
        state_for(80.0), demands, constant_provider(BatteryRequest.idle())
    )

    assert trajectory.intervals_filled == 8


def test_a_withheld_forecast_yields_no_demands_at_all() -> None:
    """Nothing to walk, rather than a day of invented load."""
    assert demands_from_forecast([], []) == ()


# -- daylight saving ---------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "intervals"),
    [(SPRING_FORWARD, 92), (NORMAL, 96), (FALL_BACK, 100)],
)
def test_a_civil_day_is_walked_at_its_real_length(day: date, intervals: int) -> None:
    """92, 96 and 100 intervals, each of them fifteen minutes long.

    The count changes with daylight saving and the duration does not, so the
    energy per interval is identical on all three days and only the number of
    them differs. Asserting the totals is what would catch a duration that had
    been scaled to "fit" a short or long day.
    """
    assert expected_quarters_for(day, TZ) == intervals

    state = state_for(100.0, min_soc=0.0, capacity_kwh=200.0)
    demands = flat_demands(intervals)
    trajectory = simulate(state, demands, ReserveGuardPolicy().provider())

    assert trajectory.intervals == intervals
    assert trajectory.intervals_with_demand == intervals
    # Every interval covered from a battery far larger than the day's demand.
    assert trajectory.discharged_ac_kwh == pytest.approx(intervals * FLAT)
    assert trajectory.grid_import_kwh == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("day", [SPRING_FORWARD, NORMAL, FALL_BACK])
def test_the_band_summary_uses_the_wall_clock_not_the_index(day: date) -> None:
    """On a fall-back day two distinct indices share one behavioural band.

    Which is exactly why the band is derived from each interval's own local time
    rather than from its position: mapping by index would put the repeated hour in
    the wrong band on one day a year and nobody would notice.
    """
    intervals = expected_quarters_for(day, TZ)
    state = state_for(100.0, min_soc=0.0, capacity_kwh=200.0)
    trajectory = simulate(
        state, flat_demands(intervals), ReserveGuardPolicy().provider()
    )

    bands = trajectory.band_summary(day, TZ)

    assert set(bands) == {"night", "morning", "afternoon", "evening"}
    total = sum(band["discharged_kwh"] for band in bands.values())
    assert total == pytest.approx(intervals * FLAT, abs=0.02)


def test_the_fall_back_day_puts_more_intervals_in_the_night_band() -> None:
    """The repeated hour is two real intervals, and both are counted."""
    normal = simulate(
        state_for(100.0, min_soc=0.0, capacity_kwh=200.0),
        flat_demands(96),
        ReserveGuardPolicy().provider(),
    ).band_summary(NORMAL, TZ)
    folded = simulate(
        state_for(100.0, min_soc=0.0, capacity_kwh=200.0),
        flat_demands(100),
        ReserveGuardPolicy().provider(),
    ).band_summary(FALL_BACK, TZ)

    assert folded["night"]["discharged_kwh"] > normal["night"]["discharged_kwh"]


# -- the reduced form -------------------------------------------------------


def test_the_reported_form_publishes_no_per_interval_array() -> None:
    """Every list anywhere in diagnostics is held to sixteen entries."""
    trajectory = simulate(
        state_for(80.0), flat_demands(96), ReserveGuardPolicy().provider()
    )
    payload = trajectory.as_dict(NORMAL, TZ)

    def oversized(value, path=""):
        found = []
        if isinstance(value, dict):
            for key, item in value.items():
                found += oversized(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            if len(value) > 16:
                found.append((path, len(value)))
            for index, item in enumerate(value):
                found += oversized(item, f"{path}[{index}]")
        return found

    assert oversized(payload) == []
    assert "outcomes" not in payload
    assert "demands" not in payload


def test_the_binding_interval_count_is_complete_even_when_the_list_is_capped() -> None:
    """A silent cap reads as full coverage when it is not.

    Driven by a provider that keeps asking for full power after the floor is
    reached, because the shipped policy deliberately stops asking -- it consults
    the reserve, so under it the clamp binds exactly once and the cap is never
    exercised. That agreement between policy and clamp is the desirable case;
    this is the undesirable one a future policy could produce, and the reporting
    has to survive it.
    """
    trajectory = simulate(
        state_for(80.0),
        flat_demands(96),
        constant_provider(BatteryRequest.discharge(5.0)),
    )
    payload = trajectory.as_dict()

    assert payload["binding_intervals_total"] > 16
    assert len(payload["binding_intervals"]) == 16
    assert payload["binding_intervals_total"] == len(trajectory.binding_intervals)


def test_the_shipped_policy_and_the_clamp_agree_about_the_floor() -> None:
    """The policy stops asking at the reserve, so the clamp barely has to bind.

    Worth asserting in its own right: a policy that had to be clamped on ninety
    intervals out of ninety-six would be a policy that did not understand its own
    constraint, and the two agreeing is what makes the recommendation legible.
    """
    trajectory = simulate(
        state_for(80.0), flat_demands(96), ReserveGuardPolicy().provider()
    )

    assert len(trajectory.binding_intervals) == 1
    assert trajectory.end_soc_percent == pytest.approx(20.0)


def test_the_reported_form_says_it_is_a_battery_only_counterfactual() -> None:
    """A grid figure with no production term must say so where it appears."""
    trajectory = simulate(
        state_for(80.0), flat_demands(96), ReserveGuardPolicy().provider()
    )
    basis = trajectory.as_dict()["basis"]

    assert "photovoltaic" in basis
    assert "counterfactual" in basis


# -- what-if ----------------------------------------------------------------


def test_the_candidate_is_compared_against_leaving_the_battery_alone() -> None:
    """Hold is the reference, and the difference is what Phase 8 will price."""
    state = state_for(80.0)
    demands = flat_demands(96)
    reference = simulate(state, demands, HoldPolicy().provider())
    candidate = simulate(state, demands, ReserveGuardPolicy().provider())

    result = compare(reference, candidate)

    assert result["reference_grid_import_kwh"] == pytest.approx(12.0)
    avoided = 6.0 * math.sqrt(0.9)
    assert result["grid_import_avoided_kwh"] == pytest.approx(avoided, abs=0.01)
    assert result["candidate_discharged_kwh"] == pytest.approx(avoided, abs=0.01)
    assert result["reference_end_soc_percent"] == 80.0
    assert result["candidate_end_soc_percent"] == pytest.approx(20.0)
    assert result["comparable_intervals"] == 96


def test_comparing_a_trajectory_with_itself_shows_no_difference() -> None:
    """The degenerate case, which a sign error would get wrong."""
    state = state_for(80.0)
    demands = flat_demands(96)
    hold = simulate(state, demands, HoldPolicy().provider())

    result = compare(hold, hold)

    assert result["grid_import_avoided_kwh"] == 0.0
    assert result["candidate_discharged_kwh"] == 0.0


# -- policies ---------------------------------------------------------------


def test_no_shipped_policy_ever_asks_to_charge() -> None:
    """The phase boundary, asserted over the real set of shipped policies.

    Every reason to put energy into a battery needs information this phase does
    not have: surplus production is Phase 5, a cheap half-hour Phase 6, a storm
    Phase 7, an arbitrage spread Phase 8. The charge path exists and is clamped
    and simulated so those phases have somewhere to land -- but nothing here
    emits one.
    """
    assert {policy.identity for policy in SHIPPED_POLICIES} == {
        POLICY_HOLD,
        POLICY_RESERVE_GUARD,
    }

    for policy_class in SHIPPED_POLICIES:
        for soc in (0.0, 5.0, 19.9, 20.0, 20.1, 50.0, 99.0, 100.0):
            for minimum in (0.0, 20.0, 99.0):
                state = state_for(soc, min_soc=minimum)
                assert not emits_charge(policy_class(), state), (policy_class, soc)


def test_the_default_policy_is_the_reserve_guard() -> None:
    """The published recommendation comes from one named policy."""
    assert DEFAULT_POLICY is ReserveGuardPolicy
    assert DEFAULT_POLICY.identity == POLICY_RESERVE_GUARD


@pytest.mark.parametrize(
    ("soc", "minimum", "baseline", "reason"),
    [
        (80.0, 20.0, FLAT, REASON_COVER_FORECAST_LOAD),
        (20.0, 20.0, FLAT, REASON_AT_RESERVE),
        (15.0, 20.0, FLAT, REASON_BELOW_RESERVE),
        (80.0, 20.0, None, REASON_FORECAST_UNAVAILABLE),
    ],
)
def test_the_reserve_guard_explains_itself(
    soc: float, minimum: float, baseline: float | None, reason: str
) -> None:
    """A recommendation nobody can explain is not usable by anyone."""
    state = state_for(soc, min_soc=minimum)
    proposal = ReserveGuardPolicy().propose(
        state, IntervalDemand(index=0, baseline_kwh=baseline)
    )

    assert proposal.reason == reason


def test_the_hold_policy_always_says_why() -> None:
    """Even doing nothing carries its reason."""
    proposal = HoldPolicy().propose(
        state_for(80.0), IntervalDemand(index=0, baseline_kwh=FLAT)
    )

    assert proposal.request.is_idle
    assert proposal.reason == REASON_POLICY_HOLD


def test_the_policy_asks_for_the_load_and_leaves_the_limits_to_the_clamp() -> None:
    """A policy that clamped its own request would be a second copy of the rules."""
    state = state_for(80.0, max_discharge_kw=1.0)
    proposal = ReserveGuardPolicy().propose(
        state, IntervalDemand(index=0, baseline_kwh=2.5)
    )

    # 2.5 kWh over a quarter-hour is 10 kW, far above the 1 kW limit -- and the
    # policy asks for it anyway.
    assert proposal.request.power_kw == pytest.approx(10.0)
    outcome = apply_request(state, proposal.request)
    assert outcome.average_power_kw == pytest.approx(1.0)


def test_the_policy_reads_the_effective_reserve_and_the_clamp_the_configured_one() -> (
    None
):
    """The two floors, doing their separate jobs at the same moment.

    With the effective reserve raised to 40 % the policy declines to discharge at
    all, while the clamp -- had something asked -- would still have allowed the
    energy down to the configured 20 %. That is exactly the flexibility Phase 8
    needs and the safety Phase 3 promises.
    """
    from custom_components.alpha_ems_manager.battery import BatteryReserve, BatteryState
    from custom_components.alpha_ems_manager.const import RESERVE_DYNAMIC

    raised = BatteryReserve(
        configured_min_soc_percent=20.0,
        effective_min_soc_percent=40.0,
        source=RESERVE_DYNAMIC,
    )
    state = BatteryState(energy_kwh=3.5, limits=limits_for(), reserve=raised)

    proposal = ReserveGuardPolicy().propose(
        state, IntervalDemand(index=0, baseline_kwh=FLAT)
    )
    assert proposal.reason == REASON_AT_RESERVE
    assert proposal.request.is_idle

    # The clamp would have permitted it: 1.5 kWh sits above the configured floor.
    assert state.usable_energy_kwh == pytest.approx(1.5)
    assert apply_request(state, BatteryRequest.discharge(4.0)).discharge_ac_kwh > 0.0


# -- determinism ------------------------------------------------------------


def test_the_same_inputs_produce_an_equal_trajectory() -> None:
    """Frozen records, compared by value."""
    state = state_for(80.0)
    demands = flat_demands(96)

    first = simulate(state, demands, ReserveGuardPolicy().provider())
    second = simulate(state, demands, ReserveGuardPolicy().provider())

    assert first == second
    assert first.as_dict(NORMAL, TZ) == second.as_dict(NORMAL, TZ)


def test_walking_the_intervals_by_hand_matches_the_simulator() -> None:
    """The simulator adds no arithmetic of its own beyond stepping."""
    state = state_for(80.0)
    demands = flat_demands(20)
    trajectory = simulate(state, demands, ReserveGuardPolicy().provider())

    current = state
    policy = ReserveGuardPolicy()
    for demand in demands:
        outcome = apply_request(current, policy.propose(current, demand).request)
        current = advance(current, outcome)

    assert trajectory.end_energy_kwh == pytest.approx(current.energy_kwh, abs=1e-12)


def test_a_multi_day_horizon_is_just_a_longer_list() -> None:
    """Today plus tomorrow, which is what the coordinator actually simulates.

    Worth pinning because Phase 10 needs it and because a simulator that assumed
    one civil day would break here rather than in two years' time.
    """
    state = state_for(100.0, min_soc=0.0, capacity_kwh=200.0)
    horizon = flat_demands(96 + 96)
    trajectory = simulate(state, horizon, ReserveGuardPolicy().provider())

    assert trajectory.intervals == 192
    assert trajectory.discharged_ac_kwh == pytest.approx(192 * FLAT)


def test_a_day_length_change_across_the_horizon_is_handled() -> None:
    """The day before a transition is 96 intervals and the day itself is not."""
    today = FALL_BACK - timedelta(days=1)
    assert expected_quarters_for(today, TZ) == 96
    assert expected_quarters_for(FALL_BACK, TZ) == 100

    state = state_for(100.0, min_soc=0.0, capacity_kwh=200.0)
    horizon = flat_demands(96 + 100)
    trajectory = simulate(state, horizon, ReserveGuardPolicy().provider())

    assert trajectory.intervals == 196
    assert trajectory.discharged_ac_kwh == pytest.approx(196 * FLAT)


def test_an_empty_horizon_reports_nothing_rather_than_zero() -> None:
    """No intervals is not a day in which nothing happened."""
    state = state_for(80.0)
    trajectory = simulate(state, (), ReserveGuardPolicy().provider())

    assert trajectory.intervals == 0
    assert trajectory.intervals_with_demand == 0
    assert trajectory.end_soc_percent == 80.0
    assert trajectory.minimum_soc_index is None


def test_the_horizon_helper_clamps_a_start_index_into_the_day() -> None:
    """An out-of-range index must not reach the fill mask."""
    assert (
        demands_from_forecast([FLAT] * 96, [False] * 96, start_index=-5)[0].index == 0
    )
    assert demands_from_forecast([FLAT] * 96, [False] * 96, start_index=500) == ()
    assert len(demands_from_forecast([FLAT] * 96, [False] * 96, start_index=90)) == 6
    # A short mask must not raise, and must not read as filled.
    demands = demands_from_forecast([FLAT] * 96, [True, True])
    assert demands[0].filled is True
    assert demands[95].filled is False


def test_an_interval_demand_converts_to_an_average_power() -> None:
    """0.125 kWh over a quarter-hour is 0.5 kW, and no forecast is no power."""
    assert IntervalDemand(index=0, baseline_kwh=FLAT).power_kw == pytest.approx(0.5)
    assert IntervalDemand(index=0, baseline_kwh=None).power_kw is None
    assert IntervalDemand(index=0, baseline_kwh=FLAT).power_kw == FLAT / INTERVAL_HOURS
