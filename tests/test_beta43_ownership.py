"""beta.43: our own dispatch winding down is not somebody else's takeover.

Alpha EMS stops its own run at a quarter boundary: it releases the owner marker and
clears the causal record. The vendor's dead-man keeps ``dispatch_active`` true for
minutes afterwards, so ``ownership_of`` saw a running dispatch with no marker and no
provable causation and answered ``foreign`` -- about a dispatch it had armed and had
just stopped.

That answer is not merely a wrong word. ``_decide``'s foreign branch sets
``stop_reason: ownership_conflict`` whenever a run was carried, and that reason is in
neither :data:`EXECUTION_FAILED_STOP_REASONS` nor
:data:`EXECUTION_COMPLETION_STOP_REASONS` -- so it falls through ``_close_campaign``'s
precedence to ``canceled``. On 2026-09-05 that reason was live in ``execution.result``
with a carried run present, five minutes after our own stop, with the register still
reporting ``dispatch_active: true`` and a timer running to 20:50:39.

The new state authorises nothing. Every clause below is a refusal, and the fallback in
every unproven case is the answer the release inherited.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.alpha_ems_manager.const import (
    EXECUTION_STATE_INHIBITED,
    EXECUTION_STOP_OWNERSHIP_CONFLICT,
    INHIBIT_OWN_DISPATCH_RELEASING,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_OWNED,
    OWNERSHIP_RELEASING,
    OWNERSHIP_STATES,
)
from custom_components.alpha_ems_manager.execution import (
    OwnershipEvidence,
    Progress,
    decide,
    ownership_of,
)

NOW = datetime(2026, 9, 5, 20, 46, tzinfo=UTC)
#: The register's own dead-man instant, as the live capture reported it.
DEADMAN = datetime(2026, 9, 5, 20, 50, 39, tzinfo=UTC)


def _evidence(**overrides) -> OwnershipEvidence:
    """Return the live post-release shape: running, unmarked, receipt held."""
    fields = {
        "dispatch_active": True,
        "marker_on": False,
        "record": None,
        "run_id": "a16660350cf0d994",
        "now": NOW,
        "readback_compatible": True,
        "release_receipt": {
            "run_id": "a16660350cf0d994",
            "released_at": (NOW - timedelta(minutes=1)).isoformat(),
            "timer_finishes_at": DEADMAN.isoformat(),
        },
        "dispatch_timer_finishes_at": DEADMAN,
    }
    fields.update(overrides)
    return OwnershipEvidence(**fields)


def test_our_own_draining_dispatch_is_releasing_rather_than_foreign() -> None:
    """The live shape, reproduced exactly.

    *Mutation: delete the ``own_release_draining`` branch and this reads foreign.*
    """
    evidence = _evidence()
    assert evidence.dispatch_active and not evidence.marker_on, (
        "the witness: this is the state that used to read foreign"
    )
    assert not evidence.record_causation_holds, (
        "and the record really is gone -- our own stop cleared it"
    )
    assert evidence.own_release_draining is True
    assert ownership_of(evidence) == OWNERSHIP_RELEASING


def test_releasing_is_never_owned() -> None:
    """The whole safety argument, asserted rather than described.

    Every gate in the integration refuses on "not owned"; none of them enumerates
    the states it will accept. So the property that matters is this one.
    """
    assert ownership_of(_evidence()) != OWNERSHIP_OWNED
    assert OWNERSHIP_RELEASING in OWNERSHIP_STATES
    assert OWNERSHIP_RELEASING != OWNERSHIP_OWNED


def test_a_passed_deadline_is_no_longer_our_tail() -> None:
    """Past the register's own deadline the receipt explains nothing.

    **No fixed grace period is invented anywhere**: the bound is the instant the
    register itself reported, and after it the answer is the inherited one.
    """
    evidence = _evidence(now=DEADMAN + timedelta(seconds=1))
    assert evidence.own_release_draining is False
    assert ownership_of(evidence) == OWNERSHIP_FOREIGN


def test_an_unreadable_timer_falls_back_to_foreign() -> None:
    """No deadline, no claim. This is the conservative direction and it is kept."""
    assert ownership_of(_evidence(release_receipt=None)) == OWNERSHIP_FOREIGN
    assert (
        ownership_of(_evidence(release_receipt={"run_id": "x"})) == OWNERSHIP_FOREIGN
    ), "a receipt without a deadline proves nothing"
    assert (
        ownership_of(_evidence(dispatch_timer_finishes_at=None)) == OWNERSHIP_FOREIGN
    ), "and neither does a register that will not say"


def test_a_dispatch_armed_after_our_release_is_still_foreign() -> None:
    """**The clause that stops a receipt excusing a real takeover.**

    Somebody arming a dispatch after our release produces a *different* dead-man
    deadline, and the comparison against the receipt's is what notices.

    *Mutation: drop the deadline comparison and a genuine foreign dispatch inherits
    our release for as long as our own receipt happens to remain unexpired.*
    """
    someone_else = DEADMAN + timedelta(minutes=17)
    evidence = _evidence(dispatch_timer_finishes_at=someone_else)
    assert evidence.own_release_draining is False
    assert ownership_of(evidence) == OWNERSHIP_FOREIGN


def test_a_marked_dispatch_is_not_a_release() -> None:
    """The marker being on means this is not the situation at all."""
    assert _evidence(marker_on=True).own_release_draining is False


def test_nothing_running_is_not_a_release() -> None:
    """A receipt does not conjure a dispatch to be draining."""
    assert _evidence(dispatch_active=False).own_release_draining is False


def test_the_releasing_inhibit_reason_is_its_own_word() -> None:
    """It refuses what ``foreign_dispatch`` refuses; it does not say the same thing.

    Naming our own cleanup a takeover is what put ``ownership_conflict`` in front of
    a campaign terminal in the first place.
    """
    assert INHIBIT_OWN_DISPATCH_RELEASING != "foreign_dispatch"
    assert INHIBIT_OWN_DISPATCH_RELEASING == "own_dispatch_releasing"


def _decision(evidence: OwnershipEvidence):
    """Return the Stage B decision for ``evidence``, with a run carried."""
    return decide(
        mode_executes=True,
        mode_off=False,
        targets=(),
        now=NOW,
        evidence=evidence,
        progress=Progress(realized_kwh=0.0, basis="accumulated", quality="exact"),
        running_run_id="a16660350cf0d994",
    )


def test_a_foreign_dispatch_still_ends_a_carried_run() -> None:
    """The inherited behaviour, unchanged. This is the branch that must not move."""
    verdict = _decision(_evidence(release_receipt=None))

    assert verdict.ownership == OWNERSHIP_FOREIGN
    assert verdict.stop_reason == EXECUTION_STOP_OWNERSHIP_CONFLICT
    assert verdict.inhibit_reason == "foreign_dispatch"


def test_our_own_tail_names_no_ownership_conflict() -> None:
    """**The false ``canceled``, removed at its source.**

    ``ownership_conflict`` is in neither the failed nor the completion reason set, so
    a campaign closing while it is in flight files ``canceled``. There is nothing to
    end here: we ended it ourselves, deliberately, and the campaign's verdict was
    decided at that stop.

    *Mutation: return the foreign branch for a releasing dispatch, or set a
    ``stop_reason`` on the new one, and this fails.*
    """
    verdict = _decision(_evidence())

    assert verdict.ownership == OWNERSHIP_RELEASING
    assert verdict.stop_reason is None, "our own cleanup ends nothing"
    assert verdict.inhibit_reason == INHIBIT_OWN_DISPATCH_RELEASING
    assert verdict.state == EXECUTION_STATE_INHIBITED, (
        "the restraint is identical: inhibited, and nothing is written"
    )
    assert not getattr(verdict, "reset_required", False)


def test_no_receipt_is_written_when_the_register_will_not_say() -> None:
    """**The refusal at the writing end, which is where a grace would be invented.**

    A snapshot without a readable dead-man leaves the tail indistinguishable from a
    foreign dispatch on the evidence available. Writing a receipt anyway -- stamped
    with our own clock, or with an assumed duration -- would be exactly the guess
    this design refuses, and it would excuse a dispatch nothing can account for.

    *Mutation: stamp the receipt with ``now`` when the timer is unreadable, and this
    fails.*
    """
    from types import SimpleNamespace

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    holder = SimpleNamespace(
        _release_receipt={"stale": True},
        _execution_identity=lambda: "a16660350cf0d994",
    )
    AlphaEmsCoordinator._note_release_receipt(
        holder, SimpleNamespace(dispatch_timer_finishes_at=None), NOW
    )
    assert holder._release_receipt is None, "no deadline, no receipt"

    AlphaEmsCoordinator._note_release_receipt(holder, None, NOW)
    assert holder._release_receipt is None, "and no snapshot is no receipt either"

    AlphaEmsCoordinator._note_release_receipt(
        holder, SimpleNamespace(dispatch_timer_finishes_at=DEADMAN), NOW
    )
    assert holder._release_receipt == {
        "run_id": "a16660350cf0d994",
        "released_at": NOW.isoformat(),
        "timer_finishes_at": DEADMAN.isoformat(),
    }, "and the deadline kept is the register's own, never a duration we assumed"
