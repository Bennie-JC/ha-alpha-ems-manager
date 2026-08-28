"""The Activity surface: one plan, one lifecycle, at most a handful of lines.

Four properties define this module, and each is enforced by the shape of the code
rather than by a convention:

**It is strictly observational.** :func:`next_activity` receives the planned runs,
what has already been said, the current instant and whether anything can actually
be sent -- and nothing else. It cannot see the plan object, the control report,
the safety state or the recovery machinery, because they are not arguments.

**Nothing reads it back.** No code in this integration subscribes to the event, no
figure is derived from it, and an installation with the recorder removed produces
identical numbers. Activity is a write-only tap.

**It cannot claim the battery did anything it did not do.** A start, a success and
an error are execution-class kinds, refused outright while nothing is executable,
and unreachable in Shadow -- where a lifecycle can only be planned and then
cancelled.

**It cannot print a figure it should not.** :class:`RunContent` carries a
category, an energy, a window and an instant. No power, no price, no expected
value, no reserve arithmetic, no charge-source prose. Those are not filtered out
of the sentence; they are *absent from the input*, which is a much stronger
guarantee than a test on a string.

Why this was rebuilt in beta.31
-------------------------------

The live Activity history was unreadable, and an export of it made the mechanism
plain. One charge campaign, ending at 16:15, reported itself planned and then
"finished" **six times** as its start slid 08:45 -> 09:15 -> 10:30 -> 11:15 ->
11:45 -> 12:15 and its energy shrank 13.33 -> 13.06 -> 11.67 -> 11.11 -> 10.83 ->
11.11 kWh. Of 79 messages in the export, roughly 47 were that churn.

Two independent faults produced it.

**The lifecycle was keyed on the wrong end of the window.** Identity was
``(direction, start_utc)``, and the horizon head is ``elapsed_intervals + 1`` --
so the *start* of a run already under way advances every single refresh while
nothing about the decision changes. The run then failed to match what had been
announced, so the old record was retired ("has finished the planned window") and
the new one announced afresh. Both lines were false: the window had not finished,
and nothing new had been planned.

**And "finished" was said about a window, not about a plan.** A superseded
announcement is not a completed campaign, and describing it as one is the single
most misleading thing this surface has ever done.

What replaces it
----------------

**The lifecycle is anchored on the window's end.** ``(category, end_utc)`` is what
a person means by "the four o'clock charge": the head of the horizon walks
forward under it, its energy is revised, its start moves -- and the thing it is
*for* does not move at all. In the export above, all six announcements share one
end, so they are one lifecycle and one Planned line.

**One plan, at most three lines.** ``Planned`` once, ``Buy Started`` at most once,
then exactly one terminal: ``Success``, ``Canceled`` or ``Error``. Each is
structural: the lifecycle record carries what has been said, and a plan id whose
terminal has been emitted is closed and can never be announced again.

**A refresh is not an event.** A re-solve that moves the target by less than a
bucket, or the window's end by no more than one planning interval, produces
nothing at all. Only a change to the *category*, the *direction* or a materially
different window ends one lifecycle and begins another -- and then the old plan is
cancelled as replaced rather than quietly overwritten.

**Every line is one short line.** Plan id, what kind of plan, when, how much. The
kW figures, the prices, the expected value, the edge value, the reserve
explanation and the solver's reason all stay in diagnostics, where a reader can go
looking for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha1

from .const import (
    ACTIVITY_CATEGORY_CURTAILMENT,
    ACTIVITY_CATEGORY_ECONOMIC_BUY,
    ACTIVITY_CATEGORY_ECONOMIC_DISCHARGE,
    ACTIVITY_CATEGORY_ECONOMIC_SELL,
    ACTIVITY_CATEGORY_MIXED_BUY,
    ACTIVITY_CATEGORY_SAFETY_BUY,
    CONTROL_EXECUTION_AVAILABLE,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_ANNOUNCE_LEAD_MINUTES,
    ECONOMIC_DEADBAND_ENERGY_KWH,
    ECONOMIC_DEADBAND_MINUTES,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    ECONOMIC_EVENT_AVAILABLE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_ERROR,
    ECONOMIC_EVENT_FINISHED,
    ECONOMIC_EVENT_INHIBITED,
    ECONOMIC_EVENT_PLANNED,
    ECONOMIC_EVENT_STARTED,
    ECONOMIC_EXECUTION_EVENT_KINDS,
    EXECUTION_STOP_BATTERY_CEILING,
    EXECUTION_STOP_EXECUTION_ERROR,
    EXECUTION_STOP_GRID_CEILING,
    EXECUTION_STOP_HEADROOM_REACHED,
    EXECUTION_STOP_MARKER_LOST,
    EXECUTION_STOP_OWNERSHIP_CONFLICT,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_TIMER_NOT_REFRESHED,
    EXECUTION_STOP_WINDOW_ENDED,
    MAX_ECONOMIC_RUNS_TRACKED,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
)

#: The name every entry is filed under.
#:
#: **"Alpha EMS" since beta.31.** It was "Economic plan", which was accurate in
#: beta.16 when the surface carried nothing but Stage-A advice, and stopped being
#: accurate the moment it began reporting real dispatches: "Economic plan - Grid
#: charge started" reads as though the plan started rather than the battery.
#:
#: Fixed rather than taken from the entity's friendly name, which is renameable --
#: a logbook filter built on that would silently stop matching. Renaming it does
#: break a filter someone built on the old string, and that is the whole cost: no
#: entity id changes, no state changes, and nothing in the integration reads it.
ACTIVITY_NAME = "Alpha EMS"

#: How each plan category reads in front of a user.
_CATEGORY_LABELS = {
    ACTIVITY_CATEGORY_SAFETY_BUY: "Safety Buy",
    ACTIVITY_CATEGORY_ECONOMIC_BUY: "Economic Buy",
    ACTIVITY_CATEGORY_MIXED_BUY: "Mixed Buy",
    ACTIVITY_CATEGORY_ECONOMIC_SELL: "Economic Sell",
    ACTIVITY_CATEGORY_ECONOMIC_DISCHARGE: "Economic Discharge",
    ACTIVITY_CATEGORY_CURTAILMENT: "Curtailment",
}

#: The one word a start and a finish are described with, per category.
_CATEGORY_VERBS = {
    ACTIVITY_CATEGORY_SAFETY_BUY: "Buy",
    ACTIVITY_CATEGORY_ECONOMIC_BUY: "Buy",
    ACTIVITY_CATEGORY_MIXED_BUY: "Buy",
    ACTIVITY_CATEGORY_ECONOMIC_SELL: "Sell",
    ACTIVITY_CATEGORY_ECONOMIC_DISCHARGE: "Discharge",
    ACTIVITY_CATEGORY_CURTAILMENT: "Curtailment",
}

#: The same word from the direction alone, for a lifecycle adopted from a running
#: dispatch after a reload -- where the category that produced it is no longer
#: knowable and inventing one would be a guess about the user's money.
_DIRECTION_VERBS = {
    ECONOMIC_DIRECTION_CHARGE: "Buy",
    ECONOMIC_DIRECTION_DISCHARGE: "Sell",
}

#: Which direction each category moves the battery.
_CATEGORY_DIRECTIONS = {
    ACTIVITY_CATEGORY_SAFETY_BUY: ECONOMIC_DIRECTION_CHARGE,
    ACTIVITY_CATEGORY_ECONOMIC_BUY: ECONOMIC_DIRECTION_CHARGE,
    ACTIVITY_CATEGORY_MIXED_BUY: ECONOMIC_DIRECTION_CHARGE,
    ACTIVITY_CATEGORY_ECONOMIC_SELL: ECONOMIC_DIRECTION_DISCHARGE,
    ACTIVITY_CATEGORY_ECONOMIC_DISCHARGE: ECONOMIC_DIRECTION_DISCHARGE,
}

#: Why a lifecycle ended without succeeding, in a phrase rather than a token.
#:
#: No internal vocabulary reaches this surface: no module names, no state names,
#: no snake_case. A reader is told what happened to their battery, not which
#: branch fired -- the branch is in diagnostics, where it belongs.
_CANCEL_REASONS: dict[str, str] = {
    EXECUTION_STOP_BATTERY_CEILING: "Battery Limit Reached",
    EXECUTION_STOP_PLAN_REPLACED: "Plan Replaced",
    EXECUTION_STOP_WINDOW_ENDED: "Window Expired",
    EXECUTION_STOP_STALE_PLAN: "Plan Expired",
    EXECUTION_STOP_STAGE_A_HOLD: "No Longer Economically Valid",
    EXECUTION_STOP_SWITCHED_TO_SHADOW: "Control Mode Changed",
    EXECUTION_STOP_SWITCHED_OFF: "Control Mode Changed",
    EXECUTION_STOP_OWNERSHIP_CONFLICT: "Ownership Lost",
    EXECUTION_STOP_SAFETY: "Safety Stop",
    EXECUTION_STOP_HEADROOM_REACHED: "Headroom Reached",
    EXECUTION_STOP_GRID_CEILING: "Grid Limit Reached",
}

#: Why a lifecycle ended in a failure. Separate from the map above because the
#: distinction is the one a reader most needs: a cancelled plan is the optimizer
#: changing its mind, and an error is something that needs looking at.
_ERROR_REASONS: dict[str, str] = {
    EXECUTION_STOP_EXECUTION_ERROR: "Command Failed",
    EXECUTION_STOP_TIMER_NOT_REFRESHED: "Timer Not Refreshed",
    # **Two reasons the controller has always produced and no map ever named.**
    # Both fell through to "Plan Replaced", which described a marker vanishing
    # under a live dispatch and a restart losing a quarter's measurement as the
    # optimiser changing its mind. ``No Charge Limit`` and ``Reserve Limit
    # Reached`` went the other way: named here, and assigned nowhere in
    # production. Green tests on inputs the pipeline could not produce, which is
    # what ``test_every_activity_reason_is_reachable_from_production_code``
    # now forbids.
    EXECUTION_STOP_MARKER_LOST: "Ownership Marker Lost",
    EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN: "Progress Unknown After Restart",
}

#: What to say when a plan is withdrawn and no stop reason was recorded, which is
#: every Stage-A retraction: the plan simply stopped being in the plan.
_CANCEL_REPLACED = "Plan Replaced"
_CANCEL_EXPIRED = "Window Expired"

#: The marker a Shadow line carries. One word, appended, and that is deliberate:
#: through beta.30 every advisory line carried a whole sentence -- "Advisory only:
#: no command is sent for this action." -- repeated on line after line until a
#: reader stopped seeing it. A disclaimer nobody reads is worse than a short one
#: they do.
_SHADOW_MARKER = "Shadow"

#: The same, for an action no actuator in this release can perform. Distinct from
#: Shadow because the causes are different and a user can act on one of them: the
#: mode is theirs to change, the missing actuator is not.
_ADVISORY_MARKER = "Advisory"


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Which *run* this is: what the battery does, and when it begins.

    Retained from beta.16, and no longer the lifecycle key -- see
    :class:`PlanIdentity` for why. It is still what :func:`_due` reads, because
    whether a run is worth announcing is a question about its **start**.
    """

    direction: str
    start_utc: datetime


@dataclass(frozen=True, slots=True)
class PlanIdentity:
    """Which *plan* this is, in terms that survive twenty re-solves.

    ``category`` rather than the action label, and the window's **end** rather
    than its start.

    The end, because the horizon's head is ``elapsed_intervals + 1``: a run
    already under way loses its leading interval on every refresh, so its start
    advances every fifteen minutes while the decision does not change at all. Its
    end does not move. Anchoring identity on the start is precisely what made one
    campaign announce itself six times.

    The category, because "Safety Buy becomes Economic Buy" is a genuinely
    different plan -- the money is being spent for a different reason -- while
    ``discharge`` flipping to ``export`` under a varying house load is not.
    """

    category: str
    end_utc: datetime


@dataclass(frozen=True, slots=True)
class RunContent:
    """What a lifecycle line may say about a run. Deliberately four fields.

    **What is missing is the design.** Through beta.30 this carried the first and
    mean and peak power, the battery energy beside the grid energy, the average
    price, the expected value, the charge source and the solver's reason -- and
    the sentence built from them ran to three clauses and two disclaimers. All of
    it is still published, in diagnostics and in the entity's attributes, which is
    where a reader who wants it goes.

    Removing the fields rather than declining to print them is what makes "no kW
    in Activity" a property of the code instead of an assertion about a string.
    """

    category: str
    #: What the plan moves, at the boundary the plan is paid at. One figure.
    energy_kwh: float
    end_utc: datetime
    #: The window as local wall-clock text, resolved by the caller that owns the
    #: calendar. This module reads no clock and knows no timezone.
    window: str
    #: Whether an actuator in this release can perform the action at all.
    executable: bool = True


@dataclass(frozen=True, slots=True)
class PlannedRun:
    """One run of the current plan, with its instants already resolved.

    Built by the caller, which owns the calendar. Keeping index-to-instant
    arithmetic out of this module is what lets the whole lifecycle policy be
    exercised against plain values.
    """

    identity: RunIdentity
    content: RunContent

    @property
    def plan_identity(self) -> PlanIdentity:
        """Return the lifecycle this run belongs to."""
        return PlanIdentity(
            category=self.content.category, end_utc=self.content.end_utc
        )


@dataclass(frozen=True, slots=True)
class TerminalView:
    """A campaign that has ended, as the layer that measured it reported it.

    **Activity decides nothing here, and that is the point of the class.**
    Through beta.31 the terminal was inferred from ``Decision.stop_reason``: a
    0.014 kWh residue became a cancellation because a tolerance lived in this
    module, and a real 0.10 / 0.11 kWh export was filed "Canceled -- Plan
    Replaced" because the reason was read as the outcome. Since beta.29 the
    hardware is armed from the admitted quarter and stopped from the 60-second
    tick, so ``Decision`` had not been the executor for two releases and this
    surface was still wired to it.

    Now the coordinator computes the class where the energy was measured, latches
    it before anything can be wiped, and this module renders four shapes.

    **One energy pair, at the objective's own boundary.** Not a battery pair
    beside a meter pair for a renderer to choose between -- choosing is what put
    a battery ceiling in a sentence about a meter objective.
    """

    campaign_id: str
    outcome: str
    objective_target_kwh: float | None = None
    objective_realized_kwh: float = 0.0
    objective_boundary: str | None = None
    reason: str | None = None
    #: Whether the realised figure is a measurement at all. False outranks a met
    #: objective in the outcome precedence, and the coordinator has already
    #: applied that -- this is carried so the line can say so.
    measurable: bool = True
    started: bool = True


@dataclass(frozen=True, slots=True)
class ExecutionView:
    """What Stage B is doing, in the few terms Activity needs.

    A deliberately narrow reading rather than the controller's own state. Activity
    must not be able to reach into the controller and start describing its
    arithmetic -- the quarter-by-quarter setpoint corrections are exactly what
    this surface must never carry, and the cheapest way to guarantee that is not
    to hand them over.

    ``executed`` is what separates Shadow from Live, and in beta.31 it does more
    than choose a wording: while it is false **no start, success or error is
    emitted at all**. Shadow shows the planning lifecycle and stops there.
    """

    #: The run being executed, so a lifecycle can be matched to it by direction.
    identity: RunIdentity | None = None
    #: When the admitted run's window closes -- the lifecycle anchor, so a running
    #: dispatch attaches to the plan that was announced for it.
    end_utc: datetime | None = None
    #: Whether a dispatch is actually under way.
    running: bool = False
    #: Whether a command physically went out.
    executed: bool = False
    #: The campaign objective and what was realised against it, **both at the
    #: boundary the objective is stated at**. Replaces beta.31's ``target_kwh`` /
    #: ``delivered_kwh``, which were battery-side whatever the plan was aiming at:
    #: for an export run that published ``Tracking 0.25 kWh`` -- the battery
    #: ceiling -- beside ``Planned ... 0.11 kWh``, the meter objective.
    objective_target_kwh: float = 0.0
    objective_realized_kwh: float = 0.0
    intent: str = ""
    stop_reason: str | None = None
    inhibit_reason: str | None = None
    #: The Stage-B run identity: minted once when a run is admitted and stable for
    #: the whole campaign. Carried on the lifecycle so a second campaign for the
    #: same window is a second lifecycle.
    run_id: str | None = None
    #: Whether a run is admitted and waiting for its window to open.
    prepared: bool = False
    #: Whether a write carrying an activation actually succeeded this refresh.
    #:
    #: **This, and nothing else, is what "started" may be said about.** Derived
    #: from the controller state it would have said "started" for an *armed*
    #: decision -- computed, sent nothing -- which on a release that writes is the
    #: one claim that must not be wrong.
    activation_confirmed: bool = False
    #: The campaign that has just ended, if one has. **Read first**, before any
    #: live intent: a campaign ends on a tick that publishes no plan at all, and
    #: requiring a live intent is why the incident's 17:45 refresh said nothing.
    terminal: TerminalView | None = None
    #: Whether a campaign is still open. **While this is true the campaign owns its
    #: own ending** and no run-level terminal may fire.
    #:
    #: Without it, a ``serve_load`` gap inside a discharge campaign terminates the
    #: lifecycle: the run is not running, no stop reason was recorded, and beta.31's
    #: fall-through reads that as ``Canceled -- Plan Replaced``. Harmless while an
    #: export run was one quarter wide; on the multi-quarter campaigns beta.32
    #: creates it would file a cancellation every time the house ate a quarter's
    #: worth of discharge, and then a second lifecycle when selling resumed.
    campaign_open: bool = False

    @property
    def deviation_kwh(self) -> float:
        """Return how far delivery landed from the objective. Signed."""
        return self.objective_realized_kwh - self.objective_target_kwh


@dataclass(frozen=True, slots=True)
class Lifecycle:
    """One plan, and everything already said about it.

    The record *is* the deduplication. "Planned appears exactly once" is not a
    filter applied to a stream of candidate events; it is the absence of any path
    from an existing record to a second Planned line.
    """

    plan_id: str
    identity: PlanIdentity
    direction: str
    #: What was announced, so a revision can be measured against it rather than
    #: against the previous revision -- a deadband measured from a moving value
    #: ratchets, and this one cannot.
    energy_kwh: float
    window: str
    started: bool = False
    #: The Stage-B run this lifecycle was executed by, once one exists.
    run_id: str | None = None
    #: Whether the plan was only ever advice: Shadow, or an action with no
    #: actuator. Carried so the terminal line marks itself the same way the
    #: Planned line did.
    advisory: bool = False
    shadow: bool = False


@dataclass(frozen=True, slots=True)
class ActivityState:
    """Every lifecycle in flight, plus the ones already closed. Bounded.

    Reset by a reload, which costs at most one redundant line and buys not
    persisting a logbook cursor. The alternative -- storing it -- would make an
    observational surface a thing that can be restored wrong.
    """

    open: tuple[Lifecycle, ...] = ()
    #: Plan ids whose terminal event has been emitted.
    #:
    #: **Why a separate set rather than deleting the record.** A plan id is derived
    #: deterministically from its identity, so a plan that terminates and then
    #: reappears in a later solve would otherwise be announced again under the same
    #: id -- "Finished, then Planned" for one plan, which the lifecycle forbids. A
    #: closed id is closed for the session.
    closed: tuple[str, ...] = ()
    #: The inhibit reason last spoken about, so a standing condition is reported on
    #: the transition and then left alone.
    inhibit_reason: str | None = None

    def find(self, identity: PlanIdentity) -> Lifecycle | None:
        """Return the open lifecycle this plan belongs to, or ``None``.

        Matched on the category exactly and the window's end **within one
        planning interval**, because one interval is precisely the drift a
        re-solve introduces: the horizon head advances by one, and a run's last
        interval can be trimmed or extended by one as demand is revised. More
        than that is a different window, and a different window is a different
        plan.
        """
        for entry in self.open:
            if entry.identity.category != identity.category:
                continue
            if _within_window_tolerance(entry.identity.end_utc, identity.end_utc):
                return entry
        return None

    def with_open(self, lifecycle: Lifecycle) -> ActivityState:
        """Return this state with one lifecycle recorded or replaced."""
        kept = tuple(e for e in self.open if e.plan_id != lifecycle.plan_id)
        return ActivityState(
            # Newest last, oldest dropped first: the cap can only ever discard a
            # lifecycle whose window is furthest behind us.
            open=(*kept, lifecycle)[-MAX_ECONOMIC_RUNS_TRACKED:],
            closed=self.closed,
            inhibit_reason=self.inhibit_reason,
        )

    def with_closed(self, plan_id: str) -> ActivityState:
        """Return this state with one lifecycle terminated."""
        return ActivityState(
            open=tuple(e for e in self.open if e.plan_id != plan_id),
            closed=(*(c for c in self.closed if c != plan_id), plan_id)[
                -MAX_ECONOMIC_RUNS_TRACKED:
            ],
            inhibit_reason=self.inhibit_reason,
        )

    def with_inhibit(self, reason: str | None) -> ActivityState:
        """Return this state with the spoken-about inhibit reason replaced."""
        return ActivityState(open=self.open, closed=self.closed, inhibit_reason=reason)


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    """One line for the logbook, and the state to carry into the next refresh."""

    kind: str
    message: str
    state: ActivityState
    #: The lifecycle the line belongs to, so a reader of the event -- or a test --
    #: can group three lines without parsing the sentence.
    plan_id: str | None = None


def next_activity(
    *,
    previous: ActivityState | None,
    runs: tuple[PlannedRun, ...],
    now: datetime,
    execution: ExecutionView | None = None,
    shadow: bool = False,
) -> ActivityEntry | None:
    """Return the one entry this refresh deserves, or ``None`` for silence.

    Silence is the common case and the default. At most one entry is returned per
    refresh; anything else waits for the next one, which is fifteen minutes away
    and self-healing.

    **The signature widened again in beta.31, and as before that is the point.**
    This module's contract is that anything it can describe must be an argument,
    so a surface that gains a voice does so visibly. ``shadow`` is the fourth such
    widening: whether the integration is permitted to write is a property of the
    whole surface rather than of one run or of Stage B's report, and it decides
    something structural -- while it is true, no start, success or error is
    emitted at all.

    Priority: a terminal first, then a start, then a retraction, then an
    announcement, then the standing conditions. A terminal leads because an
    ending that goes unrecorded leaves the previous line standing as though it
    were still true, which is the fault beta.31 exists to fix.

    What is deliberately silent: **every routine refresh.** A plan whose target
    moved by less than a bucket, or whose window end moved by no more than one
    planning interval, produces nothing here. That is arithmetic rather than a
    decision, and reporting it was the entire source of the old spam.
    """
    state = previous or ActivityState()

    if execution is not None:
        entry = _terminal_entry(state, execution, now=now)
        if entry is not None:
            return entry
        entry = _started_entry(state, execution, now=now)
        if entry is not None:
            return entry

    live = {}
    for run in runs:
        identity = run.plan_identity
        live[identity] = run

    # A plan we spoke about is no longer in the plan, and no Stage-B run accounted
    # for it. Said before anything new, so a history never shows two open plans
    # for one window.
    for lifecycle in state.open:
        if any(
            lifecycle.identity.category == identity.category
            and _within_window_tolerance(lifecycle.identity.end_utc, identity.end_utc)
            for identity in live
        ):
            continue
        expired = lifecycle.identity.end_utc <= now
        reason = _CANCEL_EXPIRED if expired else _CANCEL_REPLACED
        # **The figures come from the plan being cancelled, not from Stage B's
        # current target.** A campaign that had started still reports how far it
        # got -- a superseded plan that moved 1.4 kWh is a different thing from one
        # that never began -- but the *denominator* has to be what this plan
        # announced. Stage B's target has already been revised by the refresh that
        # replaced the plan, so quoting it would put the new plan's figure beside
        # the old plan's progress.
        delivered = None if execution is None else execution.objective_realized_kwh
        return _cancelled(state, lifecycle, reason, delivered_kwh=delivered)

    # Something new is imminent. Once per plan id, ever.
    for run in runs:
        identity = run.plan_identity
        if state.find(identity) is not None:
            continue
        plan_id = plan_id_for(identity)
        if plan_id in state.closed:
            continue
        if not _due(run, now=now):
            continue
        advisory = not run.content.executable
        lifecycle = Lifecycle(
            plan_id=plan_id,
            identity=identity,
            direction=_direction_for(identity.category),
            energy_kwh=run.content.energy_kwh,
            window=run.content.window,
            advisory=advisory,
            shadow=shadow,
        )
        return ActivityEntry(
            kind=ECONOMIC_EVENT_PLANNED,
            message=_planned_message(lifecycle),
            state=state.with_open(lifecycle),
            plan_id=plan_id,
        )

    if execution is not None:
        # **Adoption comes last, and the ordering is load-bearing.** A start with
        # no lifecycle to attach to is only an anomaly once the plan has had its
        # turn to announce one. Reached before the announcement it produced a
        # second "Started" line under a second plan id for one physical dispatch,
        # because a refresh spent on a cancellation left the replacement plan
        # unannounced and the next refresh's start had nothing to match.
        entry = _started_entry(state, execution, now=now, adopt=True)
        if entry is not None:
            return entry
        entry = _inhibit_entry(state, execution)
        if entry is not None:
            return entry

    return None


# ---------------------------------------------------------------------------
# the lifecycle transitions
# ---------------------------------------------------------------------------


def _terminal_entry(
    state: ActivityState, execution: ExecutionView, *, now: datetime
) -> ActivityEntry | None:
    """Return the one terminal line a finished dispatch deserves, or ``None``.

    Reachable only in Live: a Shadow lifecycle ends through the retraction path
    above, because a plan that never physically started cannot have succeeded or
    failed at anything.

    **Exactly one, and it is structural.** The lifecycle is closed in the state
    returned with the line, and a closed plan id matches nothing afterwards -- so
    a hundred further refreshes carrying the same ``stop_reason`` produce nothing.
    """
    # **The latched campaign outcome comes first, and it needs no live intent.**
    # This single reordering is what lets a campaign that ended on the 60-second
    # tick speak at all: that tick wipes the carriers and publishes no plan, so
    # every beta.31 path into this function had already lost its subject.
    if execution.terminal is not None:
        entry = _campaign_terminal(state, execution, execution.terminal)
        if entry is not None:
            return entry

    if not execution.executed:
        return None
    if execution.campaign_open:
        # **The campaign has not closed, so it has not ended.** A gap between two
        # export segments is a refresh with nothing running and no stop reason, and
        # the run-level fall-through below would call that a cancellation.
        return None
    lifecycle = _lifecycle_for(state, execution)
    if lifecycle is None or not lifecycle.started:
        return None
    if execution.running and execution.stop_reason is None:
        return None
    reason = execution.stop_reason or ""

    if reason == EXECUTION_STOP_TARGET_REACHED:
        return _finished(state, lifecycle, execution)
    if reason in _ERROR_REASONS:
        return _failed(state, lifecycle, _ERROR_REASONS[reason])
    return _cancelled(
        state,
        lifecycle,
        _CANCEL_REASONS.get(reason, _CANCEL_REPLACED),
        execution=execution,
    )


def _campaign_terminal(
    state: ActivityState, execution: ExecutionView, terminal: TerminalView
) -> ActivityEntry | None:
    """Render the campaign outcome the coordinator computed. Four shapes.

    **Renders, never decides.** The class arrives already settled -- computed
    where the energy was measured, under a precedence stated once in
    ``_close_campaign`` -- so this function contains no tolerance, no comparison
    and no reason-to-outcome mapping. Everything beta.31 got wrong here was a
    decision this layer had no business making.

    ``None`` when there is no lifecycle to attach the ending to, or when it has
    already been closed: a terminal for a campaign nobody announced would be a
    line about a plan the history has never seen.
    """
    lifecycle = _lifecycle_for(state, execution)
    if lifecycle is None or not lifecycle.started:
        return None
    if not terminal.started:
        return None
    figures = f"{terminal.objective_realized_kwh:.2f}"
    if terminal.objective_target_kwh is not None:
        figures += f" / {terminal.objective_target_kwh:.2f}"
    figures += " kWh"

    if terminal.outcome == OUTCOME_SUCCESS:
        return ActivityEntry(
            kind=ECONOMIC_EVENT_FINISHED,
            message=(
                f"Finished Plan ID: {lifecycle.plan_id} — Success — "
                f"Target Reached — {figures}"
            ),
            state=state.with_closed(lifecycle.plan_id),
            plan_id=lifecycle.plan_id,
        )
    if terminal.outcome == OUTCOME_PARTIAL:
        # **Partial is its own word, and it is the honest one.** beta.31 had only
        # Success and Canceled, so a campaign that delivered most of what it
        # promised had to be filed as one or the other -- and it was filed as a
        # cancellation, which reads as though nothing happened.
        return ActivityEntry(
            kind=ECONOMIC_EVENT_FINISHED,
            message=(f"Finished Plan ID: {lifecycle.plan_id} — Partial — {figures}"),
            state=state.with_closed(lifecycle.plan_id),
            plan_id=lifecycle.plan_id,
        )
    if terminal.outcome == OUTCOME_FAILED:
        detail = (
            "Measurement Unavailable"
            if not terminal.measurable
            else _ERROR_REASONS.get(
                terminal.reason or "", _humanise(terminal.reason) or "Command Failed"
            )
        )
        return _failed(state, lifecycle, detail, figures=figures)
    detail = _CANCEL_REASONS.get(terminal.reason or "", _CANCEL_REPLACED)
    line = f"Canceled Plan ID: {lifecycle.plan_id} — {detail} — {figures}"
    return ActivityEntry(
        kind=ECONOMIC_EVENT_CANCELLED,
        message=_marked(line, lifecycle),
        state=state.with_closed(lifecycle.plan_id),
        plan_id=lifecycle.plan_id,
    )


def _failed(
    state: ActivityState,
    lifecycle: Lifecycle,
    detail: str,
    *,
    figures: str | None = None,
) -> ActivityEntry:
    """Return the failure line for a campaign that ended badly.

    ``Failed Plan ID:`` rather than beta.31's ``Finished ... — Error``, which was
    self-contradictory in four words. The event **kind** is unchanged, so
    ``logbook_payload``'s refusal guard and every enum option are untouched: what
    changed is the sentence, not the vocabulary a consumer subscribes to.
    """
    line = f"Failed Plan ID: {lifecycle.plan_id} — {detail}"
    if figures is not None:
        line += f" — {figures}"
    return ActivityEntry(
        kind=ECONOMIC_EVENT_ERROR,
        message=line,
        state=state.with_closed(lifecycle.plan_id),
        plan_id=lifecycle.plan_id,
    )


def _started_entry(
    state: ActivityState,
    execution: ExecutionView,
    *,
    now: datetime,
    adopt: bool = False,
) -> ActivityEntry | None:
    """Return the line for a dispatch beginning, or ``None``.

    **Live only, and only once a write carrying an activation has succeeded.** An
    armed decision has computed a power and sent nothing; calling that "started"
    would be the one claim a release that writes must not get wrong.

    Shadow is answered structurally rather than by wording. Through beta.30 it
    emitted ``would_start`` / ``would_stop``, which meant a Shadow history looked
    exactly as busy as a Live one and every line needed a disclaimer to stay
    honest. It now emits nothing here at all: Shadow shows what was planned, and
    diagnostics show what would have been sent.
    """
    if not execution.executed or not execution.activation_confirmed:
        return None
    if not execution.intent:
        return None
    lifecycle = _lifecycle_for(state, execution)
    if lifecycle is not None and lifecycle.started:
        return None
    if lifecycle is None:
        # A dispatch under way with no lifecycle to attach it to. Deferred until
        # after the announcement pass -- see the call site -- so that a plan which
        # simply has not been announced yet is announced rather than adopted under
        # a synthetic id.
        if not adopt:
            return None
        # What remains is a dispatch this session never announced and the plan no
        # longer contains: a reload mid-campaign, essentially. Adopted rather than
        # skipped, because a start that goes unrecorded leaves the later terminal
        # line referring to a plan id the history has never seen. The category is
        # unknowable at this point and is left empty rather than guessed.
        lifecycle = _adopted(execution)
        if lifecycle is None:
            return None
        if lifecycle.plan_id in state.closed:
            return None

    started = Lifecycle(
        plan_id=lifecycle.plan_id,
        identity=lifecycle.identity,
        direction=lifecycle.direction,
        energy_kwh=lifecycle.energy_kwh,
        window=lifecycle.window,
        started=True,
        run_id=execution.run_id,
        advisory=lifecycle.advisory,
        shadow=lifecycle.shadow,
    )
    verb = _verb_for(lifecycle)
    return ActivityEntry(
        kind=ECONOMIC_EVENT_STARTED,
        message=(
            f"Plan ID: {started.plan_id} — {verb} Started — "
            f"Tracking {execution.objective_target_kwh:.2f} kWh"
        ),
        state=state.with_open(started),
        plan_id=started.plan_id,
    )


def _inhibit_entry(
    state: ActivityState, execution: ExecutionView
) -> ActivityEntry | None:
    """Return the line for a standing condition beginning or ending.

    Transitions only: repeating an inhibit every refresh is the exact spam this
    surface exists to avoid. Not part of any plan's lifecycle, so it carries no
    plan id -- it is a statement about the pipeline, not about a purchase.
    """
    if execution.inhibit_reason == state.inhibit_reason:
        return None
    began = execution.inhibit_reason is not None
    return ActivityEntry(
        kind=ECONOMIC_EVENT_INHIBITED if began else ECONOMIC_EVENT_AVAILABLE,
        message=(
            f"Execution Inhibited — {_humanise(execution.inhibit_reason)}"
            if began
            else "Execution Available"
        ),
        state=state.with_inhibit(execution.inhibit_reason),
    )


def _finished(
    state: ActivityState, lifecycle: Lifecycle, execution: ExecutionView
) -> ActivityEntry:
    """Return the success line for a run Stage B reported as complete.

    **The tolerance has gone from this module**, and its absence is the fix. It
    lived here as ``TARGET_TOLERANCE_KWH``, so a presentation layer decided
    whether a 0.014 kWh residue was a success -- 0.56 of one actuator step, which
    no command could have closed. The outcome class now arrives already decided
    from where the energy was measured (see :class:`TerminalView`), and this path
    remains only for a run-level stop with no campaign behind it, where Stage B's
    own ``target_reached`` *is* the verdict.
    """
    return ActivityEntry(
        kind=ECONOMIC_EVENT_FINISHED,
        message=(
            f"Finished Plan ID: {lifecycle.plan_id} — Success — Target Reached — "
            f"{execution.objective_realized_kwh:.2f} / "
            f"{execution.objective_target_kwh:.2f} kWh"
        ),
        state=state.with_closed(lifecycle.plan_id),
        plan_id=lifecycle.plan_id,
    )


def _cancelled(
    state: ActivityState,
    lifecycle: Lifecycle,
    reason: str,
    *,
    execution: ExecutionView | None = None,
    delivered_kwh: float | None = None,
) -> ActivityEntry:
    """Return the line for a plan that ended without succeeding.

    The figures appear only when the plan had actually started. A plan withdrawn
    before its window opened moved no energy, so quoting ``0.00 / 2.22 kWh``
    beside it would invite a reader to look for a fault where there is only a
    change of mind.

    Two sources for the pair, because there are two situations. When **Stage B**
    ended the run, its own target is the right denominator -- that is the number
    the run was tracking. When the **plan** withdrew it, Stage B's target has
    already moved on, so the denominator is what this plan announced.
    """
    line = f"Canceled Plan ID: {lifecycle.plan_id} — {reason}"
    if lifecycle.started and execution is not None:
        line += (
            f" — {execution.objective_realized_kwh:.2f}"
            f" / {execution.objective_target_kwh:.2f} kWh"
        )
    elif lifecycle.started and delivered_kwh is not None:
        line += f" — {delivered_kwh:.2f} / {lifecycle.energy_kwh:.2f} kWh"
    return ActivityEntry(
        kind=ECONOMIC_EVENT_CANCELLED,
        message=_marked(line, lifecycle),
        state=state.with_closed(lifecycle.plan_id),
        plan_id=lifecycle.plan_id,
    )


def _planned_message(lifecycle: Lifecycle) -> str:
    """Return the line for a plan that has just been made.

    Four facts in one line: which plan, what kind, when, how much. That is the
    whole of it, and the beta.30 line it replaces ran to two sentences, five
    figures at three different boundaries and a disclaimer.
    """
    label = _CATEGORY_LABELS.get(lifecycle.identity.category, "Plan")
    line = (
        f"Plan ID: {lifecycle.plan_id} — {label} Planned — "
        f"{lifecycle.window} — {lifecycle.energy_kwh:.2f} kWh"
    )
    return _marked(line, lifecycle)


def _marked(line: str, lifecycle: Lifecycle) -> str:
    """Append the Shadow or Advisory marker, if either applies.

    One word each, and never both: Shadow subsumes the question, because in
    Shadow nothing is sent whatever the actuator could have done.
    """
    if lifecycle.shadow:
        return f"{line} — {_SHADOW_MARKER}"
    if lifecycle.advisory:
        return f"{line} — {_ADVISORY_MARKER}"
    return line


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def plan_id_for(identity: PlanIdentity) -> str:
    """Return the short, stable, user-visible id for a plan.

    Derived from the identity rather than minted from a counter, which buys two
    things. A reload mid-campaign recovers the same id, so a history does not
    show one plan under two names. And the id is reproducible from a diagnostic:
    a reader with the category and the window end can compute it and confirm
    which lines belong together.

    Six hex characters. Enough that two plans in one horizon will not collide,
    short enough to read out loud, and not a UUID -- an id a person cannot say is
    an id a person will not use.
    """
    minute = identity.end_utc.replace(second=0, microsecond=0)
    digest = sha1(f"{identity.category}|{minute.isoformat()}".encode())
    return digest.hexdigest()[:6]


def _within_window_tolerance(left: datetime, right: datetime) -> bool:
    """Return whether two window ends mean the same window."""
    return abs((left - right).total_seconds()) <= ECONOMIC_DEADBAND_MINUTES * 60


def _lifecycle_for(state: ActivityState, execution: ExecutionView) -> Lifecycle | None:
    """Return the open lifecycle a running dispatch belongs to, or ``None``.

    By ``run_id`` first, which is exact: it is minted once when a run is admitted
    and stable for the whole campaign, so a lifecycle that has already been
    attached stays attached however far the window is later revised.

    Otherwise by direction and window end, which is how the *first* attachment
    happens -- Stage B's admitted run and Stage A's planned run are the same run,
    so they agree on both.
    """
    if execution.run_id is not None:
        for entry in state.open:
            if entry.run_id == execution.run_id:
                return entry
    direction = None if execution.identity is None else execution.identity.direction
    if direction is None or execution.end_utc is None:
        return None
    for entry in state.open:
        if entry.direction != direction:
            continue
        if _within_window_tolerance(entry.identity.end_utc, execution.end_utc):
            return entry
    return None


def _adopted(execution: ExecutionView) -> Lifecycle | None:
    """Return a lifecycle inferred from a dispatch nobody announced.

    Only reachable after a reload during a live campaign. The category is left
    empty because it is genuinely not knowable from Stage B's report, and a
    guessed one would be a claim about why the user's money is being spent.
    """
    if execution.identity is None or execution.end_utc is None:
        return None
    identity = PlanIdentity(category="", end_utc=execution.end_utc)
    return Lifecycle(
        plan_id=plan_id_for(identity),
        identity=identity,
        direction=execution.identity.direction,
        energy_kwh=execution.objective_target_kwh,
        window="",
        run_id=execution.run_id,
    )


def _direction_for(category: str) -> str:
    """Return the direction a category moves the battery."""
    return _CATEGORY_DIRECTIONS.get(category, category)


def _verb_for(lifecycle: Lifecycle) -> str:
    """Return the one word a start is described with."""
    verb = _CATEGORY_VERBS.get(lifecycle.identity.category)
    if verb is not None:
        return verb
    return _DIRECTION_VERBS.get(lifecycle.direction, "Plan")


def _humanise(token: str | None) -> str:
    """Return an internal token as something a person can read.

    A deliberate compromise, and the only one on this surface. The safety gate
    has more than twenty refusal reasons and they are genuinely informative, so
    an explicit table would either go stale or flatten them all to "blocked".
    Splitting on the underscore and capitalising is deterministic, adds no
    vocabulary of its own, and cannot silently stop matching a reason the gate
    starts emitting.
    """
    if not token:
        return "Unknown"
    return token.replace("_", " ").title()


def _due(run: PlannedRun, *, now: datetime) -> bool:
    """Return whether this run is close enough to be worth announcing.

    Four cases, and the last two are what keep the log honest:

    * more than one planning interval away -- **silent.** The plan is rebuilt
      every quarter and its far end moves constantly; announcing that is what
      produced an entry every fifteen minutes about a run eighteen hours out.
    * within one planning interval of starting -- announce. This is the last
      refresh before it begins.
    * already started and never announced -- announce once, which covers a reload
      and a plan that first appears after its own start.
    * already **finished** and never announced -- **silent.** Back-dating an
      announcement for a window that has closed would describe a decision nobody
      could act on.
    """
    lead = run.identity.start_utc - now
    if lead > timedelta(minutes=ECONOMIC_ANNOUNCE_LEAD_MINUTES):
        return False
    if lead.total_seconds() > 0:
        return True
    return run.content.end_utc > now


def materially_changed(lifecycle: Lifecycle, run: PlannedRun) -> bool:
    """Return whether a re-solve moved a plan enough to be a different plan.

    **Published, and it answers nothing that the identity does not.** That is the
    point of the beta.31 shape and the reason this function exists only as
    documentation of it: a change big enough to matter changes the *identity* --
    the category, or the window end by more than one interval -- and therefore
    ends one lifecycle and begins another through the ordinary paths. A change too
    small to matter changes nothing at all.

    So the answer here is never used to decide whether to speak; it is used by the
    tests to state which of the two cases a given revision falls into, and by a
    reader trying to understand why a 13.33 -> 11.11 kWh revision that spoke six
    times in beta.30 is silent now.
    """
    identity = run.plan_identity
    if lifecycle.identity.category != identity.category:
        return True
    if not _within_window_tolerance(lifecycle.identity.end_utc, identity.end_utc):
        return True
    return abs(run.content.energy_kwh - lifecycle.energy_kwh) > (
        ECONOMIC_DEADBAND_ENERGY_KWH
    )


# ---------------------------------------------------------------------------
# the emitter boundary
# ---------------------------------------------------------------------------


def logbook_payload(entry: ActivityEntry, *, domain: str, entity_id: str) -> dict:
    """Return the event data for one logbook entry.

    Both ``domain`` and ``entity_id`` are set. The domain alone files the line
    under the integration but attaches it to nothing, so it would not appear on
    the entity's own history -- which is where a user looking at the economic
    action will look for it.

    Refuses an execution kind while the barrier stands. A guard rather than an
    assumption: if a later change makes one reachable on a release that sends
    nothing, the refusal is what stops it claiming the battery moved.
    """
    if entry.kind in ECONOMIC_EXECUTION_EVENT_KINDS and not CONTROL_EXECUTION_AVAILABLE:
        raise ValueError(
            f"Activity refuses the {entry.kind!r} entry: it describes execution, "
            "and nothing is executable in this configuration"
        )
    return {
        "name": ACTIVITY_NAME,
        "message": entry.message,
        "domain": domain,
        "entity_id": entity_id,
        # The lifecycle key, so a consumer can group three lines without parsing
        # the sentence. Absent for the two standing conditions, which belong to no
        # plan.
        **({} if entry.plan_id is None else {"plan_id": entry.plan_id}),
    }


def direction_of(action: str) -> str:
    """Return the battery direction an action label implies.

    The label is what a run is *called*; the direction is what the battery does.
    """
    if action in (ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_SAFETY_BUY):
        return ECONOMIC_DIRECTION_CHARGE
    if action in (ECONOMIC_ACTION_DISCHARGE, ECONOMIC_ACTION_EXPORT):
        return ECONOMIC_DIRECTION_DISCHARGE
    return action


def category_of(
    action: str,
    attribution: tuple[float, float] | None,
    *,
    sells: bool | None = None,
) -> str:
    """Return the plan category for one run.

    The buy categories come from the **purchase attribution**, which is the
    reserve-relaxed counterfactual's own split of compelled against
    discretionary energy -- the same pair
    :func:`economic.classify_purchase` labels a run with. So the words a user
    reads on the Activity line and the figures a reader audits in diagnostics
    come from one measurement, and cannot drift apart.

    A charge with no attribution at all is an economic buy: the counterfactual
    declined to buy anything, which is precisely what "nothing was compulsory"
    means.

    **``sells`` settles the discharge side, and it is a campaign question.** The
    label alternates between ``discharge`` and ``export`` under a varying house
    load, so deriving the category from the label alone gave one physical campaign
    two categories -- and therefore two lifecycles, two Planned lines and two
    terminals. A campaign either has a material meter objective or it does not, and
    that is what the caller passes here. ``None`` keeps the label-derived answer,
    so a pre-campaign caller still describes the behaviour it was written for.
    """
    if action in (ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_SAFETY_BUY):
        compelled, discretionary = attribution or (0.0, 0.0)
        if compelled > 0.0 and discretionary > 0.0:
            return ACTIVITY_CATEGORY_MIXED_BUY
        if compelled > 0.0:
            return ACTIVITY_CATEGORY_SAFETY_BUY
        return ACTIVITY_CATEGORY_ECONOMIC_BUY
    if action == ECONOMIC_ACTION_EXPORT:
        return ACTIVITY_CATEGORY_ECONOMIC_SELL
    if action == ECONOMIC_ACTION_DISCHARGE:
        if sells:
            return ACTIVITY_CATEGORY_ECONOMIC_SELL
        return ACTIVITY_CATEGORY_ECONOMIC_DISCHARGE
    if action == ECONOMIC_ACTION_CURTAIL:
        return ACTIVITY_CATEGORY_CURTAILMENT
    return action


__all__ = [
    "ACTIVITY_NAME",
    "ActivityEntry",
    "ActivityState",
    "ExecutionView",
    "Lifecycle",
    "PlanIdentity",
    "PlannedRun",
    "RunContent",
    "RunIdentity",
    "TerminalView",
    "category_of",
    "direction_of",
    "logbook_payload",
    "materially_changed",
    "next_activity",
    "plan_id_for",
]
