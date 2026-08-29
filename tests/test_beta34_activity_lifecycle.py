"""beta.34: the Activity line carries its own facts, and says the right words.

Two changes, both additive.

**Structured data.** The bus payload carried ``name``, ``message``, ``domain``,
``entity_id`` and ``plan_id``. An automation wanting to know whether a sale
succeeded had to match on English prose, and ``plan_id`` is
``sha1(category|window-end-minute)[:6]`` -- tied to neither the run nor the
campaign. Every line now carries the same event in machine-readable form beside
the sentence, and the sentence is unchanged, so text-matching automations keep
working.

**Vocabulary.** ``stage_a_hold`` rendered as *No Longer Economically Valid*,
which reads as a verdict on the plan's worth; an ordinary 15-minute re-solve is
one plan replacing another. And ``stopped`` -- declared since beta.19,
execution-class, emitted by nothing -- now has the job it was declared for, so
an ownership loss stops looking like a change of mind in a history view.

Five kinds that no production path ever constructed are retired: ``changed``,
``ended``, ``refused``, ``would_start`` and ``would_stop``. The last two had
already been withdrawn *in behaviour* -- ``_started_entry`` says in as many words
that Shadow "now emits nothing here at all" -- and the constants outlived the
decision. A vocabulary a consumer subscribes to must not contain words nothing
says.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager import activity as activity_module
from custom_components.alpha_ems_manager.const import (
    ACTIVITY_PURPOSE_ECONOMIC,
    ACTIVITY_PURPOSE_SAFETY,
    ECONOMIC_ADVICE_EVENT_KINDS,
    ECONOMIC_EVENT_KINDS,
    ECONOMIC_EVENT_STOPPED,
    ECONOMIC_EXECUTION_EVENT_KINDS,
    EXECUTION_STOP_OWNERSHIP_CONFLICT,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_SWITCHED_OFF,
)

# ===========================================================================
# 1. every declared kind is emitted by something
# ===========================================================================


def test_no_declared_event_kind_is_unreachable() -> None:
    """**The rule beta.33 applied to stop reasons, applied to event kinds.**

    A parametrised test over ``ECONOMIC_EVENT_KINDS`` looks like coverage and is
    not: it exercises the classifier, not the pipeline. Five of beta.33's
    thirteen kinds were constructed by no production path at all, and every test
    over them was green.

    *Mutation: add a constant back to ``ECONOMIC_EVENT_KINDS`` without an emit
    site and this fails.*
    """
    import pathlib

    source = pathlib.Path(activity_module.__file__).read_text(encoding="utf-8")
    for kind in ECONOMIC_EVENT_KINDS:
        name = f"ECONOMIC_EVENT_{kind.upper()}"
        # The import line does not count as a use: an emit site assigns it to a
        # ``kind=`` argument or to the local the entry is built from.
        uses = source.count(name)
        assert uses >= 2, f"{name} is declared and never emitted ({uses} mentions)"


def test_the_retired_kinds_are_gone_from_every_tuple() -> None:
    """Retired rather than emitted, because the lifecycle table has no place.

    ``changed`` lost its job to the announcement deadband, ``ended`` to
    ``finished``, ``refused`` to the Advisory marker on the Planned line, and the
    two shadow kinds to the decision that Shadow shows planning and stops there.
    """
    from custom_components.alpha_ems_manager import const

    for retired in ("CHANGED", "ENDED", "REFUSED", "WOULD_START", "WOULD_STOP"):
        assert not hasattr(const, f"ECONOMIC_EVENT_{retired}"), retired
    # And the classification tuples still partition the survivors exactly.
    assert set(ECONOMIC_ADVICE_EVENT_KINDS) | set(
        ECONOMIC_EXECUTION_EVENT_KINDS
    ) == set(ECONOMIC_EVENT_KINDS)
    assert not set(ECONOMIC_ADVICE_EVENT_KINDS) & set(ECONOMIC_EXECUTION_EVENT_KINDS)


# ===========================================================================
# 2. the words
# ===========================================================================


def test_a_normal_replan_is_a_supersession_not_a_verdict() -> None:
    """*No Longer Economically Valid* reads as a fault report on the plan.

    A Stage-A withdrawal is nothing of the kind: prices moved, or a quarter
    elapsed, and the optimiser now prefers something else. The live 13:00
    re-solve of 2026-08-29 filed exactly this line for an ordinary event.

    *Mutation: restore the old phrase and this fails.*
    """
    assert activity_module._CANCEL_REASONS[EXECUTION_STOP_STAGE_A_HOLD] == (
        "Plan Superseded"
    )
    # Still a cancellation, never a failure: nothing happened to the battery.
    assert EXECUTION_STOP_STAGE_A_HOLD not in activity_module._ERROR_REASONS


def test_a_safety_or_ownership_stop_is_stopped_not_cancelled() -> None:
    """Who ended it is the distinction a reader most needs.

    Stage A withdrawing a plan is a change of mind. These two happened *to* a
    dispatch that was under way, and filing both under ``cancelled`` made the
    13:30 ownership incident indistinguishable from an ordinary replan.

    *Mutation: empty ``_STOPPED_REASONS`` and this fails.*
    """
    stopped = activity_module._STOPPED_REASONS
    assert EXECUTION_STOP_OWNERSHIP_CONFLICT in stopped
    assert EXECUTION_STOP_SAFETY in stopped
    # A mode change is the user withdrawing authority: Stage A's side of the line.
    assert EXECUTION_STOP_SWITCHED_OFF not in stopped
    assert EXECUTION_STOP_STAGE_A_HOLD not in stopped
    # And the kind it produces is execution-class, correctly: both assert that a
    # real dispatch existed.
    assert ECONOMIC_EVENT_STOPPED in ECONOMIC_EXECUTION_EVENT_KINDS


# ===========================================================================
# 3. the structured payload
# ===========================================================================


REQUIRED_KEYS = (
    "kind",
    "outcome",
    "purpose",
    "campaign_id",
    "run_id",
    "plan_id",
    "planned_kwh",
    "realised_kwh",
    "started_at",
    "ended_at",
    "reason",
)


def test_every_key_is_present_on_every_shape_of_line() -> None:
    """``None`` where it does not apply, never absent.

    A consumer must be able to read ``data["outcome"]`` without first working out
    which shape of line it received. Absence and null are different failures to
    debug, and only one of them is greppable.

    *Mutation: drop ``kind`` from ``_structured`` and this fails.*
    """
    from datetime import UTC, datetime

    from custom_components.alpha_ems_manager.activity import (
        ActivityState,
        Lifecycle,
        PlanIdentity,
        _structured,
    )

    del ActivityState
    identity = PlanIdentity(
        category="economic_sell", end_utc=datetime(2026, 8, 29, 20, tzinfo=UTC)
    )
    lifecycle = Lifecycle(
        plan_id="abc123",
        identity=identity,
        direction="discharge",
        energy_kwh=2.65,
        window="19:45-20:00",
        run_id="run-1",
        campaign_id="c" * 16,
        started_at=datetime(2026, 8, 29, 19, 45, tzinfo=UTC),
    )

    with_plan = _structured("started", lifecycle)
    without = _structured("inhibited", None, reason="soc_stale")
    for payload in (with_plan, without):
        for key in REQUIRED_KEYS:
            assert key in payload, key

    assert with_plan["purpose"] == ACTIVITY_PURPOSE_ECONOMIC
    assert with_plan["campaign_id"] == "c" * 16
    assert with_plan["run_id"] == "run-1"
    assert with_plan["planned_kwh"] == pytest.approx(2.65)
    assert with_plan["started_at"].startswith("2026-08-29T19:45")
    # A standing condition belongs to the pipeline, not to a plan, and says so
    # by carrying nulls rather than by omitting keys.
    assert without["plan_id"] is None
    assert without["purpose"] is None
    assert without["reason"] == "soc_stale"


def test_the_purpose_is_derived_from_the_category_not_stored_beside_it() -> None:
    """One distinction, and the one that changes what a person would do.

    Six categories are the right vocabulary for a sentence and the wrong one for
    an automation. Derived rather than duplicated, so the two cannot drift.
    """
    from custom_components.alpha_ems_manager.activity import _purpose_for

    assert _purpose_for("safety_buy") == ACTIVITY_PURPOSE_SAFETY
    assert _purpose_for("economic_buy") == ACTIVITY_PURPOSE_ECONOMIC
    assert _purpose_for("economic_sell") == ACTIVITY_PURPOSE_ECONOMIC
    # The adopted lifecycle's category is genuinely unknown -- guessing would be
    # a claim about why the user's money was spent.
    assert _purpose_for("") is None


def test_the_payload_keeps_every_key_the_old_one_had() -> None:
    """Non-breaking, asserted rather than intended.

    The message text and the five original keys are exactly as beta.33 published
    them; the structure is added beside them.
    """
    from custom_components.alpha_ems_manager.activity import (
        ACTIVITY_NAME,
        ActivityEntry,
        ActivityState,
        logbook_payload,
    )

    entry = ActivityEntry(
        kind="planned",
        message="Planned Sell — 19:45-20:00 — 2.65 kWh",
        state=ActivityState(),
        plan_id="abc123",
        data={"kind": "planned", "outcome": None},
    )
    payload = logbook_payload(entry, domain="alpha_ems_manager", entity_id="sensor.x")

    assert payload["name"] == ACTIVITY_NAME
    assert payload["message"] == entry.message
    assert payload["domain"] == "alpha_ems_manager"
    assert payload["entity_id"] == "sensor.x"
    assert payload["plan_id"] == "abc123"
    assert payload["kind"] == "planned"
    assert payload["outcome"] is None
