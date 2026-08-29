"""beta.35: the two public entities answer the question they are named for.

**Both were wrong on 2026-08-29 at the same instant, and in opposite directions.**
A 10 kW export was physically running, the meter was 8.7 kW negative, and:

* ``Economic Action`` read ``idle`` -- because it asked Stage A, whose horizon head
  is ``elapsed + 1`` and therefore describes the *next* interval and structurally
  never the one in progress;
* ``Next Planned Action`` read ``charge`` -- because it took the first run *after*
  the head, which skipped the Sell about to start at 19:45 and reported tomorrow's
  refill instead.

beta.35 gives each entity to the layer that can answer it: the present tense is a
Stage-B fact, the future tense is a Stage-A one. Neither entity may report the
other's tense, and neither may let its state and its attributes describe different
runs -- itself a beta.34 fault, with ``idle`` published beside a window, an energy
and a price all describing a sale planned for 21:00.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    CONTROL_MODE_SHADOW,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_IDLE,
    ECONOMIC_ACTION_SAFETY_BUY,
    EXECUTION_INTENT_NET_EXPORT,
    OWNERSHIP_OWNED,
    OWNERSHIP_UNPROVEN,
)
from custom_components.alpha_ems_manager.sensor import (
    _economic_action_attributes,
    _economic_action_value,
    _next_planned_action_attributes,
    _next_planned_action_value,
    _next_planned_run,
)

from .beta35_trace import CAMPAIGN_ID, step_clock
from .test_beta24_live_charge import LiveSurface, step_once
from .test_beta35_campaign_continuity import start_the_campaign

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


class _Surface:
    """A coordinator stand-in carrying one solved outcome and one control report.

    Only the container is a stub. The outcome comes from the production solver and
    the execution block is shaped exactly as ``_build_control_report`` publishes
    it, because the whole point of these entities is what they do with real
    payloads.
    """

    #: Read by ``_economic_blocked_reason``; nothing is blocked in these fixtures.
    economic_blocked_reason = ""

    def __init__(self, outcome=None, control: dict | None = None) -> None:
        self.data = {"economic": outcome, "control": control or {}}


def _executing_export(
    *, ownership: str = OWNERSHIP_OWNED, mode: str = "active"
) -> dict:
    """Return a control report describing an admitted, open export quarter."""
    return {
        "mode": mode,
        "execution": {
            "quarter": {"intent": EXECUTION_INTENT_NET_EXPORT},
            "purpose": "export",
            "state": "executing",
            "plan_id": "5a4f54a741429531",
            "ownership": {"state": ownership},
            "target": {"grid_target_kwh": 5.05},
            "progress": {"objective_realized_kwh": 1.92},
            "open_campaign": {
                "campaign_id": CAMPAIGN_ID,
                "frozen_target_kwh": 5.05,
                "started_at": "2026-08-29T19:45:00+02:00",
            },
            "carried": {"run": {"run_id": "b960f9b5e1d9e4cb"}},
            "power": {"applied_kw": 10.0},
        },
    }


# ===========================================================================
# 1. Economic Action -- the present tense, read from the execution surface
# ===========================================================================


async def test_economic_action_reads_export_during_an_owned_live_sell(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The reading that was ``idle`` while 8.7 kW crossed the meter.**

    Driven through the production refresh path rather than a stub, because the
    defect was not in the mapping -- it was in which layer was asked.

    *Mutation: point ``_economic_action_value`` back at the plan's ``current_run``
    and this fails: the executing quarter is behind the head by construction.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await step_once(hass, coordinator, live_surface, **step_clock(1))

    assert _economic_action_value(coordinator) == ECONOMIC_ACTION_EXPORT

    # **State and attributes describe the same execution.** beta.34 published
    # ``idle`` beside a window, an energy and a price belonging to a different run.
    attributes = _economic_action_attributes(coordinator)
    assert attributes["owned"] is True
    assert attributes["purpose"] == "export"
    assert attributes["campaign_id"] == CAMPAIGN_ID
    assert attributes["planned_kwh"] is not None
    assert attributes["power_kw"] is not None


def test_economic_action_is_idle_before_a_sale_starts() -> None:
    """Nothing is executing, whatever the plan intends fifteen minutes from now."""
    from .beta34_shape import solve_at

    outcome = solve_at(head=36, end=96, stored=5.0).outcome
    assert _economic_action_value(_Surface(outcome)) == ECONOMIC_ACTION_IDLE


def test_economic_action_shows_the_shadow_intent_and_marks_it_unowned() -> None:
    """**Shadow exists to be watched, so it may not read ``idle`` all day.**

    The product decision for beta.35: publish the Stage-B intent that *would*
    execute, and say plainly that nothing owns it. The alternative -- ``idle``
    whenever unowned -- makes the mode that exists for building confidence the one
    mode in which nothing can be observed.

    The honesty is carried by the attributes, not by the state, so a dashboard
    reading ``export`` in Shadow always has ``owned: false`` beside it.
    """
    from .beta34_shape import solve_at

    outcome = solve_at(head=36, end=96, stored=5.0).outcome
    shadow = _Surface(
        outcome,
        _executing_export(ownership=OWNERSHIP_UNPROVEN, mode=CONTROL_MODE_SHADOW),
    )

    assert _economic_action_value(shadow) == ECONOMIC_ACTION_EXPORT
    attributes = _economic_action_attributes(shadow)
    assert attributes["owned"] is False
    assert attributes["mode"] == CONTROL_MODE_SHADOW


# ===========================================================================
# 2. Next Planned Action -- the future tense, read from the plan
# ===========================================================================


def test_a_run_beginning_at_the_head_is_the_next_planned_action() -> None:
    """**The off-by-one that reported ``charge`` while a Sell was 15 minutes away.**

    The head is ``elapsed + 1``. A run whose ``start_index`` equals it has not
    started -- it begins at the top of the next quarter -- so ``> head`` skipped
    exactly the run the entity exists to announce.

    *Mutation: restore ``upcoming_run`` (``start_index > head``) and this fails.*
    """
    from .beta34_shape import solve_at

    # A head this shape genuinely starts a run at -- chosen rather than hoped for,
    # because a skipped test proves nothing about an off-by-one.
    outcome = solve_at(head=38, end=96, stored=5.0).outcome
    plan = outcome.desired
    head = plan.intervals[0].index
    at_head = [run for run in plan.runs if run.start_index == head]
    assert at_head, "the shape must start a run exactly at the head"
    after = [run for run in plan.runs if run.start_index > head]
    assert after, "and plan something later, or there is no off-by-one to make"

    run, _target = _next_planned_run(_Surface(outcome))
    assert run is not None
    assert run.start_index == head, "the nearest un-started run, not the one after"
    # The beta.34 reading, kept so the difference is visible rather than asserted.
    assert plan.upcoming_run is not None
    assert plan.upcoming_run.start_index == after[0].start_index


def test_next_planned_action_reports_the_earliest_run_at_or_after_the_head() -> None:
    """The nearest un-started campaign wins, whatever its direction.

    The observed misreport was ``charge`` while a Sell stood at the head, so the
    ordering is asserted directly rather than the one symptom.
    """
    from .beta34_shape import solve_at

    outcome = solve_at(head=36, end=96, stored=5.0).outcome
    plan = outcome.desired
    head = plan.intervals[0].index
    ahead = [run for run in plan.runs if run.start_index >= head]
    if not ahead:
        pytest.skip("this shape plans nothing ahead")

    run, _target = _next_planned_run(_Surface(outcome))
    assert run is not None
    assert run.start_index == min(entry.start_index for entry in ahead)
    assert _next_planned_action_value(_Surface(outcome)) in {
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_EXPORT,
        ECONOMIC_ACTION_SAFETY_BUY,
    }


async def test_next_planned_action_advances_past_the_campaign_once_it_starts(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """ "Next" may never mean "now".

    Once a campaign starts, its remaining rows can still appear in the plan.
    Naming them would make the two entities describe the same thing in different
    tenses. Excluded by campaign identity rather than by index, because the
    identity is what survives the head advancing past the executing rows.

    *Mutation: drop the campaign-identity exclusion in ``_next_planned_run`` and
    the entity starts announcing the sale it is already making.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await step_once(hass, coordinator, live_surface, **step_clock(1))

    assert _economic_action_value(coordinator) == ECONOMIC_ACTION_EXPORT
    run, target = _next_planned_run(coordinator)
    assert target.get("campaign_id") != CAMPAIGN_ID
    if run is None:
        assert _next_planned_action_value(coordinator) == ECONOMIC_ACTION_IDLE
        return
    attributes = _next_planned_action_attributes(coordinator)
    assert attributes["campaign_id"] != CAMPAIGN_ID
    # Full instants, so a bare ``19:45-20:30`` can never again mean tomorrow.
    starts_at = attributes["starts_at"]
    assert starts_at is None or "T" in starts_at
