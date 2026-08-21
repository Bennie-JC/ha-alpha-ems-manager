"""The Activity surface: what the optimizer wants, written down for a person.

Three properties define this module, and each one is enforced by the shape of the
code rather than by a convention:

**It is strictly observational.** ``next_activity`` receives the economic outcome,
the previous entry and a preformatted clock window -- and nothing else. It cannot
see the plan, the control report, the safety state or the recovery machinery,
because they are not arguments. A later phase that wants to log an execution event
has to change this signature, which is a visible act.

**Nothing reads it back.** No code in this integration subscribes to the event, no
figure is derived from it, and an installation with the recorder removed produces
identical numbers. Activity is a write-only tap.

**It cannot claim the battery did anything.** While
``CONTROL_EXECUTION_AVAILABLE`` is false the two execution kinds are refused
outright, and every advice entry carries the advisory qualifier. An Activity line
reading "charge started" on a release that sends no command would be a lie about
the hardware, which is the single failure mode this surface must not have.

Entries are change-triggered on a *coarse* fingerprint of the plan's inputs, so
ninety-six refreshes against an unchanged answer produce one line, and a plan
whose power drifts by a watt produces none.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    CONTROL_EXECUTION_AVAILABLE,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_EVENT_CHANGED,
    ECONOMIC_EVENT_ENDED,
    ECONOMIC_EVENT_PLANNED,
    ECONOMIC_EVENT_REFUSED,
    ECONOMIC_EXECUTION_EVENT_KINDS,
    ECONOMIC_GAP_NONE,
    ECONOMIC_REASON_CHEAP_WINDOW,
    ECONOMIC_REASON_EXPENSIVE_WINDOW,
    ECONOMIC_REASON_MAKE_HEADROOM,
    ECONOMIC_REASON_NEGATIVE_EXPORT,
    ECONOMIC_REASON_NO_ACTION,
    ECONOMIC_REASON_RESERVE_RECOVERY,
    ECONOMIC_REASON_SAFETY_BUY,
)
from .economic import EconomicOutcome, action_fingerprint

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


@dataclass(frozen=True, slots=True)
class ActivityState:
    """What was last written down.

    Kept so the next refresh can tell "appeared" from "changed" from "went away".
    The fingerprint is the coarse digest, never the plan itself: holding the plan
    would make every quarter-hour a change.
    """

    fingerprint: str | None
    action: str


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    """One line for the logbook, and the state to carry into the next refresh."""

    kind: str
    message: str
    state: ActivityState


def next_activity(
    *,
    previous: ActivityState | None,
    outcome: EconomicOutcome | None,
    window: str | None,
) -> ActivityEntry | None:
    """Return the entry this refresh deserves, or ``None`` for silence.

    Silence is the common case and the default: an unchanged plan, an unavailable
    plan that was already unavailable, and a plan that moved by less than the
    material thresholds all return ``None``.
    """
    fingerprint = action_fingerprint(outcome)
    seen = None if previous is None else previous.fingerprint
    if fingerprint == seen:
        return None

    if fingerprint is None or outcome is None:
        # The advice went away. Say what ended, because "no longer planning
        # anything" is not useful without naming what it was.
        was = ECONOMIC_ACTION_HOLD if previous is None else previous.action
        return ActivityEntry(
            kind=ECONOMIC_EVENT_ENDED,
            message=_ended_message(was),
            state=ActivityState(fingerprint=None, action=ECONOMIC_ACTION_HOLD),
        )

    action = outcome.action
    refused = outcome.capability_gap_reason != ECONOMIC_GAP_NONE
    if refused:
        # A refusal outranks the appeared/changed distinction deliberately. That
        # the optimizer wants something no actuator can perform is the fact worth
        # reading; whether it started wanting it now or five minutes ago is not.
        kind = ECONOMIC_EVENT_REFUSED
    elif seen is None:
        kind = ECONOMIC_EVENT_PLANNED
    else:
        kind = ECONOMIC_EVENT_CHANGED

    return ActivityEntry(
        kind=kind,
        message=_message(kind, outcome, window),
        state=ActivityState(fingerprint=fingerprint, action=action),
    )


def logbook_payload(entry: ActivityEntry, *, domain: str, entity_id: str) -> dict:
    """Return the event data for one logbook entry.

    Both ``domain`` and ``entity_id`` are set. The domain alone files the line
    under the integration but attaches it to nothing, so it would not appear on
    the entity's own history -- which is where a user looking at the economic
    action will look for it.

    Refuses the execution kinds while the barrier stands. A guard rather than an
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


def _ended_message(was: str) -> str:
    """Return the line for advice that has gone away."""
    verb = _VERBS.get(was)
    if was == ECONOMIC_ACTION_HOLD or verb is None:
        return "has no battery action planned."
    return f"no longer plans to {verb}."


def _message(kind: str, outcome: EconomicOutcome, window: str | None) -> str:
    """Return the line for advice that stands."""
    run = outcome.desired.published_run
    verb = _VERBS.get(outcome.action, outcome.action)
    lead = "wants to" if kind == ECONOMIC_EVENT_REFUSED else "plans to"
    parts = [f"{lead} {verb}"]

    if run is not None:
        quantity = _quantity(outcome.action, run.first_power_kw, run.energy_kwh)
        if quantity:
            parts.append(quantity)
        if window:
            parts.append(f"during {window}")

    sentence = " ".join(parts) + f", because {_REASONS.get(outcome.reason, 'unknown')}."

    if kind == ECONOMIC_EVENT_REFUSED:
        instead = _VERBS.get(outcome.capability_action, outcome.capability_action)
        forgone = outcome.economic_value_forgone_eur
        sentence += (
            f" No actuator in this release can do that, so the best available"
            f" action is to {instead}, at a cost of {forgone:.2f} EUR."
        )
    elif run is not None:
        sentence += f" Expected value {run.expected_value_eur:.2f} EUR."

    if not CONTROL_EXECUTION_AVAILABLE:
        sentence += f" {_ADVISORY}"
    return sentence


def _quantity(action: str, power_kw: float, energy_kwh: float) -> str:
    """Return the figures worth stating, and only those.

    A curtailment commands no battery power, so quoting ``0.00 kW`` beside it
    would read as a fault rather than as an absence.
    """
    if action == ECONOMIC_ACTION_CURTAIL:
        return f"{energy_kwh:.2f} kWh"
    return f"{power_kw:.2f} kW, {energy_kwh:.2f} kWh"
