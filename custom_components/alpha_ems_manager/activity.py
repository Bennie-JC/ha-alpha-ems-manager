"""The Activity surface: what the optimizer intends, said once, when it matters.

Four properties define this module, and each one is enforced by the shape of the
code rather than by a convention:

**It is strictly observational.** :func:`next_activity` receives planned runs, the
previously announced ones and the current instant -- and nothing else. It cannot
see the plan object, the control report, the safety state or the recovery
machinery, because they are not arguments. A later phase that wants to log an
execution event has to change this signature, which is a visible act.

**Nothing reads it back.** No code in this integration subscribes to the event, no
figure is derived from it, and an installation with the recorder removed produces
identical numbers. Activity is a write-only tap.

**It cannot claim the battery did anything.** While
``CONTROL_EXECUTION_AVAILABLE`` is false the execution kind is refused outright,
and every advice entry carries the advisory qualifier. An Activity line reading
"charge started" on a release that sends no command would be a lie about the
hardware, which is the single failure mode this surface must not have.

**One message per run.** This is what beta.16 fixes, and it needs its own
explanation.

Why the old design spammed
--------------------------

Until beta.16 the decision to speak was a hash of the published run, keyed among
other things on ``start_index`` -- a chronological index counted from midnight of
the plan's target day. Three things followed, and all three were visible in the
live logbook:

* **An in-progress run churned every quarter.** The horizon begins at
  ``elapsed_intervals + 1``, so each refresh drops the leading interval of a
  running run: its ``start_index`` advances, its remaining energy shrinks, and the
  hash changed. One entry every fifteen minutes, for hours, about a decision that
  had not changed at all.
* **Midnight rebased everything.** Tomorrow's indices became today's, dropping by
  a whole day's worth with no change in wall-clock meaning.
* **Bucket boundaries flapped.** The figures were bucketed and hashed, so a
  drift of a hundredth of a kilowatt across a boundary spoke while a drift of a
  fifth of a kilowatt inside one stayed silent.

What replaces it
----------------

**Identity is separated from content.**

*Identity* is ``(direction, start_utc)``: the direction the battery moves and the
absolute instant the run begins. Direction rather than the action *label*, because
one physical discharge carries both ``discharge`` and ``export`` as house load
rises and falls beneath it -- the label changes, the decision does not. An
absolute instant rather than an index, because an index is relative to a day and a
horizon and neither is what a person means by "the two o'clock charge".

*Content* is what the sentence says: energy, power, end, price, character. It is
compared against the **announced** value with a deadband, never bucketed and
hashed. A deadband measured from the announced value cannot flap at a boundary;
that is the whole reason for the change.

**Announcement waits until the run is imminent.** A run more than one planning
interval away is silent, however many times the plan is recomputed. That single
rule removes the overwhelming majority of the old traffic, and it is what makes an
entry worth reading: it is about something that is about to happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .const import (
    CONTROL_EXECUTION_AVAILABLE,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_ANNOUNCE_LEAD_MINUTES,
    ECONOMIC_CHARGE_SOURCE_GRID,
    ECONOMIC_CHARGE_SOURCE_MIXED,
    ECONOMIC_CHARGE_SOURCE_PRODUCTION,
    ECONOMIC_DEADBAND_ENERGY_KWH,
    ECONOMIC_DEADBAND_MINUTES,
    ECONOMIC_DEADBAND_POWER_KW,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    ECONOMIC_EVENT_AVAILABLE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_CHANGED,
    ECONOMIC_EVENT_ENDED,
    ECONOMIC_EVENT_INHIBITED,
    ECONOMIC_EVENT_PLANNED,
    ECONOMIC_EVENT_REFUSED,
    ECONOMIC_EVENT_STARTED,
    ECONOMIC_EVENT_STOPPED,
    ECONOMIC_EVENT_WOULD_START,
    ECONOMIC_EVENT_WOULD_STOP,
    ECONOMIC_EXECUTION_EVENT_KINDS,
    ECONOMIC_GAP_FORECAST_INFEASIBLE,
    ECONOMIC_GAP_NONE,
    ECONOMIC_REASON_CHEAP_WINDOW,
    ECONOMIC_REASON_EXPENSIVE_WINDOW,
    ECONOMIC_REASON_MAKE_HEADROOM,
    ECONOMIC_REASON_NEGATIVE_EXPORT,
    ECONOMIC_REASON_NO_ACTION,
    ECONOMIC_REASON_RESERVE_RECOVERY,
    ECONOMIC_REASON_SAFETY_BUY,
    MAX_ECONOMIC_RUNS_TRACKED,
)

#: The name every entry is filed under. Fixed, so a user can filter the logbook on
#: it, and deliberately not the entity's friendly name -- that is renameable and a
#: filter built on it would silently stop matching.
ACTIVITY_NAME = "Economic plan"

#: The suffix that keeps an advisory entry honest. Appended whenever the global
#: execution barrier stands, which in this release is always.
_ADVISORY = "Advisory only: this release sends no command."

#: How each action reads in a sentence, as a verb phrase.
_VERBS = {
    ECONOMIC_ACTION_CHARGE: "charge the battery",
    ECONOMIC_ACTION_SAFETY_BUY: "buy energy to protect the reserve",
    ECONOMIC_ACTION_DISCHARGE: "discharge the battery to the house",
    ECONOMIC_ACTION_EXPORT: "export to the grid",
    ECONOMIC_ACTION_CURTAIL: "decline photovoltaic production",
    ECONOMIC_ACTION_HOLD: "hold",
}

#: How each reason reads. Bounded, like the vocabulary it renders.
_REASONS = {
    ECONOMIC_REASON_CHEAP_WINDOW: "the price is low in this window",
    ECONOMIC_REASON_EXPENSIVE_WINDOW: "the price is high in this window",
    ECONOMIC_REASON_SAFETY_BUY: "the reserve cannot be met from production",
    ECONOMIC_REASON_MAKE_HEADROOM: "room is needed for forecast production",
    ECONOMIC_REASON_NEGATIVE_EXPORT: "export is priced below zero",
    ECONOMIC_REASON_RESERVE_RECOVERY: "the reserve is short and must be restored",
    ECONOMIC_REASON_NO_ACTION: "no action pays for itself",
}

#: How a charge run's energy source reads. The distinction the live installation
#: made necessary: a run that put 4.48 kWh in the battery while importing 1.55 kWh
#: must not be described as buying 4.48 kWh.
_SOURCES = {
    ECONOMIC_CHARGE_SOURCE_PRODUCTION: "almost entirely from your own production",
    ECONOMIC_CHARGE_SOURCE_MIXED: "partly from your own production",
    ECONOMIC_CHARGE_SOURCE_GRID: "from the grid",
}


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Which run this is, in terms that survive a replan.

    ``direction`` rather than the action label, and an absolute instant rather
    than an index. Immune to horizon shifting, to midnight rebasing, and to a
    label flipping between ``discharge`` and ``export`` under a varying house
    load.
    """

    direction: str
    start_utc: datetime


@dataclass(frozen=True, slots=True)
class RunContent:
    """What the sentence says about a run, and what a change is measured on."""

    action: str
    capability_action: str
    reason: str
    #: The flow the action controls, at the boundary the action is paid at:
    #: battery AC for a charge or a discharge, **grid** export for an export,
    #: production declined for a curtailment.
    energy_kwh: float
    #: What the battery itself moved across the run, always at the battery. Held
    #: separately because for an export these are different quantities at
    #: different boundaries, and beta.16 put both in one sentence without saying
    #: so: "0.95 kW, 0.27 kWh" read as arithmetic and was not.
    battery_energy_kwh: float
    #: First-interval battery power, which is what the entity publishes.
    power_kw: float
    #: Mean battery power across the whole run. The figure that actually
    #: multiplies out against ``battery_energy_kwh``.
    average_power_kw: float
    end_utc: datetime
    charge_source: str
    price_eur_kwh: float | None
    value_eur: float
    refused: bool
    window: str
    #: Why the capability plan differs, when it does. Defaulted and last, so the
    #: dataclass ordering rule is not the thing that breaks.
    gap_reason: str = ECONOMIC_GAP_NONE


@dataclass(frozen=True, slots=True)
class PlannedRun:
    """One run of the current plan, with its instants already resolved.

    Built by the caller, which owns the calendar. Keeping index-to-instant
    arithmetic out of this module is what lets the whole announcement policy be
    exercised against plain values.
    """

    identity: RunIdentity
    content: RunContent


@dataclass(frozen=True, slots=True)
class AnnouncedRun:
    """A run that has already been spoken about, and what was said."""

    identity: RunIdentity
    content: RunContent


@dataclass(frozen=True, slots=True)
class ExecutionView:
    """What Stage B is doing, in the few terms Activity needs.

    A deliberately narrow reading rather than the controller's own state. Activity
    must not be able to reach into the controller and start describing its
    arithmetic -- the quarter-by-quarter setpoint corrections are precisely what
    this surface must never carry, and the cheapest way to guarantee that is not
    to hand them over.

    ``executed`` is what separates a Shadow line from a Live one. While the
    execution barrier stands it is always false, and the wording follows it: a
    Shadow run says what it *would* have done and never claims the battery moved.
    """

    #: The run being executed, so lifecycle lines share the plan's identity.
    identity: RunIdentity | None = None
    #: Whether a dispatch is actually under way.
    running: bool = False
    #: Whether a command physically went out. False for as long as
    #: :data:`~.const.CONTROL_EXECUTION_AVAILABLE` is, which is every release so
    #: far -- stated against the constant rather than a version number, so it
    #: cannot go stale the way a version can.
    executed: bool = False
    target_kwh: float = 0.0
    delivered_kwh: float = 0.0
    initial_power_kw: float = 0.0
    window: str = ""
    intent: str = ""
    stop_reason: str | None = None
    inhibit_reason: str | None = None

    @property
    def deviation_kwh(self) -> float:
        """Return how far delivery landed from the target."""
        return self.delivered_kwh - self.target_kwh


@dataclass(frozen=True, slots=True)
class ExecutionMemory:
    """What has already been said about execution, so it is not said twice.

    Separate from ``announced`` because the two answer different questions. A plan
    is announced once because it was *planned*; a dispatch is announced once
    because it *started*, and the same plan can be announced and then never start.
    Collapsing them would make a start event impossible to distinguish from the
    plan line that preceded it.
    """

    #: The intent whose start has been announced, if any.
    #:
    #: The **intent**, not the run identity, and the difference is load-bearing. A
    #: run driven by the reserve begins at *now*, so its start instant advances
    #: every quarter and its identity with it -- keying on that re-announced the
    #: same physical campaign every fifteen minutes, which is precisely the spam
    #: this surface exists to prevent.
    #:
    #: An intent changes when the campaign genuinely changes: charging becomes
    #: discharging, or serving load becomes exporting. Those are worth a line. A
    #: window sliding forward under a campaign that is still running is not.
    started: str | None = None
    #: The inhibit reason last spoken about, so a standing condition is reported
    #: on the transition and then left alone. Repeating it every refresh is the
    #: exact spam this surface exists to avoid.
    inhibit_reason: str | None = None
    #: The mode last spoken about.
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityState:
    """Every run announced so far, bounded.

    Reset by a reload, which costs at most one redundant line and buys not
    persisting a logbook cursor. The alternative -- storing it -- would make an
    observational surface a thing that can be restored wrong.
    """

    announced: tuple[AnnouncedRun, ...] = ()
    #: What has been said about execution. Session-scoped like the rest of this
    #: state, for the same reason: a reload costs at most one redundant line and
    #: buys not restoring an observational cursor wrongly.
    execution: ExecutionMemory = field(default_factory=ExecutionMemory)

    def with_execution(self, memory: ExecutionMemory) -> ActivityState:
        """Return this state with its execution memory replaced."""
        return ActivityState(announced=self.announced, execution=memory)

    def find(self, identity: RunIdentity) -> AnnouncedRun | None:
        """Return the announced record for ``identity``, or ``None``."""
        for entry in self.announced:
            if entry.identity == identity:
                return entry
        return None

    def with_announced(self, run: PlannedRun) -> ActivityState:
        """Return this state plus (or updating) one announced run."""
        kept = tuple(e for e in self.announced if e.identity != run.identity)
        record = AnnouncedRun(identity=run.identity, content=run.content)
        # Newest last, oldest dropped first: the cap can only ever discard a run
        # whose window is furthest behind us.
        return ActivityState(
            announced=(*kept, record)[-MAX_ECONOMIC_RUNS_TRACKED:],
            execution=self.execution,
        )

    def without(self, identity: RunIdentity) -> ActivityState:
        """Return this state with one announced run forgotten."""
        return ActivityState(
            announced=tuple(e for e in self.announced if e.identity != identity),
            execution=self.execution,
        )


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    """One line for the logbook, and the state to carry into the next refresh."""

    kind: str
    message: str
    state: ActivityState


def next_activity(
    *,
    previous: ActivityState | None,
    runs: tuple[PlannedRun, ...],
    now: datetime,
    execution: ExecutionView | None = None,
) -> ActivityEntry | None:
    """Return the one entry this refresh deserves, or ``None`` for silence.

    Silence is the common case and the default. At most one entry is returned per
    refresh; anything else waits for the next one, which is fifteen minutes away
    and self-healing.

    **The signature widened in beta.19, and that was the point.** This module used
    to be unable to describe execution because execution was not an argument, and
    the docstring said a later phase wanting to log one would have to change the
    signature -- "which is a visible act". This is that act. What has *not*
    changed is the discipline: ``execution`` is a narrow view rather than the
    controller's state, so Activity still cannot reach the rolling setpoint.

    Priority: execution lifecycle first, then retraction, then change, then
    announcement. Execution leads because a dispatch that started or stopped is a
    thing that happened to the battery, and a line about a plan is a thing that
    might happen -- the first outranks the second whenever both are true.

    What is deliberately silent: every routine quarter. A run under way whose
    power moved from 2.1 to 2.3 kW produces nothing here, because that is
    arithmetic rather than a decision and reporting it was the largest source of
    the old spam. It is in diagnostics, where a reader can go looking.
    """
    state = previous or ActivityState()
    live = {run.identity: run for run in runs}

    if execution is not None:
        entry = _execution_entry(state, execution, now=now)
        if entry is not None:
            return entry

    # 1. Something we spoke about is gone.
    for record in state.announced:
        if record.identity in live:
            continue
        started = record.identity.start_utc <= now
        return ActivityEntry(
            kind=ECONOMIC_EVENT_ENDED if started else ECONOMIC_EVENT_CANCELLED,
            message=(_ended_message(record) if started else _cancelled_message(record)),
            state=state.without(record.identity),
        )

    # 2. Something we spoke about has materially moved.
    for record in state.announced:
        run = live.get(record.identity)
        if run is None:  # pragma: no cover - handled above
            continue
        if _materially_changed(record.content, run.content, now=now, run=run):
            return ActivityEntry(
                kind=ECONOMIC_EVENT_CHANGED,
                message=_message(ECONOMIC_EVENT_CHANGED, run, now=now),
                state=state.with_announced(run),
            )

    # 3. Something new is imminent.
    for run in runs:
        if state.find(run.identity) is not None:
            continue
        if not _due(run, now=now):
            continue
        kind = ECONOMIC_EVENT_REFUSED if run.content.refused else ECONOMIC_EVENT_PLANNED
        return ActivityEntry(
            kind=kind,
            message=_message(kind, run, now=now),
            state=state.with_announced(run),
        )

    return None


def _execution_entry(
    state: ActivityState, execution: ExecutionView, *, now: datetime
) -> ActivityEntry | None:
    """Return an execution lifecycle line, or ``None`` when nothing changed.

    Four things are worth saying and nothing else is: a run started, a run
    stopped, a standing inhibit began or ended, and the mode changed. Each is a
    transition, and each is said exactly once -- the memory is what makes a
    six-hour run produce a start and a stop rather than twenty-four repetitions of
    the same sentence.
    """
    memory = state.execution

    # A dispatch stopped, or turned into a different campaign. Said before
    # anything else about it, because a stop that goes unrecorded leaves the last
    # line standing as though it were still true.
    if memory.started is not None and (
        not execution.running or execution.intent != memory.started
    ):
        return ActivityEntry(
            kind=(
                ECONOMIC_EVENT_STOPPED
                if execution.executed
                else ECONOMIC_EVENT_WOULD_STOP
            ),
            message=_stopped_message(execution),
            state=state.with_execution(
                ExecutionMemory(
                    started=None,
                    inhibit_reason=memory.inhibit_reason,
                    mode=memory.mode,
                )
            ),
        )

    # A dispatch started. Once per run, keyed on the same identity the plan lines
    # use, so a replan that keeps the run does not re-announce it.
    if execution.running and execution.intent and memory.started != execution.intent:
        return ActivityEntry(
            kind=(
                ECONOMIC_EVENT_STARTED
                if execution.executed
                else ECONOMIC_EVENT_WOULD_START
            ),
            message=_started_message(execution),
            state=state.with_execution(
                ExecutionMemory(
                    started=execution.intent,
                    inhibit_reason=memory.inhibit_reason,
                    mode=memory.mode,
                )
            ),
        )

    # A standing condition changed. The transition speaks; the condition does not.
    if execution.inhibit_reason != memory.inhibit_reason:
        began = execution.inhibit_reason is not None
        return ActivityEntry(
            kind=ECONOMIC_EVENT_INHIBITED if began else ECONOMIC_EVENT_AVAILABLE,
            message=(
                f"Execution inhibited: {execution.inhibit_reason}."
                if began
                else "Execution available again."
            ),
            state=state.with_execution(
                ExecutionMemory(
                    started=memory.started,
                    inhibit_reason=execution.inhibit_reason,
                    mode=memory.mode,
                )
            ),
        )

    return None


def _started_message(execution: ExecutionView) -> str:
    """Return the line for a dispatch beginning.

    Short, and honest about which of the two things happened. A Shadow run says
    what it would have done; only a Live one says a command went out. Getting that
    wrong would make this surface claim the battery moved when it did not, which
    is the one thing it must never do.
    """
    what = execution.intent.replace("_", " ")
    window = f" during {execution.window}" if execution.window else ""
    if not execution.executed:
        return (
            f"Shadow: would {what} to a {execution.target_kwh:.2f} kWh battery "
            f"target{window}. No command sent."
        )
    return (
        f"Dispatch started: {what}, target {execution.target_kwh:.2f} kWh"
        f"{window}, initial power {execution.initial_power_kw:.1f} kW."
    )


def _stopped_message(execution: ExecutionView) -> str:
    """Return the line for a run ending.

    **The two cases report different things, and that is the point.** A Live run
    delivered energy, so it reports what arrived against what was asked for. A
    shadow run delivered nothing -- no command was sent, so the battery did
    whatever it was going to do anyway -- and reporting a "delivered" figure
    beside a target would dress that in the clothes of a measurement. So shadow
    names the target it was tracking and stops there.
    """
    reason = execution.stop_reason or "plan ended"
    if not execution.executed:
        return (
            f"Shadow run finished: {reason}. It was tracking a "
            f"{execution.target_kwh:.2f} kWh target; no command was sent, so "
            f"nothing was executed."
        )
    return (
        f"Dispatch stopped: {reason}. "
        f"{execution.delivered_kwh:.2f} / {execution.target_kwh:.2f} kWh "
        f"(deviation {execution.deviation_kwh:+.2f} kWh)."
    )


def _due(run: PlannedRun, *, now: datetime) -> bool:
    """Return whether this run is close enough to be worth announcing.

    Three cases, and the third is the one that keeps the log honest:

    * more than one planning interval away -- **silent.** The plan is rebuilt
      every quarter and its far end moves constantly; announcing that is what
      produced an entry every fifteen minutes about a run eighteen hours out.
    * within one planning interval of starting -- announce. This is the last
      refresh before it begins.
    * already started and never announced -- announce once, in the in-progress
      form. Covers a reload, and a plan that first appears after its own start.
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


def _materially_changed(
    announced: RunContent, current: RunContent, *, now: datetime, run: PlannedRun
) -> bool:
    """Return whether a run has moved enough to be worth saying again.

    Each deadband is an existing project constant rather than a chosen
    percentage, and each is measured against the **announced** value so it cannot
    flap across a boundary.

    Two things are deliberately *not* compared:

    * the action **label**, because ``discharge`` and ``export`` alternate under a
      varying house load without the decision changing;
    * for a run already under way, its remaining energy and power. Those decay as
      the horizon consumes the run -- that is arithmetic, not a decision, and
      reporting it was the single largest source of the old spam.
    """
    if announced.refused != current.refused:
        return True
    if abs((current.end_utc - announced.end_utc).total_seconds()) > (
        ECONOMIC_DEADBAND_MINUTES * 60
    ):
        return True
    if run.identity.start_utc <= now:
        return False
    if abs(current.energy_kwh - announced.energy_kwh) > ECONOMIC_DEADBAND_ENERGY_KWH:
        return True
    return abs(current.power_kw - announced.power_kw) > ECONOMIC_DEADBAND_POWER_KW


def logbook_payload(entry: ActivityEntry, *, domain: str, entity_id: str) -> dict:
    """Return the event data for one logbook entry.

    Both ``domain`` and ``entity_id`` are set. The domain alone files the line
    under the integration but attaches it to nothing, so it would not appear on
    the entity's own history -- which is where a user looking at the economic
    action will look for it.

    Refuses the execution kind while the barrier stands. A guard rather than an
    assumption: the caller cannot produce one today, and if a later change makes
    it possible the refusal is what stops the release claiming the battery moved.
    """
    if entry.kind in ECONOMIC_EXECUTION_EVENT_KINDS and not CONTROL_EXECUTION_AVAILABLE:
        raise ValueError(
            f"Activity refuses the {entry.kind!r} entry: it describes execution, "
            "and this release executes nothing"
        )
    return {
        "name": ACTIVITY_NAME,
        "message": entry.message,
        "domain": domain,
        "entity_id": entity_id,
    }


def _verb(action: str) -> str:
    """Return the verb phrase for an action."""
    return _VERBS.get(action, action)


def _quantity(content: RunContent) -> str:
    """Return the figures worth stating, each with the boundary it belongs to.

    The live beta.16 line read ``export to the grid 0.95 kW, 0.27 kWh during
    18:30-19:30``. Every figure in it was true and the sentence was still
    misleading: 0.95 kW was the **battery** discharging in the first interval,
    0.27 kWh was what reached the **meter** across the whole run, and the
    remainder covered the house. A reader who multiplies gets nonsense, and a
    reader who does not still cannot tell which quantity was which.

    So: the mean power, because it is the one that multiplies out against the
    battery energy; the battery movement; and, when the two differ, what actually
    reached the grid. A curtailment commands no battery power at all, so quoting
    ``0.00 kW`` beside it would read as a fault rather than as an absence.
    """
    if content.action == ECONOMIC_ACTION_CURTAIL:
        return f"{content.energy_kwh:.2f} kWh of production"
    battery = (
        f"{content.average_power_kw:.2f} kW average "
        f"({content.battery_energy_kwh:.2f} kWh from the battery)"
    )
    if content.action != ECONOMIC_ACTION_EXPORT:
        return battery
    # An export is paid at the meter, so the meter figure has to be present --
    # and named as the meter figure.
    return (
        f"{battery}, of which {content.energy_kwh:.2f} kWh reaches the grid"
        " and the rest covers the house"
    )


def _source_clause(content: RunContent) -> str:
    """Return where a charge run's energy comes from, or nothing.

    Only for a charge, and only when it can be stated exactly. "charged 4.48 kWh"
    read as "bought 4.48 kWh" on the live installation, where 1.55 kWh was even
    site import and the rest was the sun.
    """
    phrase = _SOURCES.get(content.charge_source)
    if phrase is None:
        return ""
    return f", {phrase}"


def _advisory_suffix() -> str:
    """Return the advisory qualifier while nothing can be executed.

    Behind the barrier rather than unconditional. Two messages appended it
    unconditionally, which was harmless while the barrier could not move and would
    have become a false statement the moment it did.
    """
    return "" if CONTROL_EXECUTION_AVAILABLE else f" {_ADVISORY}"


def _ended_message(record: AnnouncedRun) -> str:
    """Return the line for a run whose window has passed."""
    return (
        f"has finished the planned window to {_verb(record.content.action)}"
        f" ({record.content.window}).{_advisory_suffix()}"
    )


def _cancelled_message(record: AnnouncedRun) -> str:
    """Return the line for advice withdrawn before it began."""
    # "before its window opened" rather than "before it started": nothing here
    # ever starts, and a sentence a reader could take as a claim about the
    # battery is the one thing this surface must not produce.
    return (
        f"no longer plans to {_verb(record.content.action)}"
        f" ({record.content.window}); the plan changed before its window opened."
        f"{_advisory_suffix()}"
    )


def _message(kind: str, run: PlannedRun, *, now: datetime) -> str:
    """Return the line for advice that stands."""
    content = run.content
    running = run.identity.start_utc <= now
    if kind == ECONOMIC_EVENT_REFUSED:
        lead = "wants to"
    elif kind == ECONOMIC_EVENT_CHANGED:
        lead = (
            "has changed its plan and now intends to"
            if running
            else ("has changed its plan and now plans to")
        )
    elif running:
        lead = "is part way through a window to"
    else:
        lead = "plans to"

    parts = [f"{lead} {_verb(content.action)}"]
    if content.window:
        parts.append(f"during {content.window}:")
    else:
        parts.append("--")
    parts.append(_quantity(content))
    sentence = " ".join(parts) + _source_clause(content)
    sentence += f", because {_REASONS.get(content.reason, 'unknown')}."

    if kind == ECONOMIC_EVENT_REFUSED:
        instead = _verb(content.capability_action)
        # **Keyed on why, rather than asserting one cause for all of them.** The
        # sentence used to say "no actuator can do that" unconditionally, which was
        # wrong twice over for a charge: an actuator exists, and the reason that
        # actually fires for a charge desire is that the restricted plan works out
        # differently, not that the action is impossible.
        if content.gap_reason == ECONOMIC_GAP_FORECAST_INFEASIBLE:
            sentence += (
                f" Working only with charging and discharging the plan comes out"
                f" differently, so the best available action is to {instead}, at a"
                f" cost of {content.value_eur:.2f} EUR."
            )
        else:
            sentence += (
                f" No actuator in this release can do that, so the best available"
                f" action is to {instead}, at a cost of {content.value_eur:.2f} EUR."
            )
    else:
        sentence += f" Expected value {content.value_eur:.2f} EUR."

    if content.price_eur_kwh is not None:
        sentence += f" Price {content.price_eur_kwh:.4f} EUR/kWh."

    sentence += _advisory_suffix()
    return sentence


def direction_of(action: str) -> str:
    """Return the battery direction an action label implies.

    The label is what a run is *called*; the direction is what the battery does.
    Only the direction belongs in an identity.
    """
    if action in (ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_SAFETY_BUY):
        return ECONOMIC_DIRECTION_CHARGE
    if action in (ECONOMIC_ACTION_DISCHARGE, ECONOMIC_ACTION_EXPORT):
        return ECONOMIC_DIRECTION_DISCHARGE
    return action


__all__ = [
    "ACTIVITY_NAME",
    "ActivityEntry",
    "ActivityState",
    "AnnouncedRun",
    "PlannedRun",
    "RunContent",
    "RunIdentity",
    "direction_of",
    "logbook_payload",
    "next_activity",
]
