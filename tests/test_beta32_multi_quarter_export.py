"""The release's real risk surface: an export campaign spanning several quarters.

**Nothing covered this before beta.32, and beta.32 is what creates it.** Through
beta.31 an export run was one quarter wide, because ``runs_from`` split on the
action label and the label flips whenever house load crosses the discharge bucket --
so a physical sale that ran for thirteen intervals was published as several
single-interval runs and executed as several unrelated windows. Grouping on the DP's
own run state makes an export campaign genuinely multi-quarter for the first time,
and everything that has ever gone wrong at a quarter boundary can now go wrong
inside one sale.

Five properties, each a defect this project has already had once at a boundary:

1. **frozen-quarter immutability across two boundaries** -- the beta.27 authority
   rule, exercised where it has never been exercised;
2. **handover without stopping the dispatch** -- ``_async_end_row(stop=False)``,
   which is the difference between one continuous sale and three arms;
3. **the dead-man re-arm across a boundary** -- a run continues only because each
   refresh demonstrably advances the vendor timer;
4. **no energy catch-up** -- a quarter that fell short must not enlarge the next;
5. **the forward authorisation stays inert for an export** -- it bounds *grid
   purchases*, and an export buys nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_NET_EXPORT,
    MIN_EXECUTABLE_QUARTER_KWH,
    QUARTER_END_EXPIRED,
)
from custom_components.alpha_ems_manager.execution import (
    AdmittedPlan,
    CarriedQuarter,
    QuarterRow,
    carry_quarter,
    next_quarter_row,
    parse_target,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge
from .test_beta27_quarter_execution import install

pytestmark = pytest.mark.usefixtures("control_surface")

QUARTER = timedelta(minutes=15)


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def export_rows(hour: int, minute: int, count: int, *, meter: float) -> tuple:
    """Return ``count`` consecutive export rows, each aiming at ``meter`` kWh."""
    opens = local(NORMAL, hour, minute)
    return tuple(
        QuarterRow(
            start=opens + index * QUARTER,
            end=opens + (index + 1) * QUARTER,
            # The battery figure is a **ceiling** for an export, deliberately
            # larger than the meter objective: the house is fed first and only the
            # surplus crosses the meter.
            battery_kwh=meter + 0.4,
            grid_authorised_kwh=0.0,
            grid_export_target_kwh=meter,
            grid_export_caused_kwh=meter,
            desired_grid_kw=-meter / 0.25,
        )
        for index in range(count)
    )


def export_target(
    hour: int,
    minute: int,
    count: int,
    *,
    meter: float = 0.6,
    plan_id: str = "sell-1",
    campaign_id: str | None = "camp01",
) -> dict:
    """Return a published multi-quarter export target."""
    rows = export_rows(hour, minute, count, meter=meter)
    return {
        "plan_id": plan_id,
        "campaign_id": campaign_id,
        "campaign_end": rows[-1].end.isoformat(),
        "revision": 1,
        "intent": EXECUTION_INTENT_NET_EXPORT,
        "purpose": "export",
        "window_start": rows[0].start.isoformat(),
        "window_end": rows[-1].end.isoformat(),
        "issued_at": (rows[0].start - QUARTER).isoformat(),
        "stale_after": (rows[-1].end + QUARTER).isoformat(),
        "battery_target_kwh": sum(row.battery_kwh for row in rows),
        "grid_target_kwh": meter * count,
        "average_power_kw": -meter / 0.25,
        "first_power_kw": -meter / 0.25,
        "reserve_floor_kwh": 4.32,
        "quarter_schedule": [
            {
                "start": row.start.isoformat(),
                "end": row.end.isoformat(),
                "battery_kwh": row.battery_kwh,
                "grid_authorised_kwh": row.grid_authorised_kwh,
                "grid_export_target_kwh": row.grid_export_target_kwh,
                "grid_export_caused_kwh": row.grid_export_caused_kwh,
                "desired_grid_kw": row.desired_grid_kw,
                "executable": True,
            }
            for row in rows
        ],
    }


# ===========================================================================
# 1. the frozen quarter, across two boundaries
# ===========================================================================


def test_an_open_export_quarter_survives_two_boundaries_unchanged() -> None:
    """Three quarters, and the middle one is never re-derived.

    **The beta.27 authority rule, on the surface beta.32 creates.** After a
    quarter opens its allowance is never re-evaluated from a later publication,
    because no later publication can describe it: the horizon's head is the next
    boundary. The rule was written for charges, which ran multi-quarter from the
    start; an export has never been through it.
    """
    published = [export_target(19, 30, 3)]
    opens = local(NORMAL, 19, 30)

    # Admitted one refresh ahead, which is where a quarter is admitted from.
    first = carry_quarter(
        None,
        published,
        opens - timedelta(minutes=1),
        run=None,
        executable_intents=frozenset({EXECUTION_INTENT_NET_EXPORT}),
    )
    assert first is not None
    assert first.quarter_start == opens
    assert first.grid_export_target_kwh == pytest.approx(0.6)
    assert first.campaign_id == "camp01"

    # A publication that halves the objective arrives while the quarter is open.
    revised = export_target(19, 30, 3, meter=0.3)
    revised["revision"] = 2
    held = carry_quarter(
        first,
        [revised],
        opens + timedelta(minutes=7),
        run=None,
        executable_intents=frozenset({EXECUTION_INTENT_NET_EXPORT}),
    )
    assert held is first, "an open quarter is returned unchanged, not re-derived"
    assert held.grid_export_target_kwh == pytest.approx(0.6)

    # And across the *second* boundary the same holds for the second quarter: the
    # figure it was admitted with is the figure it keeps.
    second = carry_quarter(
        None,
        published,
        opens + QUARTER - timedelta(minutes=1),
        run=None,
        executable_intents=frozenset({EXECUTION_INTENT_NET_EXPORT}),
    )
    assert second is not None
    assert second.quarter_start == opens + QUARTER
    third = carry_quarter(
        second,
        [revised],
        opens + QUARTER + timedelta(minutes=7),
        run=None,
        executable_intents=frozenset({EXECUTION_INTENT_NET_EXPORT}),
    )
    assert third is second
    assert third.grid_export_target_kwh == pytest.approx(0.6)


def test_every_quarter_of_the_campaign_carries_the_same_campaign_id() -> None:
    """One sale, one identity, however many windows Stage B sees.

    This is what lets the Activity surface hold **one** lifecycle over a campaign
    the controller necessarily executes as separate windows -- and it is why
    ``campaign_id`` had to travel with the authority rather than being re-derived
    at the surface, where the horizon has already moved.
    """
    published = [export_target(19, 30, 4)]
    opens = local(NORMAL, 19, 30)
    seen = []
    for index in range(4):
        admitted = carry_quarter(
            None,
            published,
            opens + index * QUARTER - timedelta(minutes=1),
            run=None,
            executable_intents=frozenset({EXECUTION_INTENT_NET_EXPORT}),
        )
        assert admitted is not None
        seen.append(admitted.campaign_id)
    assert seen == ["camp01"] * 4


def test_a_pre_beta_32_target_degrades_to_run_level_behaviour() -> None:
    """No campaign id is a valid answer and must not be an error.

    The beta.27 ``quarter_schedule`` precedent: absent means fall back, never
    fail. A beta.31 record adopted by beta.32 carries no campaign, and the
    lifecycle then keys on direction and window end exactly as it did.
    """
    published = [export_target(19, 30, 2, campaign_id=None)]
    admitted = carry_quarter(
        None,
        published,
        local(NORMAL, 19, 30) - timedelta(minutes=1),
        run=None,
        executable_intents=frozenset({EXECUTION_INTENT_NET_EXPORT}),
    )
    assert admitted is not None
    assert admitted.campaign_id is None
    assert admitted.grid_export_target_kwh == pytest.approx(0.6)


# ===========================================================================
# 2. no catch-up, ever
# ===========================================================================


def test_a_short_quarter_does_not_enlarge_the_next_one() -> None:
    """The shortfall is recorded and Stage A decides the next quarter alone.

    Carrying a deficit forward would let Stage B accumulate an entitlement no
    economic layer authorised -- and on an export that entitlement is metered
    energy leaving the house at whatever price the next quarter happens to have.
    """
    published = [export_target(19, 30, 3)]
    opens = local(NORMAL, 19, 30)
    targets = [parse_target(raw) for raw in published]
    assert targets[0] is not None

    rows = [
        next_quarter_row(targets[0], opens + index * QUARTER - timedelta(minutes=1))
        for index in range(3)
    ]
    assert all(row is not None for row in rows)
    # Each row's objective is the figure Stage A published for *that* quarter, and
    # nothing in the schedule can express a debt from the one before.
    assert [row.grid_export_target_kwh for row in rows] == pytest.approx([0.6] * 3)


# ===========================================================================
# 3. the ceiling is not a completion test
# ===========================================================================


def test_an_export_reaching_its_battery_ceiling_is_not_a_completed_target() -> None:
    """R9, at the campaign scale: the highest-severity defect in the beta.32 set.

    ``demand_for`` compared the **battery** remainder against
    ``TARGET_TOLERANCE_KWH`` for every intent. An export run whose battery ceiling
    was at or below 0.25 kWh therefore satisfied ``remaining <= tolerance`` on its
    *first* evaluation -- before delivering anything -- and reported
    ``target_reached``. Reachable on any mid-quarter refresh while owned: a reload,
    a restart, a user action. It silently under-delivered money and then reported
    success, and the observed run's ceiling was exactly 0.25.

    The objective is at the meter; this machine's progress is battery-side, so it
    cannot judge completion for an export at all. The battery figure is a ceiling,
    and a ceiling is never a completion test -- hence ``<= 0.0``.
    """
    from custom_components.alpha_ems_manager.const import (
        EXECUTION_BASIS_ACCUMULATED,
        EXECUTION_QUALITY_MEASURED,
        EXECUTION_REDUCTION_BATTERY_CEILING,
        EXECUTION_REDUCTION_TARGET_MET,
    )
    from custom_components.alpha_ems_manager.execution import (
        TARGET_TOLERANCE_KWH,
        Progress,
        demand_for,
    )

    # **The observed run, to the kilowatt-hour.** Its battery ceiling was 0.25 --
    # exactly ``TARGET_TOLERANCE_KWH`` -- against a 0.11 kWh meter objective, so the
    # very first evaluation satisfied ``remaining <= tolerance`` and stopped it.
    ceiling = 0.25
    assert ceiling <= TARGET_TOLERANCE_KWH, "the fixture must sit inside the deadband"

    published = export_target(19, 30, 1, meter=0.11)
    published["battery_target_kwh"] = ceiling
    published["quarter_schedule"][0]["battery_kwh"] = ceiling
    target = parse_target(published)
    assert target is not None
    assert target.battery_target_kwh == pytest.approx(ceiling)
    assert target.grid_target_kwh == pytest.approx(0.11)

    def progress(realized: float) -> Progress:
        return Progress(
            realized_kwh=realized,
            basis=EXECUTION_BASIS_ACCUMULATED,
            quality=EXECUTION_QUALITY_MEASURED,
        )

    demand = demand_for(
        target,
        now=local(NORMAL, 19, 31),
        progress=progress(0.0),
    )
    # Not "target met" before a single kilowatt-hour crossed the meter.
    assert demand.reduction != EXECUTION_REDUCTION_TARGET_MET
    assert demand.remaining_kwh > 0.0

    # And when the ceiling genuinely is spent, the reason names the ceiling rather
    # than claiming the objective was met.
    spent = demand_for(
        target,
        now=local(NORMAL, 19, 31),
        progress=progress(target.battery_target_kwh),
    )
    assert spent.reduction == EXECUTION_REDUCTION_BATTERY_CEILING


# ===========================================================================
# 4. the runtime half: two boundaries on a live surface
# ===========================================================================


async def test_a_campaign_crossing_a_boundary_records_each_quarter_once(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Two export quarters, and the history has one row each.

    The boundary is where a quarter has previously been lost: through beta.26 a run
    ending on a boundary could not be replaced until the following one, so the
    quarter in between had no carrier and every tick reported "no owned run" while
    an economically active period went by. ``CarriedQuarter`` exists for that, and
    an export campaign is the first thing to cross two of them.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    opens = local(NORMAL, 19, 30)
    rows = export_rows(19, 30, 2, meter=0.6)
    coordinator._plan = AdmittedPlan(
        plan_id="sell-1",
        revision=1,
        run_id="run-1",
        intent=EXECUTION_INTENT_NET_EXPORT,
        purpose="export",
        admitted_at=opens - timedelta(minutes=1),
        rows=rows,
        campaign_id="camp01",
    )
    first = coordinator._plan.executing_quarter(opens + timedelta(minutes=1))
    assert first is not None
    install(coordinator, first)
    coordinator._plan = AdmittedPlan(
        plan_id="sell-1",
        revision=1,
        run_id="run-1",
        intent=EXECUTION_INTENT_NET_EXPORT,
        purpose="export",
        admitted_at=opens - timedelta(minutes=1),
        rows=rows,
        campaign_id="camp01",
    )
    coordinator._quarter = first

    # The first quarter expires and hands over without the campaign ending.
    coordinator._record_completed_quarter(first, QUARTER_END_EXPIRED)
    second = coordinator._plan.executing_quarter(opens + QUARTER + timedelta(minutes=1))
    assert second is not None
    assert second.quarter_start == opens + QUARTER
    assert second.campaign_id == "camp01"
    coordinator._reset_quarter_progress(second)
    coordinator._quarter = second
    coordinator._record_completed_quarter(second, QUARTER_END_EXPIRED)

    history = list(coordinator._completed_quarters)
    starts = [row["quarter_start"] for row in history]
    assert len(starts) == len(set(starts)), "a quarter may be recorded exactly once"
    assert len(history) == 2
    # Both rows name the campaign, at the boundary an export is judged at.
    assert {row["campaign_id"] for row in history} == {"camp01"}
    assert {row["objective_boundary"] for row in history} == {"meter"}


async def test_a_short_export_quarter_publishes_no_percentage_of_nothing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A sub-actuator objective has no meaningful percentage, and says so.

    The observed row published ``140 %`` against a 0.01 kWh objective:
    arithmetically correct, and useless. A percentage of a figure smaller than
    anything a command could move is noise wearing a decimal point, so it is
    withheld -- and the *signed* tracking error is published instead, which is the
    figure that can actually distinguish meter-side lag from noise.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    tiny = MIN_EXECUTABLE_QUARTER_KWH / 2.0
    quarter = CarriedQuarter(
        quarter_start=local(NORMAL, 19, 30),
        quarter_end=local(NORMAL, 19, 45),
        intent=EXECUTION_INTENT_NET_EXPORT,
        battery_target_kwh=0.5,
        grid_authorised_kwh=0.0,
        grid_export_target_kwh=tiny,
        initial_desired_grid_kw=-0.05,
        run_id="run-1",
        plan_id="sell-1",
        revision=1,
        admitted_at=local(NORMAL, 19, 29),
        campaign_id="camp01",
    )
    coordinator._completed_quarters.clear()
    coordinator._reset_quarter_progress(quarter)
    coordinator._record_completed_quarter(quarter, QUARTER_END_EXPIRED)

    row = list(coordinator._completed_quarters)[-1]
    assert row["shortfall_percent"] is None
    assert row["objective_tracking_error_fraction"] is None
    # The absolute figure is still there, signed: nothing was delivered against a
    # 0.0125 kWh objective.
    assert row["objective_tracking_error_kwh"] == pytest.approx(-tiny, abs=1e-6)


# ===========================================================================
# 5. the handover, and the dead-man across it
# ===========================================================================


async def test_a_row_ending_inside_a_campaign_hands_over_without_stopping(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The difference between one continuous sale and three separate arms.

    ``_async_end_row`` stops the dispatch **only when nothing follows**, and the
    decision is ``stop=self._quarter is None`` -- taken after the next quarter has
    been derived. Conflating the two situations is how a boundary loses a quarter:
    a row that ends inside a multi-quarter plan hands over, the dispatch keeps
    running, the claim stays, and only the measurements reset.

    On an export this matters more than on a charge. Stopping and re-arming across
    every boundary would return the pack to rest, re-run the whole authorisation
    sequence and re-anchor the vendor dead-man -- three times inside one sale, each
    with its own chance of an unverified write.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    opens = local(NORMAL, 19, 30)
    rows = export_rows(19, 30, 2, meter=0.6)
    plan = AdmittedPlan(
        plan_id="sell-1",
        revision=1,
        run_id="run-1",
        intent=EXECUTION_INTENT_NET_EXPORT,
        purpose="export",
        admitted_at=opens - timedelta(minutes=1),
        rows=rows,
        campaign_id="camp01",
    )
    coordinator._plan = plan
    first = plan.executing_quarter(opens + timedelta(minutes=1))
    assert first is not None
    install(coordinator, first)
    coordinator._plan = plan
    coordinator._quarter = first

    stops: list[bool] = []
    original = type(coordinator)._async_stop_owned_run

    async def record(self, now, snapshot, reason):
        stops.append(True)
        return await original(self, now, snapshot, reason)

    coordinator.__class__._async_stop_owned_run = record  # type: ignore[assignment]
    try:
        # The boundary: the second row exists, so the handover must not stop.
        coordinator._quarter = plan.executing_quarter(
            opens + QUARTER + timedelta(minutes=1)
        )
        assert coordinator._quarter is not None
        await coordinator._async_end_row(
            first, opens + QUARTER, None, stop=coordinator._quarter is None
        )
        assert stops == [], "a handover inside a campaign must not stop the dispatch"

        # And the end of the campaign: nothing follows, so it does stop.
        last = coordinator._quarter
        coordinator._quarter = plan.executing_quarter(opens + 2 * QUARTER)
        assert coordinator._quarter is None
        await coordinator._async_end_row(
            last, opens + 2 * QUARTER, None, stop=coordinator._quarter is None
        )
        assert stops == [True], "the last row of a campaign must stop the dispatch"
    finally:
        coordinator.__class__._async_stop_owned_run = original  # type: ignore[assignment]


async def test_the_handover_resets_the_measurements_and_nothing_else(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Only the accumulators reset. The claim and the dead-man observation stay.

    The dead-man is why: a run continues **only because each refresh demonstrably
    advances the vendor timer**, and the observation it is compared against is
    taken at the sustaining write. Clearing it at a boundary would compare the next
    quarter's first sustain against a deadline from a run that no longer exists --
    which is the fault that made a timer stalling halfway through a run look
    healthy.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    opens = local(NORMAL, 19, 30)
    rows = export_rows(19, 30, 2, meter=0.6)
    plan = AdmittedPlan(
        plan_id="sell-1",
        revision=1,
        run_id="run-1",
        intent=EXECUTION_INTENT_NET_EXPORT,
        purpose="export",
        admitted_at=opens - timedelta(minutes=1),
        rows=rows,
        campaign_id="camp01",
    )
    coordinator._plan = plan
    first = plan.executing_quarter(opens + timedelta(minutes=1))
    assert first is not None
    install(coordinator, first)
    coordinator._plan = plan
    coordinator._quarter = first
    coordinator._quarter_grid_export_kwh = 0.42
    coordinator._sustained_run_id = "run-1"
    coordinator._sustained_deadline = opens + timedelta(minutes=20)
    record_before = coordinator.store.execution_record

    coordinator._quarter = plan.executing_quarter(
        opens + QUARTER + timedelta(minutes=1)
    )
    await coordinator._async_end_row(
        first, opens + QUARTER, None, stop=coordinator._quarter is None
    )

    # The measurements are the only thing that moved.
    assert coordinator._quarter_grid_export_kwh == pytest.approx(0.0)
    assert coordinator._sustained_run_id == "run-1"
    assert coordinator._sustained_deadline == opens + timedelta(minutes=20)
    assert coordinator.store.execution_record == record_before
