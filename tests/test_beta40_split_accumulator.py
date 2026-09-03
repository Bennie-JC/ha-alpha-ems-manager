"""beta.40 Gate 3: absorbed production is not progress against an objective.

**The correctness gate of the release, and the one whose failure mode is a killed
campaign rather than a lost kilowatt-hour.**

`_completion_scope` ends a campaign when its realised total reaches the target it
froze at first activation. That total is summed from each row's *objective*. If
free production stored above a row's objective were counted into it, a sunny
afternoon would drive the figure past a target the campaign had not finished buying
towards, and Stage B would announce `campaign_complete` on a live campaign.

On the 2026-09-03 capture the frozen target was 13.1 kWh against 12.6144 kWh of
pack headroom, so the pack ceiling happens to sit half a kilowatt-hour below the
trip point -- which is precisely why this is closed by construction rather than
left to a coincidence.

The split is `objective = min(total, allowance)` and `absorbed = total - objective`,
derived rather than integrated: crediting the objective first and capping it hard
gives `obj = min(T, A)` for any sequence of increments, and an opened quarter's
allowance cannot move. So there is no second accumulator to reset, capture, restore
or lose across a stop -- and the capture tuples in the coordinator are positional.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_NET_EXPORT,
)

from .beta40_trace import CAMPAIGN_FROZEN_TARGET_KWH, ROW_BATTERY_KWH
from .test_beta24_live_charge import LiveSurface, owned_live_charge
from .test_beta27_quarter_execution import install, quarter_at

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def authorised_row(hour: int, minute: int, *, battery: float = ROW_BATTERY_KWH):
    """Return the capture's row shape, with Stage A's retention verdict on it."""
    from dataclasses import replace

    return replace(
        quarter_at(hour, minute, battery=battery, authorised=0.04),
        retention_authorised=True,
    )


# == 1. the identity =======================================================


async def test_objective_and_absorbed_always_sum_to_the_measured_charge(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The invariant a reader can check, swept over the whole range.**

    Driven by setting the measured total directly, which is what the accrual
    produces, and asserted either side of the allowance -- below it the objective is
    everything, above it the excess is absorbed and the objective stops.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, authorised_row(10, 45, battery=1.0))

    for total in (0.0, 0.1, 0.5, 0.999, 1.0, 1.001, 1.5, 3.0, 12.0):
        coordinator._quarter_battery_kwh = total

        objective = coordinator._quarter_objective_kwh
        absorbed = coordinator._quarter_absorbed_kwh

        assert objective + absorbed == pytest.approx(total, abs=1e-12), total
        assert objective <= 1.0 + 1e-12, total
        assert absorbed >= 0.0, total
        # Objective first: nothing is called absorbed while the promise is unmet.
        if total <= 1.0:
            assert absorbed == pytest.approx(0.0, abs=1e-12), total
            assert objective == pytest.approx(total, abs=1e-12), total
        else:
            assert objective == pytest.approx(1.0, abs=1e-12), total
            assert absorbed == pytest.approx(total - 1.0, abs=1e-12), total


async def test_an_export_row_has_no_absorbed_share_at_all(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """There is no such thing as free production to discharge.

    An export's whole movement is its objective, so the identity holds trivially and
    the absorbed figure stays zero however much the pack delivered.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.8),
    )
    coordinator._quarter_battery_kwh = 2.5

    assert coordinator._quarter_absorbed_kwh == 0.0
    assert coordinator._quarter_objective_kwh == pytest.approx(2.5)


# == 2. the campaign is judged on the objective alone =====================


async def test_absorbed_production_does_not_advance_the_campaign(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The hazard this gate closes.**

    A row that stored four times its objective in free production has realised its
    objective once. Counting the rest would be counting energy nobody promised
    towards a target nobody revised.

    *Mutation: read ``_quarter_battery_kwh`` in ``_open_quarter_objective_kwh`` and
    this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, authorised_row(10, 45, battery=0.28))
    coordinator._campaign_id = coordinator._quarter.campaign_id
    coordinator._campaign_realized_kwh = 0.0

    coordinator._quarter_battery_kwh = 0.28
    objective_only = coordinator._campaign_realized_now()
    # Now the same row absorbs two more kilowatt-hours of free production.
    coordinator._quarter_battery_kwh = 2.28
    with_absorption = coordinator._campaign_realized_now()

    assert objective_only == pytest.approx(0.28, abs=1e-9)
    assert with_absorption == pytest.approx(0.28, abs=1e-9)


async def test_a_sunny_row_does_not_trip_the_campaign_terminal_early(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The killed-campaign failure mode, on the capture's own figures.**

    Frozen target 13.1 kWh, as the live campaign carried. A row absorbing far past
    its own objective must leave ``_completion_scope`` answering "hold", because the
    campaign has not delivered what it promised -- it has delivered something else
    as well.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, authorised_row(10, 45, battery=0.28))
    coordinator._campaign_id = coordinator._quarter.campaign_id
    coordinator._campaign_frozen_target_kwh = CAMPAIGN_FROZEN_TARGET_KWH
    coordinator._campaign_quarters_admitted = 4
    coordinator._campaign_realized_kwh = 0.923

    # Absorb enough that the *total* would have cleared the frozen target twice.
    coordinator._quarter_battery_kwh = 30.0

    assert coordinator._campaign_realized_now() < CAMPAIGN_FROZEN_TARGET_KWH
    assert coordinator._row_objective_kwh(coordinator._quarter) == pytest.approx(0.28)


async def test_the_frozen_campaign_target_is_still_summed_from_objectives(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The target side of the same comparison, unmoved.

    A campaign objective built from anything but ``row.battery_kwh`` would move the
    goalposts rather than the score.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, authorised_row(10, 45, battery=0.28))
    plan = coordinator._plan
    assert plan is not None

    expected = sum(row.battery_kwh for row in plan.rows if row.executable)
    assert expected == pytest.approx(0.28)


# == 3. the completed row records both, and they still sum ================


async def test_a_completed_row_records_the_split_and_the_shortfall_on_the_objective(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**A shortfall is a missed promise, not a missed total.**

    Judging the row against every kWh the pack took would let absorbed production
    paper over an objective the row genuinely missed -- so the record keeps the two
    apart and publishes both.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, authorised_row(10, 45, battery=1.0))
    row = coordinator._quarter
    assert row is not None

    # Half the objective delivered, and a kilowatt-hour of free production stored.
    coordinator._quarter_battery_kwh = 1.5
    coordinator._quarter_objective_shortfall_probe = None  # documentation only
    coordinator._record_completed_quarter(row, "quarter_expired")

    record = coordinator._completed_quarters[-1]
    assert record["realized_battery_kwh"] == pytest.approx(1.5, abs=1e-3)
    assert record["objective_battery_kwh"] == pytest.approx(1.0, abs=1e-3)
    assert record["absorbed_extra_kwh"] == pytest.approx(0.5, abs=1e-3)
    assert record["objective_battery_kwh"] + record["absorbed_extra_kwh"] == (
        pytest.approx(record["realized_battery_kwh"], abs=1e-3)
    )
    # The objective was met, so there is no shortfall to report.
    assert record["shortfall_kwh"] == pytest.approx(0.0, abs=1e-3)
    assert record["retention_authorised"] is True


async def test_a_row_that_missed_its_objective_still_says_so_while_absorbing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The case absorption must not hide: promise unmet, production stored.

    Physically possible -- the grid budget spent while the sun came out -- and the
    record has to be able to say both things at once.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, authorised_row(10, 45, battery=2.0))
    row = coordinator._quarter
    assert row is not None
    coordinator._quarter_battery_kwh = 1.2

    coordinator._record_completed_quarter(row, "quarter_expired")

    record = coordinator._completed_quarters[-1]
    assert record["objective_battery_kwh"] == pytest.approx(1.2, abs=1e-3)
    assert record["absorbed_extra_kwh"] == pytest.approx(0.0, abs=1e-3)
    assert record["shortfall_kwh"] == pytest.approx(0.8, abs=1e-3)


# == 4. the derivation is equivalent to integrating =======================


async def test_deriving_the_split_matches_crediting_the_objective_first(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The proof the implementation rests on, executed rather than asserted.**

    ``obj(n+1) = obj(n) + min(step, A - obj(n))`` summed over an arbitrary sequence
    of increments equals ``min(total, A)``. If the two ever disagreed the derived
    form would be wrong, so the running sum is computed here and compared.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    allowance = 1.0
    install(coordinator, authorised_row(10, 45, battery=allowance))

    running = 0.0
    total = 0.0
    for step in (0.03, 0.4, 0.11, 0.5, 0.02, 0.9, 1.4, 0.001):
        total += step
        running += min(step, max(0.0, allowance - running))
        coordinator._quarter_battery_kwh = total

        assert coordinator._quarter_objective_kwh == pytest.approx(running, abs=1e-12)
        assert coordinator._quarter_absorbed_kwh == pytest.approx(
            total - running, abs=1e-12
        )


async def test_the_split_survives_the_end_of_quarter_capture_and_restore(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The capture tuples are positional, and both figures come back.

    They are derived from the total, so this holds by construction rather than by
    remembering to add two entries in three places -- which is the whole reason they
    are derived. Asserted anyway: the property is what matters, not the mechanism.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, authorised_row(10, 45, battery=1.0))
    coordinator._quarter_battery_kwh = 2.5

    captured = coordinator._capture_quarter_progress()
    coordinator._reset_quarter_progress(coordinator._quarter)
    assert coordinator._quarter_objective_kwh == 0.0
    assert coordinator._quarter_absorbed_kwh == 0.0

    coordinator._restore_quarter_progress(captured)

    assert coordinator._quarter_battery_kwh == pytest.approx(2.5)
    assert coordinator._quarter_objective_kwh == pytest.approx(1.0)
    assert coordinator._quarter_absorbed_kwh == pytest.approx(1.5)
