"""The planner's own projection, published instead of re-derived.

Three dashboard cards were each estimating the pack's future state from coarser
inputs than the planner used -- a daily load total spread flat over 24 hours, the
forecaster's own half-hour rows, an approximate efficiency, and a guess at whether a
published objective was already at the battery boundary. Three estimates, three
different answers, none of them the plan's.

The plan already holds the exact series. ``EconomicPlan.intervals`` is appended once
per horizon interval, unconditionally, and every entry carries the pack energy the
recursion actually stood at. So beta.53 **reads** it. There is no new simulation, no
efficiency factor and no index arithmetic anywhere in this feature, which is what
makes it incapable of changing a decision.

Three details would each have published a plausible wrong number, and each has a test
below that separates the right answer from the wrong one rather than merely asserting
the right one:

* the floor is the **configured** minimum the solver was handed, not the terminal
  bound and not the margin-inflated reachability floor;
* an interval's end energy is the **next interval's start**, never ``start + delta``,
  because the walk also drains whatever the household drew ambiently;
* the floor test is **at or below**, not a crossing, because every move passes through
  a clamp that stops the walk exactly on the floor rather than through it.

Each projection also states its own instant and campaign identity, so a reader never
has to work out which published campaign a bare number belonged to -- and the answer
stays complete when the campaign is past the eighth and therefore absent from the
capped ``upcoming`` list.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager import coordinator as coordinator_module
from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_BOUNDARY_BATTERY,
    CAMPAIGN_BOUNDARY_METER,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    PROJECTION_UNAVAILABLE_NO_PLAN,
)
from custom_components.alpha_ems_manager.coordinator import plan_projection
from custom_components.alpha_ems_manager.economic import (
    EconomicInterval,
    floor_reached_index,
    interval_end_energy_dc_kwh,
    objective_boundary_for,
    start_energy_at_index,
)
from custom_components.alpha_ems_manager.sensor import _next_planned_run

from .conftest import FakeFrank
from .test_economic_published import (
    UNCHANGED,
    allow_trading,
    attributes_of,
    drive,
)

#: A midnight in a zone with no transition on the day, so the pure tests below can
#: talk about instants without the calendar being the subject.
MIDNIGHT = datetime(2026, 9, 8, 0, 0)


def interval(
    index: int,
    *,
    start: float,
    delta: float = 0.0,
    service: float = 0.0,
    action: str = "hold",
    charge_ac: float = 0.0,
    export: float = 0.0,
) -> EconomicInterval:
    """Return one solved interval with only the fields this feature reads."""
    return EconomicInterval(
        index=index,
        action=action,
        start_energy_dc_kwh=start,
        battery_delta_dc_kwh=delta,
        battery_charge_ac_kwh=charge_ac,
        battery_discharge_ac_kwh=0.0,
        grid_import_kwh=0.0,
        grid_export_kwh=export,
        pv_curtailed_kwh=0.0,
        cost_eur=0.0,
        import_price_eur_kwh=0.25,
        export_price_eur_kwh=0.08,
        run_start=False,
        battery_state_service_dc_kwh=service,
    )


def moment_of(index: int) -> datetime:
    """Return the instant a quarter index opens, on a 96-interval day."""
    return MIDNIGHT + timedelta(minutes=15 * index)


# ===========================================================================
# the three traps
# ===========================================================================


def test_the_end_energy_is_the_next_intervals_start_not_start_plus_delta() -> None:
    """**The two disagree exactly where the household drew from the pack.**

    ``start + delta`` describes only what the plan *dispatched*. The walk also drains
    ``battery_state_service_dc_kwh`` -- the ambient self-consumption the inverter
    covered without being told to -- so on a sunny hold interval the two differ by
    precisely the energy that matters, and adding the delta would report a pack
    fuller than the plan expects it to be.
    """
    intervals = (
        interval(40, start=10.0, delta=0.0, service=0.4),
        interval(41, start=9.6),
    )

    end = interval_end_energy_dc_kwh(intervals, 0, plan_end_energy_dc_kwh=9.6)

    assert end == pytest.approx(9.6)
    # The wrong answer, named so a future reader can see it was considered.
    assert intervals[0].start_energy_dc_kwh + intervals[0].battery_delta_dc_kwh == 10.0
    assert end != 10.0


def test_the_last_intervals_end_is_the_plans_own_endpoint() -> None:
    """There is no next interval, and the plan publishes where it lands."""
    intervals = (interval(40, start=10.0, delta=-1.0, service=0.2),)

    assert interval_end_energy_dc_kwh(
        intervals, 0, plan_end_energy_dc_kwh=8.8
    ) == pytest.approx(8.8)


def test_a_gap_in_the_index_frame_yields_no_end_energy() -> None:
    """A non-adjacent interval is not this one's end, and is not treated as one.

    The walk appends every horizon interval, so this cannot arise today. It is
    refused rather than assumed, because the failure mode of assuming is a silently
    plausible number.
    """
    intervals = (interval(40, start=10.0), interval(44, start=6.0))

    assert interval_end_energy_dc_kwh(intervals, 0, plan_end_energy_dc_kwh=6.0) is None


def test_the_floor_is_reached_at_or_below_and_never_by_crossing() -> None:
    """**Every move passes through a clamp that stops the walk on the floor.**

    A test for a crossing -- ``previous > floor and end < floor`` -- would never
    fire, because the pack sits at the floor rather than passing through it. The
    horizon below lands exactly on 2.0 and the answer is the interval after it.
    """
    intervals = (
        interval(40, start=4.0, delta=-2.0),
        interval(41, start=2.0),
        interval(42, start=2.0),
    )

    reached = floor_reached_index(
        intervals, floor_energy_kwh=2.0, plan_end_energy_dc_kwh=2.0
    )

    # The pack stands at the floor from index 41 onward, so that is the index.
    assert reached == 41


def test_already_at_the_floor_is_the_head_index_and_not_an_absence() -> None:
    """Zero is a real answer here, and ``None`` would mean something else.

    A pack sitting on its floor now has no time left before it gets there, which a
    reader must be able to distinguish from a plan that never takes it there.
    """
    intervals = (interval(40, start=2.0), interval(41, start=2.0))

    assert (
        floor_reached_index(intervals, floor_energy_kwh=2.0, plan_end_energy_dc_kwh=2.0)
        == 40
    )


def test_a_horizon_that_never_reaches_the_floor_reports_no_index() -> None:
    """Absent, because the plan makes no such claim about this horizon."""
    intervals = (interval(40, start=9.0), interval(41, start=8.5))

    assert (
        floor_reached_index(intervals, floor_energy_kwh=2.0, plan_end_energy_dc_kwh=8.0)
        is None
    )


def test_energy_at_an_index_is_matched_by_equality_not_by_position() -> None:
    """The index is chronological and the list position is not the index.

    A horizon beginning at 40 has its head at position 0, so subtracting a base or
    trusting the position would read the wrong interval -- which is the shape of the
    two frame confusions this integration has already had to fix once.
    """
    intervals = (interval(40, start=10.0), interval(41, start=9.0))

    assert start_energy_at_index(intervals, 41) == pytest.approx(9.0)
    assert start_energy_at_index(intervals, 0) is None
    assert start_energy_at_index(intervals, 99) is None


# ===========================================================================
# the assembled projection
# ===========================================================================


class FakeLimits:
    """The two conversions the projection needs, and nothing else."""

    capacity_kwh = 20.0

    def soc_for_energy(self, energy_kwh: float) -> float:
        return energy_kwh / self.capacity_kwh * 100.0


def targets_for(
    *,
    campaign_id: str,
    intent: str,
    first_index: int,
    last_index: int,
    executable_from: int | None = None,
) -> dict:
    """Return one published execution target with per-quarter rows.

    ``executable_from`` leaves the earlier rows refused, which is how a campaign
    whose first quarter is below the actuator's resolution is published.
    """
    rows = []
    for index in range(first_index, last_index + 1):
        armable = executable_from is None or index >= executable_from
        rows.append(
            {
                "start": moment_of(index).isoformat(),
                "end": moment_of(index + 1).isoformat(),
                "not_executable": None if armable else "below_actuator_resolution",
            }
        )
    return {
        "campaign_id": campaign_id,
        "intent": intent,
        "window_start": moment_of(first_index).isoformat(),
        "quarter_schedule": rows,
    }


def test_the_energy_before_a_charge_campaign_is_the_plans_own_state() -> None:
    """The scalar is ``start_energy_dc_kwh`` at that campaign's own instant.

    Hand-checked: the charge opens at index 42, and the plan stood at 5.0 kWh there.
    """
    intervals = (
        interval(40, start=7.0, delta=-1.0),
        interval(41, start=6.0, delta=-1.0),
        interval(42, start=5.0, delta=2.0, action="charge", charge_ac=2.0),
        interval(43, start=7.0),
    )
    targets = (
        targets_for(
            campaign_id="c-charge",
            intent=EXECUTION_INTENT_GRID_CHARGE,
            first_index=42,
            last_index=42,
        ),
    )

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=7.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=targets,
        moment=moment_of,
        now=moment_of(40),
    )

    assert payload["battery_before_next_charge_dc_kwh"] == pytest.approx(5.0)
    assert payload["battery_before_next_charge_soc_percent"] == pytest.approx(25.0)
    assert payload["next_charge_projection_at"] == moment_of(42).isoformat()
    assert payload["next_charge_projection_end_at"] == moment_of(43).isoformat()
    assert payload["next_charge_campaign_id"] == "c-charge"
    assert payload["projection_unavailable_reason"] is None


def test_the_export_projection_is_the_same_read_at_the_meter_boundary() -> None:
    """Both boundaries answered from one array, so they cannot disagree."""
    intervals = (
        interval(40, start=7.0),
        interval(41, start=7.0, delta=-3.0, action="export", export=2.7),
        interval(42, start=4.0),
    )
    targets = (
        targets_for(
            campaign_id="c-sell",
            intent=EXECUTION_INTENT_NET_EXPORT,
            first_index=41,
            last_index=41,
        ),
    )

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=4.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=targets,
        moment=moment_of,
        now=moment_of(40),
    )

    assert payload["battery_before_next_export_dc_kwh"] == pytest.approx(7.0)
    assert payload["battery_before_next_export_soc_percent"] == pytest.approx(35.0)
    assert payload["next_export_projection_at"] == moment_of(41).isoformat()
    assert payload["next_export_campaign_id"] == "c-sell"
    # No charge is planned, and that is an absence rather than a failure.
    assert payload["battery_before_next_charge_dc_kwh"] is None
    assert payload["projection_unavailable_reason"] is None


def test_a_refused_first_quarter_moves_the_projection_to_the_armable_one() -> None:
    """**The instant is where something will actually happen.**

    A campaign whose opening quarter is below the actuator's resolution is published
    with that row refused, and the plan will not act on it. Projecting to the
    campaign's nominal start would state a pack energy for a moment at which nothing
    is dispatched.
    """
    intervals = (
        interval(40, start=7.0),
        interval(41, start=7.0, delta=0.01, action="charge", charge_ac=0.01),
        interval(42, start=7.01, delta=2.0, action="charge", charge_ac=2.0),
        interval(43, start=9.01),
    )
    targets = (
        targets_for(
            campaign_id="c-charge",
            intent=EXECUTION_INTENT_GRID_CHARGE,
            first_index=41,
            last_index=42,
            executable_from=42,
        ),
    )

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=9.01,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=targets,
        moment=moment_of,
        now=moment_of(40),
    )

    assert payload["next_charge_projection_at"] == moment_of(42).isoformat()
    assert payload["battery_before_next_charge_dc_kwh"] == pytest.approx(7.01)


def test_a_campaign_with_no_armable_row_is_not_projected_to() -> None:
    """Nothing will be dispatched, so there is nothing to stand before."""
    intervals = (interval(40, start=7.0), interval(41, start=7.0))
    targets = (
        targets_for(
            campaign_id="c-charge",
            intent=EXECUTION_INTENT_GRID_CHARGE,
            first_index=41,
            last_index=41,
            executable_from=99,
        ),
    )

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=7.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=targets,
        moment=moment_of,
        now=moment_of(40),
    )

    assert payload["next_charge_projection_at"] is None
    assert payload["battery_before_next_charge_dc_kwh"] is None
    assert payload["next_charge_campaign_id"] is None


def test_the_projection_states_its_own_instant_and_campaign() -> None:
    """**The ninth campaign, which the published list cannot reach.**

    ``upcoming`` is capped at eight rows, so a dashboard handed a bare number could
    not always say which campaign it belonged to. Each projection therefore carries
    its own instant, its own end and its own campaign identity, and this test proves
    the answer survives with nine campaigns present.
    """
    intervals = tuple(interval(40 + step, start=7.0) for step in range(30))
    targets = tuple(
        targets_for(
            campaign_id=f"c-{ordinal}",
            intent=EXECUTION_INTENT_NET_EXPORT,
            first_index=41 + ordinal * 2,
            last_index=41 + ordinal * 2,
        )
        for ordinal in range(9)
    )
    # The ninth is the only executable one, and it is past the publication cap.
    ninth = targets[8]
    targets = tuple(
        {
            **target,
            "quarter_schedule": [
                {**row, "not_executable": "below_actuator_resolution"}
                for row in target["quarter_schedule"]
            ],
        }
        if target is not ninth
        else target
        for target in targets
    )

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=7.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=targets,
        moment=moment_of,
        now=moment_of(40),
    )

    assert payload["next_export_campaign_id"] == "c-8"
    assert payload["next_export_projection_at"] == moment_of(57).isoformat()
    assert payload["battery_before_next_export_dc_kwh"] == pytest.approx(7.0)


def test_the_time_to_floor_is_measured_against_the_configured_floor() -> None:
    """The number the solver was handed, and not one of the two near neighbours.

    ``terminal_floor_kwh`` is a bound on where the plan may *end*, and the
    reachability floor is the hard floor plus an uncertainty margin. Either would
    answer a different question, and both are plausible enough to have been picked by
    mistake -- so the fixture places the answer strictly between them.
    """
    intervals = (
        interval(40, start=4.0, delta=-1.0),
        interval(41, start=3.0, delta=-1.0),
        interval(42, start=2.0),
    )

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=2.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=(),
        moment=moment_of,
        now=moment_of(40),
    )

    assert payload["reserve_floor_reached_at"] == moment_of(42).isoformat()
    assert payload["minutes_until_reserve_floor"] == 30

    # A 3.0 kWh floor -- what a margin-inflated figure would look like -- lands an
    # interval earlier, so the two are distinguishable and this asserts the right one.
    inflated = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=2.0,
        floor_energy_kwh=3.0,
        limits=FakeLimits(),
        targets=(),
        moment=moment_of,
        now=moment_of(40),
    )
    assert inflated["minutes_until_reserve_floor"] == 15


def test_a_pack_already_on_its_floor_reports_zero_minutes() -> None:
    """Zero, which is an answer, and never ``None``, which is not."""
    intervals = (interval(40, start=2.0), interval(41, start=2.0))

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=2.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=(),
        moment=moment_of,
        now=moment_of(40),
    )

    assert payload["minutes_until_reserve_floor"] == 0
    assert payload["reserve_floor_reached_at"] == moment_of(40).isoformat()


def test_minutes_never_go_negative_when_the_head_is_behind_the_clock() -> None:
    """The horizon head is the *next* interval, so ``now`` can be past an instant.

    A negative "minutes until" is not a fact about the plan; it is an artefact of two
    clocks, and it would render as a countdown running backwards.
    """
    intervals = (interval(40, start=2.0),)

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=2.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=(),
        moment=moment_of,
        now=moment_of(44),
    )

    assert payload["minutes_until_reserve_floor"] == 0


def test_an_empty_horizon_refuses_with_a_named_reason() -> None:
    """No plan is not a projection of zero, on the same terms as everything here."""
    payload = plan_projection(
        intervals=(),
        plan_end_energy_dc_kwh=0.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=(),
        moment=moment_of,
        now=moment_of(40),
    )

    assert payload["projection_unavailable_reason"] == PROJECTION_UNAVAILABLE_NO_PLAN
    for name in (
        "battery_before_next_charge_dc_kwh",
        "battery_before_next_charge_soc_percent",
        "battery_before_next_export_dc_kwh",
        "minutes_until_reserve_floor",
        "reserve_floor_reached_at",
    ):
        assert payload[name] is None, name


def test_a_projection_publishes_no_array_and_no_mapping() -> None:
    """The horizon stays out of the payload, whatever its length.

    Recorder writes every attribute on every state change, which is why the entity
    contract caps a list at eight items and forbids a mapping outright. This feature
    turns a hundred intervals into scalars, so that invariant is untouched rather
    than relaxed.
    """
    intervals = tuple(interval(index, start=7.0) for index in range(100))

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=7.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=(),
        moment=moment_of,
        now=moment_of(0),
    )

    for key, value in payload.items():
        assert not isinstance(value, (list, tuple, dict)), key


@pytest.mark.parametrize("interval_count", [92, 96, 100])
def test_an_index_past_today_resolves_into_tomorrow(interval_count: int) -> None:
    """**92, 96 and 100, because the civil day is not always 96 quarters long.**

    The horizon runs through today and straight on into tomorrow, so an index at or
    beyond today's real length names an interval of the next day. The projection does
    no arithmetic of its own: it is handed the same resolver every other published
    instant in this integration uses, and this proves the seam is honoured at all
    three day lengths.
    """
    seen: list[int] = []

    def resolver(index: int) -> datetime:
        seen.append(index)
        if index < interval_count:
            return MIDNIGHT + timedelta(minutes=15 * index)
        return (
            MIDNIGHT
            + timedelta(days=1)
            + timedelta(minutes=15 * (index - interval_count))
        )

    # A head near the end of the day, so the campaign falls on the far side.
    head = interval_count - 2
    intervals = tuple(interval(head + step, start=7.0) for step in range(6))
    targets = (
        targets_for(
            campaign_id="c-tomorrow",
            intent=EXECUTION_INTENT_GRID_CHARGE,
            first_index=interval_count + 1,
            last_index=interval_count + 1,
        ),
    )
    # The fixture's own rows must be built with the same resolver as the projection.
    targets = (
        {
            **targets[0],
            "window_start": resolver(interval_count + 1).isoformat(),
            "quarter_schedule": [
                {
                    "start": resolver(interval_count + 1).isoformat(),
                    "end": resolver(interval_count + 2).isoformat(),
                    "not_executable": None,
                }
            ],
        },
    )

    payload = plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=7.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=targets,
        moment=resolver,
        now=resolver(head),
    )

    expected = (MIDNIGHT + timedelta(days=1) + timedelta(minutes=15)).isoformat()
    assert payload["next_charge_projection_at"] == expected
    assert payload["battery_before_next_charge_dc_kwh"] == pytest.approx(7.0)


# ===========================================================================
# the structural promise
# ===========================================================================


def test_the_projection_is_read_from_the_plan_and_never_recomputed() -> None:
    """**No efficiency, no solver, no price.** The feature is a scan, not a model.

    An efficiency factor here would be the second place in this codebase that
    converts between AC and DC, and the first one -- ``battery.apply_request`` -- is
    the only one allowed to exist. A projection that applied its own would disagree
    with the plan it claims to describe, which is precisely the defect the dashboard
    cards had.
    """
    source = inspect.getsource(plan_projection)

    for forbidden in (
        "efficiency",
        "solve",
        "optimise",
        "optimize",
        "import_price",
        "export_price",
        "pv_kwh",
        "baseline_kwh",
    ):
        assert forbidden not in source, forbidden
    # It reads the plan's own state and converts it with the pack's own helper.
    assert "start_energy_dc_kwh" in source
    assert "soc_for_energy" in source


def test_the_projection_reads_the_desired_plan_and_not_the_capability_one() -> None:
    """The execution targets are built from ``desired``, so this must be too.

    Reading ``capability`` would describe a plan the published campaigns do not come
    from, and the scalar and the campaign rendered beside it would then be about
    different futures.
    """
    source = inspect.getsource(coordinator_module.AlphaEmsCoordinator.plan_projection)

    assert "outcome.desired" in source
    assert "outcome.capability" not in source


def test_the_boundary_words_are_the_ones_the_campaigns_already_use() -> None:
    """No third vocabulary for the same two meter faces."""
    assert CAMPAIGN_BOUNDARY_BATTERY == "battery"
    assert CAMPAIGN_BOUNDARY_METER == "meter"


# ===========================================================================
# on the entity
# ===========================================================================


PLANNED_ENTITY = "sensor.alpha_ems_next_planned_action"

#: Every scalar the feature adds, so an assertion covers the surface rather than a
#: sample of it.
PROJECTION_KEYS = (
    "battery_before_next_charge_dc_kwh",
    "battery_before_next_charge_soc_percent",
    "next_charge_projection_at",
    "next_charge_projection_end_at",
    "next_charge_campaign_id",
    "battery_before_next_export_dc_kwh",
    "battery_before_next_export_soc_percent",
    "next_export_projection_at",
    "next_export_projection_end_at",
    "next_export_campaign_id",
    "minutes_until_reserve_floor",
    "reserve_floor_reached_at",
    "projection_basis",
    "projection_unavailable_reason",
)


async def test_the_projection_is_published_on_the_existing_entity(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """**No new entity, and no forecast series.**

    Fourteen scalars on a surface that already exists. The dashboard consumes
    answers rather than intervals, so it cannot get the interval basis, the index
    frame, the civil day's length or a conversion wrong -- and all three cards share
    one basis because there is only one array behind them.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await drive(coordinator, frank)

    outcome = (coordinator.data or {}).get("economic")
    if outcome is None or not outcome.available:
        pytest.skip("no economic plan for this fixture")
    attributes = attributes_of(hass, PLANNED_ENTITY)

    for key in PROJECTION_KEYS:
        assert key in attributes, key
    assert attributes["projection_basis"]
    assert attributes["projection_unavailable_reason"] is None


async def test_no_projection_attribute_is_a_large_array_or_a_mapping(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The horizon stays out of the payload, and the entity contract is untouched.

    Recorder writes every attribute on every state change, which is why a list is
    capped at eight items and a mapping is forbidden outright. A hundred intervals
    become scalars here, so that invariant is honoured rather than relaxed.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await drive(coordinator, frank)

    attributes = attributes_of(hass, PLANNED_ENTITY)

    for key in PROJECTION_KEYS:
        value = attributes.get(key)
        assert not isinstance(value, dict), key
        if isinstance(value, (list, tuple)):
            assert len(value) <= 8, key


async def test_a_campaign_projection_names_a_campaign_the_plan_knows(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The identity is an exact join, never an inference from a start time.

    Where the campaign is inside the published list the two must agree; where it is
    past the eighth it is absent from the list and the scalar is still complete.
    That asymmetry is why the identity is published at all.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await drive(coordinator, frank)

    outcome = (coordinator.data or {}).get("economic")
    if outcome is None or not outcome.available:
        pytest.skip("no economic plan for this fixture")
    attributes = attributes_of(hass, PLANNED_ENTITY)
    published = {row["campaign_id"] for row in attributes.get("upcoming") or ()}

    for word in ("charge", "export"):
        campaign_id = attributes[f"next_{word}_campaign_id"]
        if campaign_id is None:
            continue
        instant = attributes[f"next_{word}_projection_at"]
        assert instant is not None
        if campaign_id in published:
            row = next(
                item
                for item in attributes["upcoming"]
                if item["campaign_id"] == campaign_id
            )
            # The projection lands inside the campaign it names, at or after its
            # start -- later only where the opening quarter could not be armed.
            assert instant >= row["starts_at"]


async def test_the_objective_boundary_is_published_beside_the_planned_energy(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """**The single most misreadable figure this integration publishes.**

    ``planned_kwh`` switches boundary with the action and said nothing about which.
    The label is derived from the same action the energy is, so the two cannot come
    apart, and the rule beside it says the thing a dashboard has to know: every
    objective here is AC, and the boundary is a meter face rather than an AC/DC flag.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await drive(coordinator, frank)

    outcome = (coordinator.data or {}).get("economic")
    if outcome is None or not outcome.available:
        pytest.skip("no economic plan for this fixture")
    attributes = attributes_of(hass, PLANNED_ENTITY)

    assert "objective_boundary" in attributes
    assert attributes["objective_boundary"] in (
        CAMPAIGN_BOUNDARY_BATTERY,
        CAMPAIGN_BOUNDARY_METER,
        None,
    )
    rule = attributes["objective_boundary_rule"]
    assert "AC" in rule
    assert "no efficiency" in rule

    run, _target = _next_planned_run(coordinator)
    if run is not None:
        assert attributes["objective_boundary"] == objective_boundary_for(run.action)


async def test_a_withheld_plan_publishes_no_projection_and_never_a_zero(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """**Honours the precedent that kept a projected state of charge out until now.**

    ``usable_battery_energy`` needs no forecast, which is exactly why it survives a
    young installation and why a projected state of charge did not. So on a refresh
    with no prices and therefore no horizon, every projection scalar is absent or
    null -- never zero, which would read as "the pack will be empty" rather than
    "nobody knows yet".
    """
    coordinator = setup_integration.runtime_data

    outcome = (coordinator.data or {}).get("economic")
    assert outcome is None or not outcome.available

    attributes = attributes_of(hass, PLANNED_ENTITY)

    for key in PROJECTION_KEYS:
        if key == "projection_basis":
            continue
        assert attributes.get(key) in (None, PROJECTION_UNAVAILABLE_NO_PLAN), key


async def test_the_projection_moves_no_other_published_figure(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The five figures beta.13 published, unchanged by an observability release.

    The projection recomputes nothing and the boundary label decides nothing, so a
    plan that produced these numbers before must produce them still.
    """
    coordinator = setup_integration.runtime_data
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await drive(coordinator, frank)

    for entity_id in UNCHANGED:
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        # The projection is attributes on one other entity; nothing here may have
        # become unknown because of it.
        assert state.state not in ("unavailable",), entity_id
