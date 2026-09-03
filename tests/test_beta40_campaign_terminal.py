"""beta.40: a campaign terminal may not claim an objective it did not reach.

**The live evidence.** The 2026-09-03 charge campaign closed like this:

    objective_target_kwh     13.100
    objective_realized_kwh    5.939      45 % delivered
    rows_completed               23      of 23
    outcome                 partial      correct, and computed from the energy
    reason   campaign_objective_reached  a claim about the objective, and false

`outcome` was never wrong -- `_close_campaign` computes it here, from the
measurement -- but a reader trusting `reason` would have filed a 45 %-delivered
campaign as a success, and the two fields sat side by side.

**The cause was a scope standing in for a reason.** `_completion_scope` returns
`STOP_SCOPE_CAMPAIGN` for two entirely different endings: the objective was
delivered, or the last planned row closed without it. Both are legitimate, both
stop the same dispatch, so one *scope* is right -- but both call sites then
published `campaign_objective_reached`, which is a statement about the objective
and only one of the two endings supports it.

`window_ended` already exists, already means exactly "the last planned quarter
closed", and is already in `EXECUTION_COMPLETION_STOP_REASONS` -- so the outcome
mapping is untouched and a short campaign still files `partial`, never
`canceled`. No new vocabulary; a synonym would be a sixth alias to disambiguate.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    EXECUTION_COMPLETION_STOP_REASONS,
    EXECUTION_STOP_CAMPAIGN_COMPLETE,
    EXECUTION_STOP_QUARTER_EXPIRED,
    EXECUTION_STOP_QUARTER_TARGET_REACHED,
    EXECUTION_STOP_WINDOW_ENDED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge
from .test_beta27_quarter_execution import install, quarter_at

pytestmark = pytest.mark.usefixtures("control_surface")

#: The live campaign, so the numbers in the tests are the numbers that happened.
LIVE_TARGET_KWH = 13.100
LIVE_REALISED_KWH = 5.939
LIVE_ROWS = 23


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def open_campaign(
    coordinator, *, target: float, realised: float, rows: int = LIVE_ROWS
):
    """Put an open campaign in place with a frozen target and a realised total."""
    quarter = coordinator._quarter
    assert quarter is not None
    # An explicit id: ``quarter_at`` builds a row with no campaign, and an open
    # campaign is exactly what these tests are about.
    coordinator._campaign_id = "f45d513a019342c9"
    coordinator._campaign_instance_id = "instance-1"
    coordinator._campaign_frozen_target_kwh = target
    coordinator._campaign_quarters_admitted = rows
    coordinator._campaign_objective_rows = rows
    coordinator._campaign_realized_kwh = realised
    coordinator._campaign_started_at = local(NORMAL, 9, 0)
    coordinator._campaign_opened_at = local(NORMAL, 9, 0)
    coordinator._campaign_measurable = True
    coordinator._quarter_battery_kwh = 0.0


# == 1. the reason is decided by the energy, not by the scope ================


async def test_a_window_that_ended_short_says_window_ended(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The live defect, on the live figures.**

    23 of 23 rows closed, so the schedule was final and the campaign scope was
    right. 5.939 of 13.100 kWh was delivered, so the objective was not reached and
    the reason must not say it was.

    *Mutation: publish ``campaign_objective_reached`` for any campaign-scoped stop
    and this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(coordinator, target=LIVE_TARGET_KWH, realised=LIVE_REALISED_KWH)

    assert coordinator._campaign_objective_met() is False
    assert (
        coordinator._campaign_stop_reason(EXECUTION_STOP_QUARTER_EXPIRED)
        == EXECUTION_STOP_WINDOW_ENDED
    )


async def test_a_delivered_objective_still_says_objective_reached(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The reason keeps its meaning where the meaning is true.

    A fix that made ``campaign_objective_reached`` unreachable would have replaced
    a false claim with no claim at all.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(coordinator, target=LIVE_TARGET_KWH, realised=LIVE_TARGET_KWH)

    assert coordinator._campaign_objective_met() is True
    assert (
        coordinator._campaign_stop_reason(EXECUTION_STOP_QUARTER_EXPIRED)
        == EXECUTION_STOP_CAMPAIGN_COMPLETE
    )


async def test_an_objective_reached_inside_tolerance_counts_as_reached(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The same tolerance the verdict uses, deliberately.**

    Three places ask whether the objective was met -- the scope, the reason and
    the outcome -- and if each picked its own tolerance a campaign could be filed
    Success with a reason saying it fell short.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    tolerance = coordinator._completion_tolerance_kwh(LIVE_TARGET_KWH, LIVE_ROWS)
    assert tolerance > 0.0, "a zero tolerance would make this test vacuous"
    open_campaign(
        coordinator,
        target=LIVE_TARGET_KWH,
        realised=LIVE_TARGET_KWH - tolerance * 0.5,
    )

    assert coordinator._campaign_objective_met() is True
    assert (
        coordinator._campaign_stop_reason(EXECUTION_STOP_QUARTER_EXPIRED)
        == EXECUTION_STOP_CAMPAIGN_COMPLETE
    )


async def test_an_objective_reached_early_says_objective_reached(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Delivered ahead of the window closing, with rows still unopened.

    The energy is what the claim is about, so the reason does not wait for the
    schedule to run out.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(
        coordinator, target=2.0, realised=2.4, rows=4
    )  # over-delivered, four rows in

    assert coordinator._campaign_objective_met() is True
    assert (
        coordinator._campaign_stop_reason(EXECUTION_STOP_QUARTER_TARGET_REACHED)
        == EXECUTION_STOP_CAMPAIGN_COMPLETE
    )


async def test_no_campaign_open_keeps_the_rows_own_reason(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """An ordinary single-run charge has no campaign to have completed.

    "Campaign objective reached" would be a claim about a thing that does not
    exist, and the surfaces would render a campaign success for a run.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    coordinator._campaign_id = None
    coordinator._campaign_frozen_target_kwh = None

    assert coordinator._campaign_objective_met() is False
    for row_reason in (
        EXECUTION_STOP_QUARTER_EXPIRED,
        EXECUTION_STOP_QUARTER_TARGET_REACHED,
    ):
        assert coordinator._campaign_stop_reason(row_reason) == row_reason


async def test_a_campaign_with_no_published_objective_never_claims_one(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """No target recorded means nothing to have reached. Fail closed."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(coordinator, target=LIVE_TARGET_KWH, realised=LIVE_REALISED_KWH)
    coordinator._campaign_frozen_target_kwh = None

    assert coordinator._campaign_objective_met() is False
    assert (
        coordinator._campaign_stop_reason(EXECUTION_STOP_QUARTER_EXPIRED)
        == EXECUTION_STOP_WINDOW_ENDED
    )


# == 2. the terminal record, end to end =====================================


async def test_the_terminal_record_pairs_partial_with_window_ended(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Outcome and reason must agree, and the live record did not.**

    Filed on the live figures: ``partial`` beside ``window_ended``, with the
    tracking error stating the size of the miss.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(coordinator, target=LIVE_TARGET_KWH, realised=LIVE_REALISED_KWH)

    coordinator._close_campaign(
        local(NORMAL, 14, 45),
        coordinator._campaign_stop_reason(EXECUTION_STOP_QUARTER_EXPIRED),
    )
    record = coordinator._closed_campaign
    assert record is not None

    assert record["outcome"] == OUTCOME_PARTIAL
    assert record["reason"] == EXECUTION_STOP_WINDOW_ENDED
    assert record["objective_target_kwh"] == pytest.approx(LIVE_TARGET_KWH, abs=1e-3)
    assert record["objective_realized_kwh"] == pytest.approx(
        LIVE_REALISED_KWH, abs=1e-3
    )
    assert record["objective_tracking_error_kwh"] == pytest.approx(-7.161, abs=1e-3)
    # ``window_ended`` is a completion reason, so the outcome mapping is unmoved:
    # a short campaign is Partial, never Canceled.
    assert EXECUTION_STOP_WINDOW_ENDED in EXECUTION_COMPLETION_STOP_REASONS


async def test_the_terminal_record_pairs_success_with_objective_reached(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The other half, so the pairing is proven in both directions."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(coordinator, target=LIVE_TARGET_KWH, realised=LIVE_TARGET_KWH)

    coordinator._close_campaign(
        local(NORMAL, 14, 45),
        coordinator._campaign_stop_reason(EXECUTION_STOP_QUARTER_EXPIRED),
    )
    record = coordinator._closed_campaign
    assert record is not None

    assert record["outcome"] == OUTCOME_SUCCESS
    assert record["reason"] == EXECUTION_STOP_CAMPAIGN_COMPLETE


async def test_the_terminal_is_emitted_exactly_once(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Exactly-once, and the reason fix must not have weakened it.**

    A second close of the same attempt files nothing: the instance is latched, and
    two terminals for one attempt is the shape that lets a reader double-count a
    campaign.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(coordinator, target=LIVE_TARGET_KWH, realised=LIVE_REALISED_KWH)

    coordinator._close_campaign(local(NORMAL, 14, 45), EXECUTION_STOP_WINDOW_ENDED)
    first = coordinator._closed_campaign
    assert first is not None
    coordinator._closed_campaign = None

    # Re-open the same instance and close it again.
    coordinator._campaign_id = first["campaign_id"]
    coordinator._campaign_instance_id = first["campaign_instance_id"]
    coordinator._campaign_started_at = local(NORMAL, 9, 0)
    coordinator._close_campaign(
        local(NORMAL, 14, 46) + timedelta(seconds=1), EXECUTION_STOP_WINDOW_ENDED
    )

    assert coordinator._closed_campaign is None, "a second terminal was filed"


async def test_the_frozen_target_is_never_rewritten_by_the_reason_fix(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Asking whether the objective was met must not adjust the objective.

    The frozen target is the thing the campaign is judged against; a predicate
    that quietly relaxed it would make every campaign a success.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(coordinator, target=LIVE_TARGET_KWH, realised=LIVE_REALISED_KWH)

    for _ in range(3):
        coordinator._campaign_objective_met()
        coordinator._campaign_stop_reason(EXECUTION_STOP_QUARTER_EXPIRED)

    assert coordinator._campaign_frozen_target_kwh == pytest.approx(LIVE_TARGET_KWH)
    assert coordinator._campaign_realized_kwh == pytest.approx(LIVE_REALISED_KWH)


async def test_free_pv_retention_may_continue_after_the_objective_is_met(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**beta.40 case 6: reaching the objective is not the end of the row.**

    The campaign objective being met makes the *reason* true; it does not close an
    open row that is still storing free production. The two questions are separate
    and this pins them apart -- the retention gate reads the row, never the
    campaign ledger.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.28, authorised=0.04))
    open_campaign(coordinator, target=2.0, realised=2.4, rows=4)

    # The campaign objective is met...
    assert coordinator._campaign_objective_met() is True
    # ...and the open row's own objective/absorbed split is untouched by that.
    coordinator._quarter_battery_kwh = 0.28
    assert coordinator._quarter_objective_kwh == pytest.approx(0.28, abs=1e-9)
    coordinator._quarter_battery_kwh = 1.28
    assert coordinator._quarter_objective_kwh == pytest.approx(0.28, abs=1e-9)
    assert coordinator._quarter_absorbed_kwh == pytest.approx(1.0, abs=1e-9)
