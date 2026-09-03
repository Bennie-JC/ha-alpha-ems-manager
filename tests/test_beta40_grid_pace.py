"""beta.40: the run's grid budget is an energy, not a pace.

**The largest single defect this release fixes, and it cost 5.076 kWh.**

Stage A concentrates a grid purchase into the cheapest quarters of a window and
asks for full charging power there. `_charge_limits` turned the run's remaining
authorisation into a rate by dividing it by the *run's* remaining time -- a flat
average -- and `decide_charge` then folded that rate into `battery_cap_kw`, where
it capped the battery in every individual row.

Measured on the 2026-09-03 campaign, on the three rows Stage A had sized at full
inverter power:

    row 11:45   needed 10.00 kW   row authorised 9.08   flat pace 2.73
                observed mean 3.40 kW   delivered 0.795 of 2.50
    row 12:15   needed 10.00 kW   row authorised 9.28   flat pace 2.99
                observed mean 3.46 kW   delivered 0.808 of 2.50
    row 13:00   needed 10.00 kW   row authorised 9.84   flat pace 3.78
                observed mean 4.38 kW   delivered 1.021 of 2.50

each observed mean predicted by `pace + measured surplus` to within 0.5-2.8 %.
Those three rows carry 4.876 kWh -- 68 % -- of the 7.161 kWh campaign shortfall,
and the campaign ended with **5.076 kWh of authorised grid purchase unspent**.

**It is the beta.36 defect one level up.** beta.36 stopped the *row's* grid
authorisation capping battery power; the *run's* remaining budget was still doing
it, as an average.

The run figure is now expressed at the open row's own clock. Two properties have
to hold together and this file pins both: a concentrated row reaches the power its
own authorisation permits, and the run total is still bounded exactly -- with no
catch-up, because each row is bounded by its own frozen authorisation first.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import EXECUTION_REDUCTION_NONE
from custom_components.alpha_ems_manager.execution import Demand

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge
from .test_beta27_quarter_execution import install, quarter_at

pytestmark = pytest.mark.usefixtures("control_surface")

#: The live run: 8.85 kWh of grid authorisation across 09:00-14:45.
RUN_GRID_CAP_KWH = 8.85
#: The three concentrated rows, verbatim from the diagnostic.
ROW_BATTERY_KWH = 2.50
ROW_GRID_AUTHORISED_KWH = 2.27
#: 2.50 kWh over a quarter is the full inverter power the plan asked for.
NEEDED_KW = ROW_BATTERY_KWH / 0.25


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def demand_at(*, charged: float, run_minutes: float) -> Demand:
    """Return the run-level demand a mid-campaign refresh would carry."""
    return Demand(
        rolling_kw=NEEDED_KW,
        ceiling_kw=None,
        required_kw=NEEDED_KW,
        reduction=EXECUTION_REDUCTION_NONE,
        remaining_kwh=ROW_BATTERY_KWH,
        remaining_minutes=run_minutes,
        ahead_kwh=0.0,
        grid_cap_kwh=RUN_GRID_CAP_KWH,
        grid_charged_kwh=charged,
    )


def limits_for(coordinator, *, charged: float, run_minutes: float, now):
    """Return the charge limits for an open concentrated row."""
    coordinator._stage_b_decision = type(
        "Decision", (), {"demand": demand_at(charged=charged, run_minutes=run_minutes)}
    )()
    return coordinator._charge_limits(None, now)


# == 1. a concentrated row is not throttled by a flat pace ==================


async def test_a_concentrated_row_is_not_capped_by_the_runs_average_pace(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The defect, on the live run's own figures.**

    At 11:45 the run had 8.199 kWh left over 180 minutes: a flat pace of 2.73 kW
    against a row that needed 10.00 and was itself authorised 9.08. The run figure
    must not be the binding term.

    *Mutation: divide the remaining budget by the run's remaining minutes and this
    fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    row = quarter_at(
        10, 45, battery=ROW_BATTERY_KWH, authorised=ROW_GRID_AUTHORISED_KWH
    )
    install(coordinator, row)
    now = local(NORMAL, 10, 45)

    limits = limits_for(coordinator, charged=0.651, run_minutes=180.0, now=now)

    flat_pace = (RUN_GRID_CAP_KWH - 0.651) / 3.0
    assert flat_pace == pytest.approx(2.733, abs=0.01), flat_pace
    # The run figure now permits the whole remaining budget inside this row, so it
    # is far above both the row's own authorisation and the power the plan needs.
    assert limits.remaining_grid_kw is not None
    assert limits.remaining_grid_kw > NEEDED_KW, limits.remaining_grid_kw
    assert limits.remaining_grid_kw > flat_pace * 5


@pytest.mark.parametrize(
    ("charged", "run_minutes", "flat_pace"),
    [
        (0.651, 180.0, 2.733),  # row 11:45
        (1.370, 150.0, 2.992),  # row 12:15
        (2.233, 105.0, 3.781),  # row 13:00
    ],
)
async def test_each_of_the_three_throttled_rows_is_now_free_to_run(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,
    charged: float,
    run_minutes: float,
    flat_pace: float,
) -> None:
    """All three rows the live campaign lost, each with its own arithmetic.

    The flat pace figures are the ones reconstructed from the diagnostic and they
    predicted the observed mean dispatch to within 0.5-2.8 %; each is now well
    below what the run figure permits.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(
        coordinator,
        quarter_at(10, 45, battery=ROW_BATTERY_KWH, authorised=ROW_GRID_AUTHORISED_KWH),
    )
    now = local(NORMAL, 10, 45)

    limits = limits_for(coordinator, charged=charged, run_minutes=run_minutes, now=now)

    assert (RUN_GRID_CAP_KWH - charged) / (run_minutes / 60.0) == pytest.approx(
        flat_pace, abs=0.01
    )
    assert limits.remaining_grid_kw > NEEDED_KW, (flat_pace, limits.remaining_grid_kw)


# == 2. the run total is still bounded, and nothing catches up ==============


async def test_a_nearly_spent_run_budget_still_binds_hard(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The half of the contract the fix must not have loosened.**

    The run figure is an energy ceiling. With 0.05 kWh left of the authorisation,
    the rate it permits across the open row is 0.05 / 0.25 h = 0.2 kW -- and that
    binds, hard.

    *Mutation: drop the run figure entirely and this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(
        coordinator,
        quarter_at(10, 45, battery=ROW_BATTERY_KWH, authorised=ROW_GRID_AUTHORISED_KWH),
    )
    now = local(NORMAL, 10, 45)

    limits = limits_for(
        coordinator, charged=RUN_GRID_CAP_KWH - 0.05, run_minutes=60.0, now=now
    )

    # A quarter of an hour to spend 0.05 kWh in.
    assert limits.remaining_grid_kw == pytest.approx(0.2, abs=0.02)
    assert limits.remaining_grid_kw < NEEDED_KW


async def test_an_exhausted_run_budget_permits_no_purchase_at_all(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Spent is spent. The row may still absorb free production, never buy."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(
        coordinator,
        quarter_at(10, 45, battery=ROW_BATTERY_KWH, authorised=ROW_GRID_AUTHORISED_KWH),
    )
    now = local(NORMAL, 10, 45)

    limits = limits_for(
        coordinator, charged=RUN_GRID_CAP_KWH, run_minutes=60.0, now=now
    )

    assert limits.remaining_grid_kw == pytest.approx(0.0, abs=1e-9)


async def test_the_row_permits_at_most_the_remaining_budget_across_itself(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The energy bound, stated as energy. This is why it is not catch-up.**

    ``rate * row_hours <= remaining_budget`` for every state of the budget and
    every point in the row. Missed energy from an expired quarter never becomes
    available here: this bounds the run's *unspent* authorisation, and each row is
    bounded by its own frozen ``grid_authorised_kwh`` first.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    row = quarter_at(
        10, 45, battery=ROW_BATTERY_KWH, authorised=ROW_GRID_AUTHORISED_KWH
    )
    install(coordinator, row)

    for charged in (0.0, 2.0, 5.0, 8.0, RUN_GRID_CAP_KWH):
        for minute, second in ((45, 0), (50, 0), (57, 0), (59, 30)):
            now = local(NORMAL, 10, minute).replace(second=second)
            remaining_budget = max(0.0, RUN_GRID_CAP_KWH - charged)
            limits = limits_for(
                coordinator, charged=charged, run_minutes=240.0, now=now
            )
            row_hours = max(90.0, row.seconds_remaining(now)) / 3600.0
            assert limits.remaining_grid_kw * row_hours <= remaining_budget + 1e-6, (
                charged,
                minute,
                second,
                limits.remaining_grid_kw,
            )


async def test_the_rows_own_authorisation_is_still_what_paces_the_row(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**No catch-up, because the row's frozen figure is the first bound.**

    ``_charge_limits`` does not know the row's authorisation and must not: that is
    ``progress.grid_rate_kw``, derived from the frozen ``grid_authorised_kwh``, and
    ``decide_charge`` takes the ``min`` of the two. A row authorised 0.04 kWh can
    still only buy 0.04 kWh however much the run has left.
    """
    from custom_components.alpha_ems_manager.dispatch import (
        QuarterProgress,
        decide_charge,
    )

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    now = local(NORMAL, 10, 45)
    limits = limits_for(coordinator, charged=0.0, run_minutes=240.0, now=now)

    # The run figure is generous -- well above the power any row here needs. Its
    # exact value depends on the forward revision the fixture carries, which is
    # not what this test is about.
    assert limits.remaining_grid_kw > NEEDED_KW
    # ...and the row's own 0.04 kWh authorisation still governs what it may buy.
    progress = QuarterProgress(
        seconds_remaining=900.0,
        battery_remaining_kwh=0.28,
        grid_remaining_kwh=0.04,
    )
    decision = decide_charge(
        progress=progress,
        house_load_kw=0.8,
        pv_kw=0.0,
        limits=replace(limits, inverter_kw=10.0),
        last_applied_kw=None,
    )
    # No production, so every kWh is bought: the command cannot exceed the row's
    # own grid rate of 0.04 / 0.25 = 0.16 kW.
    assert abs(decision.applied_kw) <= 0.04 / 0.25 + 1e-9, decision
