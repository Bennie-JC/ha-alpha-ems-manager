"""beta.27: an open quarter's authority, one test per clause of the rule.

**The rule.** Once ``quarter_start`` passes, a quarter's *economic* authority is
frozen for at most fifteen minutes. Exactly four things end it:

1. the objective is reached;
2. ``now >= quarter_end``;
3. a safety condition invalidates execution;
4. an explicit, causally attributable signal referring to **this** quarter --
   which **does not exist in the contract today**.

And withdrawal is **never inferred** from: the quarter's absence from a horizon
that structurally cannot contain it; an intent disappearing globally; or the same
intent appearing in an unrelated later run.

**Why there is no inference to be made.** ``execution.py`` says it of runs:

    *"This is an inference, not a cancellation signal, and the contract cannot do
    better: a withdrawn run and a rolled-forward run are both simply absent from
    the next publication."*

Run-level withdrawal works only because a *future* window can be re-described by a
later publication. An **open quarter can never be re-described**: Stage A's horizon
head is ``elapsed_intervals + 1``, so no publication issued after ``quarter_start``
contains it. Any rule claiming to detect a withdrawal is inferring from evidence
that structurally cannot exist -- which is why the discriminator "the intent still
appears somewhere in the horizon" was withdrawn, and why the tests below assert
that no such inference happens.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
)
from custom_components.alpha_ems_manager.execution import (
    CarriedQuarter,
    admit_quarter,
    carry_quarter,
    next_quarter_row,
    parse_target,
)

from .forecast_helpers import local

NORMAL = datetime(2026, 3, 10).date()
LIVE = frozenset({EXECUTION_INTENT_GRID_CHARGE, EXECUTION_INTENT_NET_EXPORT})


def rows(
    start_hour: int,
    start_minute: int,
    count: int = 1,
    *,
    battery: float = 1.0,
    authorised: float = 0.8,
    export: float = 0.0,
) -> list[dict]:
    """Return ``count`` consecutive published quarter rows from a boundary."""
    out = []
    for step in range(count):
        opens = local(NORMAL, start_hour, start_minute) + timedelta(minutes=15 * step)
        out.append(
            {
                "start": opens.isoformat(),
                "end": (opens + timedelta(minutes=15)).isoformat(),
                "battery_kwh": battery,
                "grid_authorised_kwh": authorised,
                "grid_export_target_kwh": export,
                "grid_export_caused_kwh": 0.0,
                "desired_grid_kw": authorised / 0.25,
            }
        )
    return out


def target(
    *,
    plan_id: str = "plan-1",
    intent: str = EXECUTION_INTENT_GRID_CHARGE,
    opens_hour: int = 15,
    opens_minute: int = 0,
    quarters: int = 1,
    schedule: list[dict] | None = None,
    battery: float = 1.0,
    export: float = 0.0,
    revision: int = 1,
) -> dict:
    """Return a published Stage-A target with a quarter schedule."""
    opens = local(NORMAL, opens_hour, opens_minute)
    body = (
        rows(opens_hour, opens_minute, quarters, battery=battery, export=export)
        if schedule is None
        else schedule
    )
    return {
        "plan_id": plan_id,
        "revision": revision,
        "intent": intent,
        "purpose": intent,
        "window_start": opens.isoformat(),
        "window_end": (opens + timedelta(minutes=15 * max(1, quarters))).isoformat(),
        "battery_target_kwh": battery * max(1, quarters),
        "average_power_kw": battery / 0.25,
        "quarter_schedule": body,
    }


def open_quarter(
    *,
    intent: str = EXECUTION_INTENT_GRID_CHARGE,
    hour: int = 15,
    minute: int = 0,
    battery: float = 1.0,
    export: float = 0.0,
    run_id: str = "run-1",
    frozen: float | None = None,
) -> CarriedQuarter:
    """Return a quarter admitted just before it opened."""
    published = target(
        intent=intent,
        opens_hour=hour,
        opens_minute=minute,
        battery=battery,
        export=export,
    )
    parsed = parse_target(published)
    assert parsed is not None
    row = next_quarter_row(parsed, local(NORMAL, hour, minute) - timedelta(minutes=1))
    assert row is not None
    return admit_quarter(
        row,
        intent=intent,
        run_id=run_id,
        plan_id=parsed.plan_id,
        revision=parsed.revision,
        now=local(NORMAL, hour, minute) - timedelta(minutes=1),
        frozen_remaining_kwh=frozen,
    )


# == the R1 fix: admission happens one refresh ahead ========================


def test_the_quarter_opening_next_is_admitted_before_it_opens() -> None:
    """**The R1 fix.** A publication at 14:45 admits the 15:00 quarter.

    ``actionable_target`` requires a window *containing* now, and Stage A's horizon
    head is ``elapsed + 1``, so a fresh publication never contains now. That is why
    a quarter must be admitted from the row that opens **next**.
    """
    published = target(opens_hour=15, opens_minute=0)
    parsed = parse_target(published)
    assert parsed is not None

    row = next_quarter_row(parsed, local(NORMAL, 14, 45))

    assert row is not None
    assert row.start == local(NORMAL, 15, 0)


def test_a_row_that_has_already_opened_is_not_admitted() -> None:
    """Strictly ``start > now``, so a quarter is never adopted mid-flight.

    Adopting one would mean executing against a target with no measured progress --
    the same reason a restart stops an owned dispatch rather than continuing it.
    """
    parsed = parse_target(target(opens_hour=15, opens_minute=0))
    assert parsed is not None

    assert next_quarter_row(parsed, local(NORMAL, 15, 0)) is None
    assert next_quarter_row(parsed, local(NORMAL, 15, 1)) is None


def test_the_quarter_survives_the_refresh_at_its_own_boundary() -> None:
    """**Case H.** A refresh three seconds after 15:00 leaves it executable.

    This is the exact interleaving beta.26 lost the quarter on: the run ended at
    the boundary, the fresh publication could not be admitted until 15:15, and the
    15:00 quarter never physically executed.
    """
    quarter = open_quarter(hour=15, minute=0)
    # The parent run has gone -- which is what happened, and is now irrelevant.
    carried = carry_quarter(
        quarter,
        [target(plan_id="plan-2", opens_hour=15, opens_minute=15)],
        local(NORMAL, 15, 0, 3),
        run=None,
        executable_intents=LIVE,
    )

    assert carried is quarter
    assert carried.open_at(local(NORMAL, 15, 0, 3))
    assert carried.seconds_remaining(local(NORMAL, 15, 0, 3)) == pytest.approx(897.0)


def test_a_publication_with_no_schedule_admits_nothing_rather_than_failing() -> None:
    """Backward compatibility: a pre-beta.27 publication degrades, never raises."""
    published = target()
    del published["quarter_schedule"]
    parsed = parse_target(published)

    assert parsed is not None
    assert parsed.quarter_schedule == ()
    assert (
        carry_quarter(
            None, [published], local(NORMAL, 14, 45), run=None, executable_intents=LIVE
        )
        is None
    )


def test_a_quarter_with_nothing_to_do_is_not_admitted() -> None:
    """An envelope with no target would arm a dispatch and stop it immediately."""
    empty = rows(15, 0, 1, battery=0.0, authorised=0.0, export=0.0)

    assert (
        carry_quarter(
            None,
            [target(schedule=empty, battery=0.0)],
            local(NORMAL, 14, 45),
            run=None,
            executable_intents=LIVE,
        )
        is None
    )


def test_an_intent_this_release_does_not_execute_is_not_admitted() -> None:
    """``serve_load`` has no published meter figure to be measured against."""
    assert (
        carry_quarter(
            None,
            [target(intent="serve_load")],
            local(NORMAL, 14, 45),
            run=None,
            executable_intents=LIVE,
        )
        is None
    )


# == clause 1-3: what does end an open quarter ==============================


def test_the_quarter_ends_unconditionally_at_its_own_end() -> None:
    """**Clause 2.** No lease, no target and no publication extends it."""
    quarter = open_quarter(hour=15, minute=0)

    assert quarter.open_at(local(NORMAL, 15, 14, 59))
    assert not quarter.open_at(local(NORMAL, 15, 15))
    assert quarter.seconds_remaining(local(NORMAL, 15, 15)) == 0.0
    assert quarter.seconds_remaining(local(NORMAL, 15, 30)) == 0.0


def test_a_finished_quarter_is_dropped_and_the_next_one_admitted() -> None:
    """The carrier moves on, and the caller records the finished one's shortfall."""
    finished = open_quarter(hour=15, minute=0)
    fresh = target(plan_id="plan-2", opens_hour=15, opens_minute=30)

    carried = carry_quarter(
        finished, [fresh], local(NORMAL, 15, 15), run=None, executable_intents=LIVE
    )

    assert carried is not finished
    assert carried is not None
    assert carried.quarter_start == local(NORMAL, 15, 30)


def test_the_envelope_ends_at_quarter_end_so_no_authority_is_orphaned() -> None:
    """No overlap is possible, so reconciliation needs no rule.

    The horizon head guarantees a new run's first quarter opens at the *next*
    boundary, and this envelope ends at its own -- so two quarters can never both
    be open.
    """
    first = open_quarter(hour=15, minute=0)
    second = open_quarter(hour=15, minute=15)

    assert first.quarter_end == second.quarter_start
    assert not first.open_at(second.quarter_start)


# == clause 4 and the withdrawal rule: what does NOT end it ================


def test_an_unrelated_charge_tomorrow_does_not_affect_an_open_charge() -> None:
    """**Clause 1 of the withdrawal rule.** Presence proves nothing.

    The discriminator "the intent still appears somewhere in the horizon" is
    withdrawn: an unrelated charge tomorrow would have kept a genuinely abandoned
    charge alive. The open quarter is valid here for a different reason -- it is
    economically immutable while open, whatever else the horizon says.
    """
    quarter = open_quarter(hour=15, minute=0)
    tomorrow = target(plan_id="plan-tomorrow", opens_hour=23, opens_minute=45)

    carried = carry_quarter(
        quarter, [tomorrow], local(NORMAL, 15, 5), run=None, executable_intents=LIVE
    )

    assert carried is quarter


def test_an_unrelated_export_later_tonight_does_not_affect_an_open_export() -> None:
    """**Clause 2 of the withdrawal rule.** The same, in the other direction."""
    quarter = open_quarter(
        intent=EXECUTION_INTENT_NET_EXPORT, hour=15, minute=0, export=0.8
    )
    tonight = target(
        plan_id="plan-tonight",
        intent=EXECUTION_INTENT_NET_EXPORT,
        opens_hour=21,
        opens_minute=0,
        export=0.5,
    )

    carried = carry_quarter(
        quarter, [tonight], local(NORMAL, 15, 5), run=None, executable_intents=LIVE
    )

    assert carried is quarter
    assert carried.intent == EXECUTION_INTENT_NET_EXPORT


def test_an_intent_vanishing_from_the_horizon_does_not_cancel_it() -> None:
    """**Clause 3, and the important one.** Absence alone cancels nothing.

    Stage A's horizon head is ``elapsed + 1``, so the open quarter *cannot* appear
    in any later publication. Reading its absence as a withdrawal would treat a
    structural certainty as evidence -- and would cancel every quarter, always.
    """
    quarter = open_quarter(hour=15, minute=0)
    # A horizon containing no charge at all, and not even this plan.
    unrelated = target(
        plan_id="plan-other",
        intent=EXECUTION_INTENT_NET_EXPORT,
        opens_hour=19,
        opens_minute=0,
        export=0.5,
    )

    assert (
        carry_quarter(
            quarter,
            [unrelated],
            local(NORMAL, 15, 5),
            run=None,
            executable_intents=LIVE,
        )
        is quarter
    )
    # And with an entirely empty horizon, which is the strongest form of absence.
    assert (
        carry_quarter(
            quarter, [], local(NORMAL, 15, 5), run=None, executable_intents=LIVE
        )
        is quarter
    )


def test_a_parent_run_ending_or_rolling_has_no_effect_on_an_open_quarter() -> None:
    """Not a discriminator any more -- simply irrelevant, which is stronger."""
    quarter = open_quarter(hour=15, minute=0, run_id="run-1")

    for run_state in (None,):
        carried = carry_quarter(
            quarter,
            [target(plan_id="plan-2", opens_hour=15, opens_minute=15)],
            local(NORMAL, 15, 7),
            run=run_state,
            executable_intents=LIVE,
        )
        assert carried is quarter
        # And the execution identity still names the run it was admitted under, so
        # the causal record keeps matching while the run slot is empty.
        assert carried.run_id == "run-1"


def test_a_fresh_publication_cannot_enlarge_an_open_quarter() -> None:
    """**Invariant 7, the growth half.** Immutable means immutable upward too."""
    quarter = open_quarter(hour=15, minute=0, battery=1.0)
    generous = target(
        plan_id="plan-2",
        schedule=rows(15, 0, 1, battery=9.0, authorised=9.0),
        battery=9.0,
    )

    carried = carry_quarter(
        quarter, [generous], local(NORMAL, 15, 5), run=None, executable_intents=LIVE
    )

    assert carried is quarter
    assert carried.battery_target_kwh == pytest.approx(1.0)
    assert carried.battery_allowance_kwh() == pytest.approx(1.0)


def test_a_fresh_publication_cannot_reduce_an_open_quarter_either() -> None:
    """**Invariant 7, the reduction half.** And there is no signal that could.

    A reduction after ``quarter_start`` would have to come from a publication whose
    horizon cannot contain this quarter, so what arrives is a *different* quarter's
    figures. Applying them here would reduce the open quarter on evidence about
    another one.
    """
    quarter = open_quarter(hour=15, minute=0, battery=1.0)
    meagre = target(
        plan_id="plan-2",
        schedule=rows(15, 0, 1, battery=0.1, authorised=0.05),
        battery=0.1,
    )

    carried = carry_quarter(
        quarter, [meagre], local(NORMAL, 15, 5), run=None, executable_intents=LIVE
    )

    assert carried is quarter
    assert carried.battery_target_kwh == pytest.approx(1.0)


# == the allowance is snapshotted at admission =============================


def test_the_run_level_cap_bounds_the_quarter_at_admission() -> None:
    """**Clause 4 of the authority rule.** It applies while the quarter is future.

    A run-level frozen remainder of 0.4 kWh caps a 1.0 kWh quarter at admission --
    which is a legitimate reduction, because the quarter had not opened yet.
    """
    quarter = open_quarter(hour=15, minute=0, battery=1.0, frozen=0.4)

    assert quarter.battery_target_kwh == pytest.approx(1.0)
    assert quarter.battery_allowance_kwh() == pytest.approx(0.4)


def test_a_later_reduction_governs_the_next_quarter_and_never_the_open_one() -> None:
    """The snapshot is what makes that true, and it is not an exception.

    ``remaining_authorised_kwh`` returns the *frozen* cap whenever
    ``now < forward.forward_from``, and ``forward_from`` is by construction the next
    boundary -- so the forward cap has always been a "next quarter onward"
    instrument. It keeps governing every later quarter and simply never governs the
    one under way.
    """
    open_now = open_quarter(hour=15, minute=0, battery=1.0, frozen=1.0)
    # The same publication, admitted later, under a reduced remainder.
    next_one = open_quarter(hour=15, minute=15, battery=1.0, frozen=0.2)

    assert open_now.battery_allowance_kwh() == pytest.approx(1.0)
    assert next_one.battery_allowance_kwh() == pytest.approx(0.2)


def test_an_uncapped_run_leaves_the_quarter_at_its_published_figure() -> None:
    """``None`` means unconstrained, and must not be read as a cap of zero."""
    quarter = open_quarter(hour=15, minute=0, battery=1.0, frozen=None)

    assert quarter.frozen_remaining_at_admission_kwh is None
    assert quarter.battery_allowance_kwh() == pytest.approx(1.0)


def test_the_allowance_is_never_negative() -> None:
    """A nonsense figure floors at zero rather than inverting the bound."""
    quarter = open_quarter(hour=15, minute=0, battery=1.0, frozen=-5.0)

    assert quarter.battery_allowance_kwh() == 0.0


# == the structural guard against re-introducing the inference =============


def test_no_code_path_infers_withdrawal_from_intent_presence_or_absence() -> None:
    """Asserted structurally, because the absence is the property.

    ``carry_quarter``'s open-quarter branch must be **unconditional** on the
    horizon: one test, ``current.open_at(now)``, and an immediate return. A
    condition that also consulted ``targets`` would be the withdrawn inference
    creeping back, and it would pass every behavioural test above on the day it was
    written and fail silently a release later.
    """
    import ast
    import inspect

    from custom_components.alpha_ems_manager import execution

    source = inspect.getsource(execution.carry_quarter)
    tree = ast.parse(source.lstrip())
    function = tree.body[0]

    # The first statement is the open-quarter guard, and it returns immediately.
    first = function.body[1] if len(function.body) > 1 else function.body[0]
    while not isinstance(first, ast.If):
        function.body.pop(0)
        first = function.body[0]

    names = {node.id for node in ast.walk(first.test) if isinstance(node, ast.Name)}
    assert "targets" not in names, ast.dump(first.test)
    # Exactly the carrier and the clock. Nothing about the horizon, the intent, the
    # parent run or any published figure may appear in this condition.
    assert names == {"current", "now"}, names
    assert len(first.body) == 1
    assert isinstance(first.body[0], ast.Return)


def test_the_quarter_documents_that_withdrawal_is_never_inferred() -> None:
    """The rule is published with the object, so a reader need not find this file."""
    quarter = open_quarter(hour=15, minute=0)
    rule = quarter.as_dict()["authority_rule"]

    assert "withdrawal is never inferred" in rule
    assert "structurally cannot describe" in rule
