"""beta.45: the plan, published before the campaign, and fenced off from it.

Two things are pinned here.

**The overlap predicate, both halves and strict.** Announcement continuity cannot be
``campaign_id`` equality, because that identifier is a digest of the campaign's end
and the end moves: on the 2026-09-06 capture one live charge was published as
``af82a579ac6a803a`` (end 15:00Z) and, ninety minutes later, as
``c9d9217306560d3a`` (end 14:45Z) -- the same campaign, because the DP ends a charge
earlier as the pack fills. So continuity is *same purpose and the windows genuinely
overlap*, and the one-sided form the executor uses for runs is not enough: it would
match a campaign lying entirely in the past.

**The accounting fence.** An announcement exists before any instance is minted, so it
can end without a campaign ever having existed. Everything about that ending is
publication-only, and the tests below check the boundary at the payload, at the
runtime state and at the source, because a fence that holds by convention is not one.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_BOUNDARY_BATTERY,
    CAMPAIGN_BOUNDARY_METER,
    ECONOMIC_ANNOUNCE_LEAD_MINUTES,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_SERVE_LOAD,
    LIFECYCLE_KIND_PLAN_CLOSED,
    LIFECYCLE_KIND_PLANNED,
    LIFECYCLE_KIND_REMOVED,
    OUTCOME_NOT_EXECUTED,
    OUTCOME_SUPERSEDED,
    PLAN_CLOSED_RESULTS,
)
from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

NOW = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def _at(**kwargs: int) -> datetime:
    """Return an instant on the capture's own day."""
    return NOW.replace(**kwargs)


class _Store:
    """The two persisted fields the announcement may touch, and nothing else.

    ``note_lifecycle_closed`` and ``lifecycle_closed`` raise rather than return: the
    announcement path must never reach the exactly-once latch, and a latch that
    quietly answered would let that regression pass.
    """

    def __init__(self, announcement=None, lifecycle=None):
        self.campaign_announcement = announcement
        self.campaign_lifecycle = lifecycle
        self.saves = 0

    def schedule_save(self) -> None:
        self.saves += 1

    def note_lifecycle_closed(self, instance_id):  # pragma: no cover - must not run
        raise AssertionError("an announcement latched a campaign instance")

    def lifecycle_closed(self, instance_id):  # pragma: no cover - must not run
        raise AssertionError("an announcement consulted the terminal latch")


class _Rig:
    """A coordinator whose ``__init__`` never ran, with only what this path reads."""

    def __init__(
        self,
        *,
        targets: tuple[dict[str, Any], ...] = (),
        announcement: dict[str, Any] | None = None,
        lifecycle: dict[str, Any] | None = None,
        campaign_id: str | None = None,
        opened_at: datetime | None = None,
        planned_end: datetime | None = None,
    ) -> None:
        self.c = object.__new__(AlphaEmsCoordinator)
        self.c.execution_targets = tuple(targets)
        self.c._campaign_classifications = {}
        self.c._campaign_id = campaign_id
        self.c._campaign_opened_at = opened_at
        self.c._campaign_planned_end_utc = planned_end
        self.c._campaign_end_utc = None
        # Sentinels: the settlement surfaces an announcement may never write.
        self.c._last_campaign_result = "UNTOUCHED"
        self.c._closed_campaign = "UNTOUCHED"
        self.store = _Store(announcement, lifecycle)
        self.c.store = self.store
        self.fired: list[dict[str, Any]] = []
        self.c.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda event, data: self.fired.append(data))
        )

    @property
    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.fired]

    def announce(self, now: datetime = NOW) -> None:
        self.c._note_campaign_announcement(now)


def _target(
    campaign_id: str,
    intent: str,
    start: datetime,
    campaign_end: datetime,
    kwh: float = 2.0,
) -> dict[str, Any]:
    """Return one published execution target, in the shape Stage A writes."""
    return {
        "campaign_id": campaign_id,
        "intent": intent,
        "purpose": "export" if intent == EXECUTION_INTENT_NET_EXPORT else "charge",
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(minutes=15)).isoformat(),
        "campaign_end": campaign_end.isoformat(),
        "battery_target_kwh": kwh,
        "grid_target_kwh": kwh,
    }


def _window(purpose: str, start: datetime, end: datetime) -> dict[str, Any]:
    return {"purpose": purpose, "window_start": start, "window_end": end}


def _executable_source(method: Any) -> str:
    """Return a method's source with its docstring and comments removed.

    **The prose has to go before the scan, or the scan reads the prose.** These
    docstrings name the very symbols the fence forbids, in the course of explaining
    why they are forbidden -- so a raw text search finds them and reports the
    explanation as the violation.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    function = tree.body[0]
    body = getattr(function, "body", [])
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        function.body = body[1:]
    # ``unparse`` re-emits code alone: comments never survive the parse.
    return ast.unparse(tree)


def _holder() -> AlphaEmsCoordinator:
    return object.__new__(AlphaEmsCoordinator)


def _continues(published: dict[str, Any], announced: dict[str, Any]) -> bool:
    return _holder()._announcement_continues(published, announced)


# ===========================================================================
# the overlap predicate
# ===========================================================================

ANNOUNCED = _window(
    EXECUTION_INTENT_GRID_CHARGE, _at(hour=12, minute=0), _at(hour=14, minute=0)
)


def test_a_moving_head_still_continues_the_same_announcement() -> None:
    """**The head advances every refresh, by design.**

    The campaign's start is clipped to the horizon's head, so the same plan is
    republished starting later and later. That is not a new plan.
    """
    published = _window(
        EXECUTION_INTENT_GRID_CHARGE, _at(hour=13, minute=0), _at(hour=14, minute=0)
    )
    assert _continues(published, ANNOUNCED)


def test_a_moving_tail_still_continues_the_same_announcement() -> None:
    """**The live shape, and the one ``campaign_id`` equality gets wrong.**

    15:00Z became 14:45Z on the capture because the DP ends a charge earlier as the
    pack fills. Same campaign, different digest.

    *Mutation: key continuity on ``campaign_id`` and this fails.*
    """
    published = _window(
        EXECUTION_INTENT_GRID_CHARGE, _at(hour=12, minute=0), _at(hour=13, minute=45)
    )
    assert _continues(published, ANNOUNCED)


def test_a_campaign_entirely_before_the_announcement_is_not_the_same() -> None:
    """**The half a one-sided test would miss.**

    ``published.window_start <= announced.window_end`` alone is satisfied by every
    campaign in the past, which is exactly what an announcement that persists across
    refreshes will meet.

    *Mutation: drop ``announced.window_start < published.window_end`` and this
    fails.*
    """
    published = _window(
        EXECUTION_INTENT_GRID_CHARGE, _at(hour=9, minute=0), _at(hour=11, minute=0)
    )
    assert not _continues(published, ANNOUNCED)


def test_a_campaign_entirely_after_the_announcement_is_not_the_same() -> None:
    """*Mutation: drop ``published.window_start < announced.window_end``.*"""
    published = _window(
        EXECUTION_INTENT_GRID_CHARGE, _at(hour=15, minute=0), _at(hour=16, minute=0)
    )
    assert not _continues(published, ANNOUNCED)


def test_abutting_windows_do_not_continue_one_another() -> None:
    """**Half-open ``[start, end)``, decided rather than stumbled into.**

    ``campaigns_from`` groups contiguous same-run-state intervals into *one*
    campaign, so two abutting same-purpose campaigns cannot come from one solve.
    Across solves an abutting publication is a genuinely new campaign.

    *Mutation: relax either comparison to ``<=`` and both halves of this fail.*
    """
    after = _window(
        EXECUTION_INTENT_GRID_CHARGE, _at(hour=14, minute=0), _at(hour=16, minute=0)
    )
    before = _window(
        EXECUTION_INTENT_GRID_CHARGE, _at(hour=10, minute=0), _at(hour=12, minute=0)
    )
    assert not _continues(after, ANNOUNCED), "starts exactly where the plan ends"
    assert not _continues(before, ANNOUNCED), "ends exactly where the plan starts"


def test_the_same_window_with_a_different_purpose_is_not_the_same() -> None:
    """An export over a charge's exact window is a different plan.

    *Mutation: drop the purpose equality and this fails.*
    """
    published = _window(
        EXECUTION_INTENT_NET_EXPORT, _at(hour=12, minute=0), _at(hour=14, minute=0)
    )
    assert not _continues(published, ANNOUNCED)


# ===========================================================================
# announcing
# ===========================================================================


def test_a_charge_is_announced_once_before_it_starts() -> None:
    """One ``planned`` event, carrying the promise and the window."""
    opens = NOW + timedelta(minutes=10)
    closes = _at(hour=14, minute=45)
    rig = _Rig(targets=(_target("c1", EXECUTION_INTENT_GRID_CHARGE, opens, closes),))
    rig.announce()

    assert rig.kinds == [LIFECYCLE_KIND_PLANNED]
    event = rig.fired[0]
    assert event["purpose"] == EXECUTION_INTENT_GRID_CHARGE
    assert event["objective_boundary"] == CAMPAIGN_BOUNDARY_BATTERY
    assert event["planned_kwh"] == pytest.approx(2.0)
    assert event["window_start"] == opens.isoformat()
    assert event["window_end"] == closes.isoformat()
    assert event["campaign_instance_id"] is None


def test_an_export_is_announced_once_before_it_starts() -> None:
    """The meter side, at the meter boundary."""
    opens = NOW + timedelta(minutes=10)
    closes = _at(hour=13, minute=0)
    rig = _Rig(targets=(_target("e1", EXECUTION_INTENT_NET_EXPORT, opens, closes),))
    rig.announce()

    assert rig.kinds == [LIFECYCLE_KIND_PLANNED]
    assert rig.fired[0]["objective_boundary"] == CAMPAIGN_BOUNDARY_METER


def test_a_campaign_beyond_the_lead_is_not_announced_yet() -> None:
    """**Approximately one planning cadence, not the whole horizon.**

    *Mutation: drop the lead-time bound and the evening export is announced at
    breakfast.*
    """
    opens = NOW + timedelta(minutes=ECONOMIC_ANNOUNCE_LEAD_MINUTES + 1)
    rig = _Rig(
        targets=(
            _target("c1", EXECUTION_INTENT_GRID_CHARGE, opens, _at(hour=16, minute=0)),
        )
    )
    rig.announce()
    assert rig.fired == []
    assert rig.store.campaign_announcement is None


def test_a_moving_tail_does_not_announce_a_second_time() -> None:
    """**Policy C: the event stands and the attributes move under it.**

    The tail moves on nearly every refresh, so a "replanned" line per wiggle would
    rebuild the per-quarter noise this surface exists to replace.

    *Mutation: dedup on ``campaign_id`` and this fires every refresh.*
    """
    opens = NOW + timedelta(minutes=10)
    rig = _Rig(
        targets=(
            _target(
                "af82a579ac6a803a",
                EXECUTION_INTENT_GRID_CHARGE,
                opens,
                _at(hour=15, minute=0),
            ),
        )
    )
    rig.announce()
    assert rig.kinds == [LIFECYCLE_KIND_PLANNED]

    # The same campaign, republished with a retreated tail and a fresh digest.
    rig.c.execution_targets = (
        _target(
            "c9d9217306560d3a",
            EXECUTION_INTENT_GRID_CHARGE,
            opens,
            _at(hour=14, minute=45),
            kwh=2.5,
        ),
    )
    rig.announce(NOW + timedelta(minutes=15))

    assert rig.kinds == [LIFECYCLE_KIND_PLANNED], "one plan, one line"
    mark = rig.store.campaign_announcement
    assert mark["window_end"] == _at(hour=14, minute=45).isoformat()
    assert mark["planned_kwh"] == pytest.approx(2.5)
    assert mark["campaign_id"] == "c9d9217306560d3a", "the live id is carried forward"


def test_a_campaign_of_gaps_alone_announces_nothing() -> None:
    """A ``serve_load`` stretch commands nothing, so it promises nothing."""
    rig = _Rig(
        targets=(
            _target(
                "g1",
                EXECUTION_INTENT_SERVE_LOAD,
                NOW + timedelta(minutes=5),
                _at(hour=13, minute=0),
            ),
        )
    )
    rig.announce()
    assert rig.fired == []


# ===========================================================================
# planned-only closure, and the accounting fence
# ===========================================================================


def _announced_rig(**kwargs: Any) -> _Rig:
    """Return a rig with one live announcement for a charge at 12:00-14:00."""
    mark = {
        "campaign_id": "c1",
        "purpose": EXECUTION_INTENT_GRID_CHARGE,
        "objective_boundary": CAMPAIGN_BOUNDARY_BATTERY,
        "planned_kwh": 2.0,
        "window_start": _at(hour=12, minute=0).isoformat(),
        "window_end": _at(hour=14, minute=0).isoformat(),
    }
    return _Rig(announcement=mark, **kwargs)


def test_a_different_campaign_supersedes_the_announcement() -> None:
    """A non-overlapping plan of the same purpose is a different plan."""
    rig = _announced_rig(
        targets=(
            _target(
                "c2",
                EXECUTION_INTENT_GRID_CHARGE,
                _at(hour=15, minute=0),
                _at(hour=16, minute=0),
            ),
        )
    )
    rig.announce(_at(hour=14, minute=50))

    assert rig.kinds[0] == LIFECYCLE_KIND_PLAN_CLOSED
    assert rig.fired[0]["result"] == OUTCOME_SUPERSEDED
    assert rig.kinds[1] == LIFECYCLE_KIND_PLANNED, "and the new plan is announced"


def test_an_announcement_whose_window_passes_closes_not_executed() -> None:
    """Nothing was published and the window is over."""
    rig = _announced_rig()
    rig.announce(_at(hour=14, minute=1))

    assert rig.kinds == [LIFECYCLE_KIND_PLAN_CLOSED]
    assert rig.fired[0]["result"] == OUTCOME_NOT_EXECUTED
    assert rig.store.campaign_announcement is None


def test_silence_inside_the_window_is_not_withdrawal() -> None:
    """One refresh that published nothing does not close a live plan."""
    rig = _announced_rig()
    rig.announce(_at(hour=13, minute=0))
    assert rig.fired == []
    assert rig.store.campaign_announcement is not None


def test_a_plan_closed_result_is_only_ever_superseded_or_not_executed() -> None:
    """The vocabulary is closed, and both members already exist."""
    assert set(PLAN_CLOSED_RESULTS) == {OUTCOME_SUPERSEDED, OUTCOME_NOT_EXECUTED}


def test_a_planned_only_closure_fabricates_no_instance_and_no_energy() -> None:
    """**The payload half of the fence.**

    ``campaign_instance_id`` is null because none was ever minted, and there is no
    ``realised_kwh`` key at all -- not ``0.0``, not ``None``. A null invites a
    template to render a zero, and "0.0 kWh" against a promise is the precise lie
    this release removes.

    *Mutation: emit ``realised_kwh: 0.0``, or derive an id from ``campaign_id``, and
    this fails.*
    """
    rig = _announced_rig()
    rig.announce(_at(hour=14, minute=1))

    payload = rig.fired[0]
    assert payload["campaign_instance_id"] is None
    assert "realised_kwh" not in payload
    assert "realized_kwh" not in payload
    assert "shortfall_kwh" not in payload


def test_a_planned_only_closure_never_touches_a_campaign_terminal() -> None:
    """**The runtime half of the fence.**

    ``last_campaign_result`` answers "how did the last campaign that actually ran
    turn out". An announcement has no answer to that question, so it does not
    overwrite one -- and it never emits ``removed``, never latches an instance and
    never writes the campaign mark. The ``_Store`` above raises if the latch is even
    consulted.

    *Mutation: write ``_last_campaign_result`` here, or fire ``removed``, and this
    fails.*
    """
    rig = _announced_rig()
    rig.announce(_at(hour=14, minute=1))

    assert rig.c._last_campaign_result == "UNTOUCHED"
    assert rig.c._closed_campaign == "UNTOUCHED"
    assert rig.store.campaign_lifecycle is None
    assert LIFECYCLE_KIND_REMOVED not in rig.kinds


def test_the_announcement_path_cannot_reach_any_settlement_symbol() -> None:
    """**The structural half of the fence, and the one that survives a refactor.**

    A boundary asserted only through behaviour is a boundary one new call away from
    being crossed. These four methods are the whole announcement surface, and none
    of them may name a settlement or accounting symbol.
    """
    forbidden = (
        "_close_campaign",
        "_lifecycle_removed",
        "_publish_recovered_terminal",
        "note_lifecycle_closed",
        "_last_campaign_result",
        "_closed_campaign",
        "campaign_lifecycle =",
        "_campaign_realized_now",
        "battery_return",
        "realized_window",
        "today_accounting",
    )
    for method in (
        AlphaEmsCoordinator._note_campaign_announcement,
        AlphaEmsCoordinator._fire_plan_closed,
        AlphaEmsCoordinator._published_campaign_windows,
        AlphaEmsCoordinator._announcement_payload,
    ):
        source = _executable_source(method)
        for symbol in forbidden:
            assert symbol not in source, f"{method.__name__} names {symbol}"


def test_an_open_campaign_is_never_re_announced() -> None:
    """The campaign has already taken over; the plan line was its own.

    *Mutation: drop the open-campaign filter and every running campaign is
    announced again beside itself.*
    """
    opens = NOW + timedelta(minutes=10)
    closes = _at(hour=14, minute=0)
    rig = _Rig(
        targets=(_target("c1", EXECUTION_INTENT_GRID_CHARGE, opens, closes),),
        lifecycle={
            "instance_id": "aaaa",
            "campaign_id": "c1",
            "purpose": EXECUTION_INTENT_GRID_CHARGE,
            "window_start": _at(hour=9, minute=0).isoformat(),
        },
        campaign_id="c1",
        opened_at=_at(hour=9, minute=0),
        planned_end=closes,
    )
    rig.announce()
    assert rig.fired == []
