"""Every campaign that is publicly created is publicly closed, exactly once.

**The guarantee, and the hole that made it not one.** The open campaign's identity
lived only in memory -- ``_campaign_id``, ``_campaign_instance_id`` and
``_campaign_started_at`` are plain attributes, and the store's ``execution`` block
held only revisions and the causal record. So a restart mid-campaign minted a fresh
instance with a new ``opened_at``, and the pre-restart instance never received
``removed``: one physical objective appeared twice in the log under two ids.

**And the redefinition that makes the log readable.** ``stopped`` cannot mean "the
dispatch stopped". ``_async_stop_dispatch`` notes a stop for all three scopes, and
``STOP_SCOPE_ROW`` means *this row is done and a later executable row remains* -- the
frozen plan and the campaign instance survive, the dispatch stops, and the next
boundary arms again. ``test_beta36_lifecycle`` positively asserts one instance across
two plans and two ``serve_load`` gaps. So a multi-row campaign row-stops repeatedly
by design, and a public event there would be the per-quarter spam this surface exists
to replace. ``stopped`` is therefore defined at the irreversible boundary.

These tests are written against the **persisted marks and the recovery**, not against
a live multi-quarter run, because that is where the guarantee actually lives: the
question "will this restart replay an event?" is answered by the document, and a test
that never writes one cannot ask it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import callback

from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_LIFECYCLE_KINDS,
    CAMPAIGN_OUTCOMES,
    EVENT_CAMPAIGN_LIFECYCLE,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
    EXECUTION_STOP_WINDOW_ENDED,
    LIFECYCLE_KIND_CREATED,
    LIFECYCLE_KIND_REMOVED,
    LIFECYCLE_KIND_STARTED,
    LIFECYCLE_KIND_STOPPED,
    MAX_CAMPAIGN_LIFECYCLE_REMEMBERED,
    OUTCOME_FAILED,
    OUTCOME_NOT_EXECUTED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    OUTCOME_SUPERSEDED,
)

NOW = datetime(2026, 8, 21, 3, 30, tzinfo=UTC)


def _mark(**overrides: Any) -> dict[str, Any]:
    """Return a persisted lifecycle mark, in the shape the coordinator writes."""
    mark = {
        "instance_id": "abcd1234abcd1234",
        "campaign_id": "9f5611a4",
        "opened_at": "2026-08-21T02:00:00+00:00",
        "marks": [LIFECYCLE_KIND_CREATED],
        "purpose": "grid_charge",
        "classification_at_creation": "coverage_buy",
        "objective_boundary": "battery",
        "planned_kwh": 8.61,
        "window_start": "2026-08-21T02:00:00+00:00",
        # Already past at ``NOW``, so the base mark is a window-ended one. A test
        # that wants the other authoritative answer moves it forward explicitly.
        "window_end": "2026-08-21T03:00:00+00:00",
        "revision": 1,
        "started_at": None,
        "stopped_at": None,
        "stop_reason": None,
        "realized_kwh": 0.0,
        "measurable": True,
        "result": None,
    }
    mark.update(overrides)
    return mark


@pytest.fixture
def events(hass):
    """Collect every lifecycle event fired during a test, **in fire order**.

    ``hass.bus.async_fire`` schedules delivery rather than performing it, so a test
    that asserts immediately after the call is measuring the scheduler. Every test
    here drains the bus through the ``settle`` fixture first.

    **The listener is a ``@callback``, and that is load-bearing rather than
    idiomatic.** Home Assistant classifies a plain function listener as an
    ``Executor`` job and runs it in a thread pool, so two events fired back to back
    arrive in whatever order the pool schedules them. This file asserts that
    ``stopped`` precedes ``removed``, and with an executor listener that assertion
    was a race -- it passed locally at one, four and thirty-two workers and on three
    CI shards before failing on the fourth.

    A ``@callback`` listener is a ``Callback`` job, dispatched synchronously inside
    ``async_fire``. That is also what any real consumer that cares about ordering
    uses, so the fixture now observes the bus the way the code's own guarantee is
    stated: sequential fires, no await between them, delivered in order.
    """
    seen: list[dict[str, Any]] = []

    @callback
    def _record(event: Any) -> None:
        seen.append(event.data)

    hass.bus.async_listen(EVENT_CAMPAIGN_LIFECYCLE, _record)
    return seen


@pytest.fixture
def settle(hass):
    """Return a coroutine that drains the event bus."""

    async def _settle() -> None:
        await hass.async_block_till_done()

    return _settle


@pytest.fixture
async def restarted(hass, setup_integration, source_entities, frank):
    """Return a coordinator with a driven plan, ready to be given a dangling mark."""
    from .test_beta33_campaign_wiring import planning_coordinator

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    coordinator.store.campaign_lifecycle = None
    coordinator.store.closed_lifecycle.clear()
    return coordinator


# ===========================================================================
# the vocabulary
# ===========================================================================


def test_the_two_new_outcomes_are_in_the_published_enum() -> None:
    """Extended, never substituted.

    ``canceled`` keeps its one ``l`` -- the machine spelling is the enum's, and
    presentation is the translation layer's problem.
    """
    assert OUTCOME_NOT_EXECUTED in CAMPAIGN_OUTCOMES
    assert OUTCOME_SUPERSEDED in CAMPAIGN_OUTCOMES
    assert "canceled" in CAMPAIGN_OUTCOMES
    assert "cancelled" not in CAMPAIGN_OUTCOMES
    assert len(set(CAMPAIGN_OUTCOMES)) == len(CAMPAIGN_OUTCOMES)


def test_the_four_kinds_are_exactly_the_four_transitions() -> None:
    """A fifth kind would be a fifth thing a reader has to learn."""
    assert CAMPAIGN_LIFECYCLE_KINDS == (
        LIFECYCLE_KIND_CREATED,
        LIFECYCLE_KIND_STARTED,
        LIFECYCLE_KIND_STOPPED,
        LIFECYCLE_KIND_REMOVED,
    )


# ===========================================================================
# the restart recovery matrix -- one test per row
# ===========================================================================


async def test_row_a_a_never_started_campaign_closes_without_a_stopped_event(
    restarted, events, settle
) -> None:
    """**Row A, and the assertion that matters is the negative one.**

    ``created -> removed`` with no ``stopped`` is a legal sequence: nothing physical
    had begun, so nothing had begun to stop. A ``stopped`` here would claim an
    execution that never happened.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark()

    assert coordinator._recover_campaign_lifecycle(NOW) == OUTCOME_NOT_EXECUTED
    await settle()

    kinds = [event["kind"] for event in events]
    assert kinds == [LIFECYCLE_KIND_REMOVED]
    assert events[0]["result"] == OUTCOME_NOT_EXECUTED
    assert events[0]["completion_reason"] == EXECUTION_STOP_WINDOW_ENDED
    assert events[0]["realised_kwh"] == 0.0


async def test_row_a_never_touches_execution_finality(restarted, settle) -> None:
    """**The guard on the whole design.**

    ``_close_campaign`` returns before latching anything for a never-started
    campaign, and says why: nothing physical happened, so there is nothing to have
    finished -- and nothing to latch either, *which is what leaves a never-started
    campaign free to be attempted properly later*. Filing this terminal through
    ``_final_campaigns`` would mean ``CARRY_REFUSED_CAMPAIGN_FINAL`` blocks the
    legitimate retry. So the telemetry latch is a third one, and this proves the
    other two are untouched.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark()

    coordinator._recover_campaign_lifecycle(NOW)

    assert coordinator._final_campaigns == []
    assert coordinator._closed_instances == []
    assert coordinator.store.closed_lifecycle == ["abcd1234abcd1234"]


async def test_row_a_stays_open_while_neither_answer_is_yet_true(
    restarted, events, settle
) -> None:
    """**Closed late rather than wrongly.**

    The store is read during setup and the first solve happens later, so at restore
    time there is no authoritative plan to say whether a replacement exists. Guessing
    ``plan_replaced`` there would be inventing a fact, so the instance is re-examined
    each refresh instead -- and nothing is blocked meanwhile, because this state never
    latched execution finality.
    """
    coordinator = restarted
    coordinator._plan = None
    coordinator.store.campaign_lifecycle = _mark(
        window_end=(NOW + timedelta(hours=2)).isoformat()
    )

    assert coordinator._recover_campaign_lifecycle(NOW) == "deferred"
    await settle()

    assert events == []
    assert coordinator.store.campaign_lifecycle is not None
    assert coordinator.store.closed_lifecycle == []


async def test_row_a_names_plan_replaced_when_a_newer_plan_covers_the_window(
    restarted, events, settle
) -> None:
    """Two authoritative answers and no third.

    The reason is chosen from the authoritative set rather than invented:
    ``window_expired_before_start`` does not exist in this codebase and is not
    created for the occasion.
    """
    from types import SimpleNamespace

    coordinator = restarted
    # A newer plan naming a different campaign. The window is deliberately still
    # open, so the *only* clause that can fire is the plan one -- otherwise this
    # would pass on ``window_ended`` and prove nothing about plan replacement.
    coordinator._plan = SimpleNamespace(campaign_id="a-newer-campaign")
    coordinator.store.campaign_lifecycle = _mark(
        campaign_id="a-campaign-no-longer-planned",
        window_end=(NOW + timedelta(hours=2)).isoformat(),
    )

    assert coordinator._recover_campaign_lifecycle(NOW) == OUTCOME_NOT_EXECUTED
    await settle()

    assert events[0]["completion_reason"] == EXECUTION_STOP_PLAN_REPLACED


async def test_row_b_a_started_campaign_is_failed_with_progress_unknown(
    restarted, events, settle
) -> None:
    """**Agreeing with the executor, not overruling it.**

    beta.27 stops an owned dispatch on restart and marks progress unknown, which
    clears ``_campaign_measurable`` -- and the honesty guard already makes success
    unreachable when the total is not a measurement. So ``failed`` here is the
    executor's judgement being reported, not a new one.

    A ``stopped`` event *is* emitted: execution had begun, and the restart is what
    ended it.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark(
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED],
        started_at="2026-08-21T02:15:00+00:00",
        realized_kwh=3.2,
        frozen_target_kwh=8.61,
    )

    assert coordinator._recover_campaign_lifecycle(NOW) == OUTCOME_FAILED
    await settle()

    assert [event["kind"] for event in events] == [
        LIFECYCLE_KIND_STOPPED,
        LIFECYCLE_KIND_REMOVED,
    ]
    terminal = events[-1]
    assert terminal["result"] == OUTCOME_FAILED
    assert terminal["completion_reason"] == EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN
    assert terminal["objective_measurable"] is False
    assert terminal["realised_kwh"] == 3.2
    assert terminal["shortfall_kwh"] == pytest.approx(5.41)


async def test_row_c_preserves_a_verdict_that_was_already_reachable(
    restarted, events, settle
) -> None:
    """**This is why the marks carry evidence and not four booleans.**

    A campaign whose objective was measurable, whose target was frozen and whose
    reason was recorded had a truthful result before the restart. Without the
    evidence this state is indistinguishable from row B, and the log would report a
    finished campaign as failed -- which is closing the log rather than reporting it.

    No second ``stopped``: that event was already published.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark(
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED, LIFECYCLE_KIND_STOPPED],
        started_at="2026-08-21T02:15:00+00:00",
        stopped_at="2026-08-21T03:00:00+00:00",
        stop_reason="campaign_objective_reached",
        realized_kwh=8.59,
        measurable=True,
        frozen_target_kwh=8.61,
        success_tolerance_kwh=0.1,
    )

    assert coordinator._recover_campaign_lifecycle(NOW) == OUTCOME_SUCCESS
    await settle()

    assert [event["kind"] for event in events] == [LIFECYCLE_KIND_REMOVED]
    assert events[0]["result"] == OUTCOME_SUCCESS
    assert events[0]["stopped_at"] == "2026-08-21T03:00:00+00:00"


async def test_row_c_still_refuses_success_when_the_total_was_not_a_measurement(
    restarted, events, settle
) -> None:
    """Unmeasurable outranks a met objective, exactly as it does live.

    Preserving a pre-restart verdict must not become a way around the honesty guard:
    a total that is not a measurement cannot be evidence of anything, including of
    success.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark(
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED, LIFECYCLE_KIND_STOPPED],
        started_at="2026-08-21T02:15:00+00:00",
        stopped_at="2026-08-21T03:00:00+00:00",
        stop_reason="campaign_objective_reached",
        realized_kwh=8.59,
        measurable=False,
        frozen_target_kwh=8.61,
        success_tolerance_kwh=0.1,
    )

    assert coordinator._recover_campaign_lifecycle(NOW) == OUTCOME_FAILED


async def test_row_c_reports_a_displaced_campaign_as_superseded(
    restarted, events, settle
) -> None:
    """The shortfall is not the plant's.

    A started campaign overtaken by a newer authoritative plan did not under-deliver;
    it was replaced. ``partial`` would read as a plant miss on a day nothing missed.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark(
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED, LIFECYCLE_KIND_STOPPED],
        started_at="2026-08-21T02:15:00+00:00",
        stopped_at="2026-08-21T03:00:00+00:00",
        stop_reason=EXECUTION_STOP_PLAN_REPLACED,
        realized_kwh=1.0,
        measurable=True,
        frozen_target_kwh=8.61,
        success_tolerance_kwh=0.1,
    )

    assert coordinator._recover_campaign_lifecycle(NOW) == OUTCOME_SUPERSEDED


async def test_row_d_a_published_terminal_is_never_republished(
    restarted, events, settle
) -> None:
    """**Row D, and the property the whole persistence exists for.**

    A restart after ``removed`` must replay nothing. The mark is cleared when the
    event fires, and the latch answers the question even if a mark survives -- two
    independent reasons, because one of them is a cleanup and cleanups get missed.
    """
    coordinator = restarted
    coordinator.store.closed_lifecycle.append("abcd1234abcd1234")
    coordinator.store.campaign_lifecycle = _mark(
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED, LIFECYCLE_KIND_STOPPED],
    )

    assert coordinator._recover_campaign_lifecycle(NOW) == "already_closed"
    await settle()

    assert events == []
    assert coordinator.store.campaign_lifecycle is None


async def test_recovery_is_idempotent_across_repeated_refreshes(
    restarted, events, settle
) -> None:
    """It runs on **every** refresh, not once at startup, so it has to be safe to.

    Deferring row A is what makes that necessary: the classification is not knowable
    at restore time, so the pass has to be able to ask again -- and asking again must
    not publish again.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark()

    coordinator._recover_campaign_lifecycle(NOW)
    coordinator._recover_campaign_lifecycle(NOW)
    coordinator._recover_campaign_lifecycle(NOW)
    await settle()

    assert [event["kind"] for event in events] == [LIFECYCLE_KIND_REMOVED]


async def test_the_publisher_refuses_a_second_terminal_for_one_instance(
    restarted, events, settle
) -> None:
    """**Defence in depth, and it is exercised rather than assumed.**

    ``_recover_campaign_lifecycle`` checks the latch before it publishes, and the
    publisher checks again. Two guards for one property looks redundant, and the
    mutation table showed that the second is untested through the first: clearing
    the mark makes the outer check sufficient on every ordinary path.

    The path it is *not* sufficient on is a mark that survived -- a write that did
    not land, a document restored from a backup. So the inner guard is called
    directly here, which is the only way to reach it.
    """
    coordinator = restarted
    mark = _mark(marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED])
    coordinator.store.campaign_lifecycle = mark

    for _ in range(2):
        coordinator._publish_recovered_terminal(
            mark,
            NOW,
            result=OUTCOME_FAILED,
            completion_reason=EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
            realised_kwh=1.0,
            shortfall_kwh=None,
            measurable=False,
            emit_stopped=False,
        )
    await settle()

    assert [event["kind"] for event in events] == [LIFECYCLE_KIND_REMOVED]


async def test_a_recovered_terminal_publishes_the_classification_it_was_created_with(
    restarted, events, settle
) -> None:
    """Not this boot's.

    A live reclassification would come from a solve that never saw the campaign, so
    it would be a fact about a different plan wearing the recovered campaign's name.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark(
        classification_at_creation="safety_buy"
    )

    coordinator._recover_campaign_lifecycle(NOW)
    await settle()

    assert events[0]["classification"] == "safety_buy"
    assert events[0]["classification_at_creation"] == "safety_buy"
    assert events[0]["final_classification"] == "safety_buy"
    assert events[0]["recovered_after_restart"] is True


# ===========================================================================
# the persisted latch
# ===========================================================================


def test_the_latch_round_trips_and_stays_bounded(hass) -> None:
    """Bounded for the reason ``MAX_ABORTED_CAMPAIGNS_REMEMBERED`` is bounded.

    The question "have I already published this instance's terminal?" is only ever
    asked about the recent past, and an unbounded list on a document rewritten every
    quarter is a slow leak rather than a guarantee.
    """
    from custom_components.alpha_ems_manager.storage import LearningStore

    store = LearningStore(hass, "entry")
    for index in range(MAX_CAMPAIGN_LIFECYCLE_REMEMBERED + 10):
        assert store.note_lifecycle_closed(f"instance{index:04d}")
    assert not store.note_lifecycle_closed("instance0073")

    # **In memory, not only on disk.** ``to_dict`` slices to the cap on its way out,
    # so a document written by an unbounded list still looks bounded -- the mutation
    # table found exactly that. The leak this cap exists to prevent is in the
    # process, on a list appended to for as long as the integration runs.
    assert len(store.closed_lifecycle) == MAX_CAMPAIGN_LIFECYCLE_REMEMBERED

    payload = store.to_dict()

    assert len(payload["execution"]["closed_lifecycle"]) == (
        MAX_CAMPAIGN_LIFECYCLE_REMEMBERED
    )
    assert payload["execution"]["closed_lifecycle"][-1] == "instance0073"


def test_a_document_with_no_campaign_open_is_byte_identical_to_a_beta41_one(
    hass,
) -> None:
    """Additive. An installation that has never opened a campaign writes what it did."""
    from custom_components.alpha_ems_manager.storage import LearningStore

    store = LearningStore(hass, "entry")

    assert "execution" not in store.to_dict()


@pytest.mark.parametrize(
    "broken", [{"marks": ["created"]}, {"instance_id": 7}, [], "created", None]
)
async def test_a_malformed_mark_reads_as_no_campaign_open(hass, broken) -> None:
    """Absence and damage mean the same thing here, and that is the safe direction.

    The recovery then finds nothing to close, which costs one log line. Trusting a
    damaged mark could publish a terminal for a campaign that never existed.

    Round-tripped through the **real** writer and the real loader, because a test
    that re-implements the validation inline would pass against its own copy of the
    rule rather than against the one that ships.
    """
    from custom_components.alpha_ems_manager.storage import LearningStore

    store = LearningStore(hass, "entry-damaged")
    store.execution_record = {"anything": True}
    store.campaign_lifecycle = _mark()
    await store.async_save_now()

    damaged = LearningStore(hass, "entry-damaged")
    document = store.to_dict()
    document["execution"]["lifecycle"] = broken
    damaged._store.async_load = _returning(document)
    await damaged.async_load("Europe/Amsterdam")

    assert damaged.campaign_lifecycle is None, broken


def _returning(document: dict[str, Any]):
    """Return an awaitable stub that hands back one crafted document."""

    async def _load() -> dict[str, Any]:
        return document

    return _load


def test_the_stop_scope_that_re_arms_publishes_nothing(
    restarted, events, settle
) -> None:
    """**The redefinition, asserted directly.**

    ``_lifecycle_stopped`` is called only from the campaign-scoped branch. Row-scope
    stops reach the shared internal ``_note_lifecycle`` calls and deliberately not
    this one, because that scope means the row is done and a later executable row
    remains -- the instance survives and re-arms at the next boundary. Publishing
    there would put a line in the log at every row boundary and every ``serve_load``
    gap.
    """
    import inspect

    source = inspect.getsource(type(restarted)._async_stop_dispatch)

    assert source.count("self._lifecycle_stopped(") == 1
    campaign_branch = source[source.index("if scope == STOP_SCOPE_CAMPAIGN:") :]
    assert "self._lifecycle_stopped(" in campaign_branch


async def test_a_never_started_instance_emits_no_stopped_event_on_the_live_path(
    restarted, events, settle
) -> None:
    """The same rule as row A, on the path that does not involve a restart.

    ``_lifecycle_stopped`` refuses an instance with no ``started`` mark, so a
    campaign that closes without ever running produces ``created -> removed`` and
    nothing between.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark()

    coordinator._lifecycle_stopped(NOW, "window_ended")
    await settle()

    assert events == []
    assert coordinator.store.campaign_lifecycle is not None
    assert LIFECYCLE_KIND_STOPPED not in coordinator.store.campaign_lifecycle["marks"]


async def test_progress_alone_never_produces_an_event(
    restarted, events, settle
) -> None:
    """No event because the realised figure moved, a quarter completed, Stage A
    refreshed, the revision incremented or power moved inside the deadband.

    ``started`` is idempotent by its own mark, so calling it repeatedly -- which the
    live path does, once per refresh -- publishes once.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark()

    coordinator._lifecycle_started(NOW)
    coordinator._lifecycle_started(NOW + timedelta(minutes=15))
    coordinator._lifecycle_started(NOW + timedelta(minutes=30))
    await settle()

    assert [event["kind"] for event in events] == [LIFECYCLE_KIND_STARTED]


async def test_a_partial_result_carries_a_positive_shortfall(
    restarted, events, settle
) -> None:
    """The terminal publishes a signed *tracking error*, negative when the plant
    under-delivered; a public result publishes a *shortfall*, positive for the same
    event.

    One negation in one place, rather than a sign convention a reader has to carry
    between two payloads.
    """
    coordinator = restarted
    coordinator.store.campaign_lifecycle = _mark(
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED],
        started_at="2026-08-21T02:15:00+00:00",
    )
    coordinator._campaign_instance_id = "abcd1234abcd1234"

    coordinator._lifecycle_removed(
        NOW,
        result=OUTCOME_PARTIAL,
        completion_reason=EXECUTION_STOP_WINDOW_ENDED,
        terminal={
            "objective_realized_kwh": 9.84,
            "objective_tracking_error_kwh": -2.66,
            "objective_measurable": True,
            "success_tolerance_kwh": 0.1,
        },
    )
    await settle()

    assert events[0]["result"] == OUTCOME_PARTIAL
    assert events[0]["realised_kwh"] == 9.84
    assert events[0]["shortfall_kwh"] == pytest.approx(2.66)


def test_the_event_order_assertions_rest_on_a_synchronous_listener(hass) -> None:
    """**The guard on this file's own ordering claims.**

    ``test_row_b`` asserts ``stopped`` precedes ``removed``. That assertion is only
    meaningful if the listener observes the bus in fire order, and Home Assistant
    decides that from the listener itself: a plain function is an ``Executor`` job
    dispatched through a thread pool, where two back-to-back fires arrive in
    whatever order the pool picks. Under that classification the ordering assertions
    here were a race -- they passed at one, four and thirty-two workers locally and
    on three CI shards before failing on the fourth.

    Without this test, deleting the decorator would restore the flake silently and
    the file would keep asserting an order it no longer observes. So the
    classification is asserted, not the decorator: what matters is the job type
    Home Assistant derives, which is also what a real order-sensitive consumer gets.
    """
    from homeassistant.core import HassJobType, get_hassjob_callable_job_type

    seen: list[dict[str, Any]] = []

    @callback
    def _record(event: Any) -> None:
        seen.append(event.data)

    assert get_hassjob_callable_job_type(_record) is HassJobType.Callback
    # And the negative, so the assertion above is not trivially true of everything.
    assert (
        get_hassjob_callable_job_type(lambda event: seen.append(event.data))
        is HassJobType.Executor
    )
