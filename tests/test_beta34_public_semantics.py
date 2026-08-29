"""beta.34: the two public entities answer the question they are named for.

Both were reading true and describing something else.

**Economic Action** published ``desired.published_run``, which is ``current_run
or next_run`` with no bound on how far ``next_run`` may be. At 14:00 on
2026-08-29 tomorrow's prices arrived, the horizon grew from 40 intervals to 135,
and the entity announced ``export`` for a sale planned at 20:30 the *following*
evening. Its ``window`` attribute renders ``HH:MM-HH:MM`` with no date, so
nothing in the reading revealed it.

**Control State** was set to ``executed`` by the generic "the staged write did
not raise" branch, which every successful write reaches -- including a stale
ownership-marker release whose entire command list is one
``input_boolean.turn_off``. The English label for that value is *Executing*. So
at 14:00, with the dispatch off, the timer inactive and one boolean written, the
dashboard read **Executing**, beside ``applied_kw: 0.9, executed: true``.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    DISPATCH_ENABLE,
    DISPATCH_MODE_SOC_CONTROL,
    DISPATCH_POWER,
    plan_dispatch_arm,
    plan_release_marker,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_STATE_EXECUTED,
    CONTROL_STATE_EXECUTING,
    CONTROL_STATE_IDLE,
    CONTROL_STATE_OPTIONS,
    ECONOMIC_ACTION_IDLE,
    ECONOMIC_ACTION_OPTIONS,
    SENSOR_NEXT_PLANNED_ACTION,
)

from .test_beta33_campaign_wiring import planning_coordinator

pytestmark = pytest.mark.usefixtures("control_surface")


# ===========================================================================
# 1. Economic Action is the present tense
# ===========================================================================


def test_the_action_entity_reads_the_current_run_and_nothing_further() -> None:
    """**The whole of the fix, on the property that decides it.**

    ``current_action`` may only ever consult ``current_run`` -- the run starting
    at the horizon's *head*. ``published_run`` is what reached forward, and it is
    still there for the planned-action entity, which prints instants.

    *Mutation: point ``_economic_action_value`` back at ``outcome.action`` and
    the tomorrow-only case below reports ``export`` instead of ``idle``.*
    """
    import inspect
    import textwrap

    from custom_components.alpha_ems_manager import economic as module

    source = textwrap.dedent(
        inspect.getsource(module.EconomicOutcome.current_action.fget)
    )
    # The body only, so the docstring's account of the defect does not satisfy
    # the assertion that the defect is gone.
    body = source.split('"""')[-1]
    assert "self.desired.current_run" in body
    assert "published_run" not in body
    assert "next_run" not in body
    assert ECONOMIC_ACTION_IDLE in ECONOMIC_ACTION_OPTIONS


class _Published:
    """A coordinator stand-in carrying one real solved outcome.

    The outcome comes from the production solver over the reconstructed
    2026-08-29 shape; only the container is a stub, because
    ``_economic_action_value`` reads exactly one thing.
    """

    def __init__(self, outcome) -> None:
        self.data = {"economic": outcome}


def test_a_plan_that_starts_later_reads_idle_now() -> None:
    """**The 14:00 misreport, on the value function that produced it.**

    A horizon whose first run begins after the head. ``outcome.action`` -- what
    the entity used to publish -- says ``charge``, because ``published_run`` falls
    through to the next run however distant it is. Nothing is happening.

    *Mutation: point ``_economic_action_value`` back at ``outcome.action`` and
    this fails.*
    """
    from custom_components.alpha_ems_manager.sensor import _economic_action_value

    from .beta34_shape import solve_at

    outcome = solve_at(head=36, end=96, stored=5.0).outcome
    assert outcome.desired.current_run is None, "the shape must have nothing running"
    assert outcome.desired.upcoming_run is not None, "and something planned"
    # The old reading, kept so the difference is visible rather than asserted.
    assert outcome.action == "charge"

    assert _economic_action_value(_Published(outcome)) == ECONOMIC_ACTION_IDLE


def test_a_run_in_progress_still_reads_as_itself() -> None:
    """The other direction: present-tense must not mean silent.

    A charge that is genuinely happening in this interval reports ``charge``, so
    the fix narrows what the entity claims without taking away what it says.
    """
    from custom_components.alpha_ems_manager.sensor import _economic_action_value

    from .beta34_shape import solve_at

    outcome = solve_at(head=52, end=96, stored=11.0).outcome
    assert outcome.desired.current_run is not None

    assert _economic_action_value(_Published(outcome)) == "charge"


def test_idle_is_not_a_synonym_for_hold() -> None:
    """Two different statements, and beta.33 had a word for only one of them.

    ``hold`` is a verdict -- the optimiser weighed the prices and chose to do
    nothing. ``idle`` says only that no run occupies the present interval, which
    is a weaker claim and the honest one when the plan is busy tomorrow.
    """
    from custom_components.alpha_ems_manager.const import ECONOMIC_ACTION_HOLD

    assert ECONOMIC_ACTION_IDLE != ECONOMIC_ACTION_HOLD
    assert ECONOMIC_ACTION_HOLD in ECONOMIC_ACTION_OPTIONS
    # Additive: every value beta.33 could publish still exists.
    for value in ("hold", "charge", "discharge", "export", "curtail_pv", "safety_buy"):
        assert value in ECONOMIC_ACTION_OPTIONS


def test_the_upcoming_run_starts_strictly_after_the_head() -> None:
    """``next_run`` tests ``start_index > 0`` -- an absolute index.

    So a run beginning *at* the head satisfies it, and the property returns the
    run in progress while calling it the next one. Harmless inside
    ``published_run``, where ``current_run`` is consulted first and wins; wrong
    anywhere the question really is "what comes after this". ``next_run`` is left
    exactly as it was, so nothing reading it changes behaviour.
    """
    import inspect
    import textwrap

    from custom_components.alpha_ems_manager import economic as module

    upcoming = textwrap.dedent(inspect.getsource(module.EconomicPlan.upcoming_run.fget))
    assert "run.start_index > head" in upcoming
    original = textwrap.dedent(inspect.getsource(module.EconomicPlan.next_run.fget))
    assert "run.start_index > 0" in original


async def test_the_planned_action_entity_exists_and_prints_instants(
    hass: HomeAssistant, setup_integration
) -> None:
    """**Why it is a second entity rather than a second attribute.**

    A clock window with no date was adequate while the horizon ended at midnight
    and became a misreport the moment the two-day horizon shipped. This entity
    exists to carry full instants, so a reader cannot mistake tomorrow for today.
    """
    state = hass.states.get("sensor.alpha_ems_next_planned_action")
    assert state is not None, "the entity must be registered"
    assert SENSOR_NEXT_PLANNED_ACTION == "next_planned_action"
    assert state.attributes["device_class"] == "enum"
    # The same vocabulary as the action beside it: a reader comparing "now"
    # against "next" is comparing two answers to one question.
    assert set(state.attributes["options"]) == set(ECONOMIC_ACTION_OPTIONS)


async def test_the_planned_action_prints_full_instants(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """**Why it is a second entity rather than a second attribute.**

    A clock window with no date was adequate while the horizon ended at midnight
    and became a misreport the moment the two-day horizon shipped: a live reading
    of "20:30-22:00" taken on 2026-08-29 was describing 2026-08-30. This entity
    carries instants, so a reader cannot make that mistake.
    """
    from custom_components.alpha_ems_manager.sensor import (
        _next_planned_action_attributes,
        _next_planned_action_value,
    )

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    attributes = _next_planned_action_attributes(coordinator)
    value = _next_planned_action_value(coordinator)

    assert value in ECONOMIC_ACTION_OPTIONS
    for name in ("starts_at", "ends_at", "energy_kwh"):
        assert name in attributes, name
    starts = attributes["starts_at"]
    # A shape with no upcoming run would make every assertion below vacuous, so
    # the fixture is required to produce one rather than allowed to skip.
    assert starts is not None, (value, attributes)
    assert "T" in starts, starts
    assert starts > "2000-01-01", starts
    assert attributes["ends_at"] > starts
    # And the campaign it belongs to, so a line and an entity can be tied
    # together without either re-deriving the calendar.
    assert "campaign_id" in attributes


# ===========================================================================
# 2. Control State is the present tense
# ===========================================================================


def test_executing_is_published_and_executed_is_not() -> None:
    """The value the coordinator writes, and the one it keeps only for readers.

    ``executed`` stays in the enum so a dashboard built against beta.33 keeps
    every value it could already match on, and stays in the payload as
    ``execution.result.command_result`` where "did the last write succeed" is
    exactly the question being asked. It is no longer a *state*.

    *Mutation: restore ``report["state"] = CONTROL_STATE_EXECUTED`` unconditionally
    and the marker-release case below reads ``executed`` again.*
    """
    import inspect
    import textwrap

    from custom_components.alpha_ems_manager import coordinator as module

    source = textwrap.dedent(
        inspect.getsource(module.AlphaEmsCoordinator._async_dispatch)
    )
    assert "_mark_command_result(report, CONTROL_STATE_EXECUTED)" in source
    assert 'report["state"] = CONTROL_STATE_EXECUTING' in source
    assert 'report["state"] = CONTROL_STATE_EXECUTED' not in source

    assert CONTROL_STATE_EXECUTING in CONTROL_STATE_OPTIONS
    assert CONTROL_STATE_EXECUTED in CONTROL_STATE_OPTIONS


def test_a_marker_release_is_not_a_battery_command() -> None:
    """One ``input_boolean.turn_off`` is not the inverter doing anything.

    The condition the coordinator applies is "this refresh is not a stop **and**
    it either carries an activation or writes a power setpoint". A marker release
    is neither, which is what keeps it out of ``executing``.
    """
    release = plan_release_marker()
    assert release, "the release must actually be a command list"
    assert all(step.entity_id != DISPATCH_POWER for step in release)
    assert all(step.entity_id != DISPATCH_ENABLE for step in release)

    # And a real arm is both.
    arm = plan_dispatch_arm(
        mode=DISPATCH_MODE_SOC_CONTROL,
        power_kw=-1.0,
        cutoff_soc_percent=21,
        duration_minutes=20,
        pv_enabled=True,
    )
    assert any(step.entity_id == DISPATCH_POWER for step in arm)
    assert any(step.entity_id == DISPATCH_ENABLE for step in arm)


def test_a_stop_reports_idle_rather_than_planned() -> None:
    """``eligible`` renders as *Planned*, which a stop is the opposite of.

    After a stop or a stale-marker release nothing is running and nothing is
    queued. The steps that were sent are in ``commands`` for anyone who needs
    them; the state says what is happening, which is nothing.
    """
    import inspect
    import textwrap

    from custom_components.alpha_ems_manager import coordinator as module

    source = textwrap.dedent(
        inspect.getsource(module.AlphaEmsCoordinator._build_control_report)
    )
    # The stop branch: idle outright, with no eligibility question asked.
    stop_branch = source[source.index("if resetting or releasing:") :]
    stop_branch = stop_branch[: stop_branch.index("elif ")]
    assert "state = CONTROL_STATE_IDLE" in stop_branch
    assert "CONTROL_STATE_ELIGIBLE" not in stop_branch
    # The eligibility branch below it is untouched: a safe refresh with a command
    # it may not send is still *Planned*, which is what that word is for.
    assert "CONTROL_STATE_ELIGIBLE if commands else CONTROL_STATE_IDLE" in source
    assert CONTROL_STATE_IDLE in CONTROL_STATE_OPTIONS


def test_the_applied_power_describes_a_command_that_was_issued() -> None:
    """``applied_kw: 0.9, executed: true`` while the dispatch was off.

    Every successful staged write reached the marker, including one whose entire
    command list is a boolean. A figure describing a command that was not issued
    is worse than no figure.

    *Mutation: remove the ``DISPATCH_POWER in commands`` condition and this
    fails.*
    """
    import inspect
    import textwrap

    from custom_components.alpha_ems_manager import coordinator as module

    source = textwrap.dedent(
        inspect.getsource(module.AlphaEmsCoordinator._async_dispatch)
    )
    marker = "if any(step.entity_id == DISPATCH_POWER for step in commands):"
    assert marker in source
    applied = "_mark_execution_applied(report, self._pending_power_kw)"
    assert applied in source
    assert source.index(marker) < source.index(applied)
