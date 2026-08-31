"""Phase 4: turning a battery decision into a vendor-neutral control intent.

This module is a **projection**, not a decision. Every field of a
:class:`ControlIntent` is either copied from the plan, read from configuration,
or derived by one documented division. Phase 3 owns what the battery should do;
this owns saying it in a form something else could carry out.

Nothing here knows what an inverter is. There is no register, no dispatch mode,
no entity id and no vendor name in this file, and that is a deliberate boundary:
a reader should not be able to tell which battery is downstream. The one module
that does know lives next door.

The interesting question this module answers is why converting an energy into a
power is legitimate at all, given that Phase 3 explicitly warns against reading
``average_power_kw`` as a setpoint. The short answer is that the warning is
about physics and this is about a command:

* The device cannot be told an energy. It accepts a power and a duration, so
  *some* mapping from energy to power is unavoidable.
* The window a command actually acts over is the planning cadence, not the
  duration. Each refresh supersedes the last, so the duration is a dead-man
  margin that in normal operation never expires.
* Over a fixed window there is exactly **one** constant power that delivers a
  given energy. One admissible value means no choice, and no choice means no
  decision -- which is what keeps this a projection.

What Phase 3's warning does forbid is commanding full power and letting the
device's own cutoff stop it. That would deliver the same energy by accident
rather than by construction, and it is refused explicitly: see
``energy_limit_bound``, which exists so the adapter can tell the difference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .battery import INTERVAL_HOURS
from .const import (
    ACTION_HOLD,
    BATTERY_KW_PRECISION,
    BATTERY_KWH_PRECISION,
    BATTERY_SOC_PRECISION,
    CONSTRAINT_MAX_SOC,
    CONSTRAINT_MIN_SOC,
)
from .plan import BatteryPlan

#: Constraint names that mean the *energy* limit bound rather than a power one.
#:
#: Read straight off the decision. The adapter needs this to honour the rule that
#: the device's cutoff is a backstop and never the mechanism that meets the
#: energy limit: if a discharge stops at the cutoff while this is false, the
#: clamp and the device disagree about the floor, and that is a fault to report
#: rather than absorb.
_ENERGY_LIMIT_CONSTRAINTS = frozenset({CONSTRAINT_MIN_SOC, CONSTRAINT_MAX_SOC})


@dataclass(frozen=True, slots=True)
class ControlIntent:
    """One interval's request, in terms no particular device owns.

    ``energy_ac_kwh`` is the primitive and is copied byte-for-byte from the
    decision. It has already been through the single clamp, so recomputing it
    from anything else would reintroduce the limit it is supposed to trust.
    """

    action: str
    #: AC energy the clamp permitted, ``>= 0``. Copied, never recomputed.
    energy_ac_kwh: float
    #: The interval **average** implied by that energy. Phase 3's name and
    #: Phase 3's meaning are both kept, so the moment it becomes a device
    #: setpoint is a visible rename in one other file rather than a silent
    #: reinterpretation here.
    average_power_kw: float
    #: How long the energy applies to, in hours. Carried explicitly so the one
    #: definition of an interval stays in one place and nothing downstream has
    #: to divide.
    interval_hours: float
    #: The user's configured floor. The device-side backstop for a **discharge**
    #: only, and structurally unusable by a charge -- see ``ceiling_soc_percent``.
    floor_soc_percent: float
    #: Whether the energy limit bound, rather than a power limit or nothing.
    energy_limit_bound: bool
    #: The dead-man margin, in minutes. Not a delivery window.
    horizon_minutes: int
    #: Which interval of which day this describes. Chronological, so a
    #: daylight-saving day of 92 or 100 intervals needs no special case.
    target_day: date
    start_index: int
    #: When the plan behind this was built. Passed in, so this module stays pure.
    built_at: datetime
    #: Phase 3's own reason, carried through unchanged.
    reason: str
    policy: str
    policy_version: int
    #: The applicable upper state of charge for a **charge**, in percent.
    #:
    #: ``None`` when no ceiling could be established, and that is not a licence to
    #: substitute something: a charge with no valid ceiling is refused. Measured on
    #: the real installation, a charge cutoff is an *upper* bound -- a run with
    #: cutoff 90 % while the pack sat below 90 % charged normally -- so reusing the
    #: discharge floor here would have written "stop at 21 %" to a pack already at
    #: 61 %, and the first Live charge would simply not have run.
    ceiling_soc_percent: float | None = None
    #: A commanded rest inside a live run: this row has nothing to ask for at this
    #: instant, the dispatch stays armed, and the commanded power is exactly zero.
    #:
    #: **beta.36, and it is deliberately not ``ACTION_HOLD``.** A Phase-3 hold means
    #: *there is no run*; this means *there is a run and it is resting*. Spelling it
    #: as ``ACTION_HOLD`` would also change which cutoff the device layer writes --
    #: ``_cutoff_for`` sends a non-charge action to the **discharge floor** helper,
    #: so a resting charge would have written "stop at 21 %" to a pack at 61 %,
    #: which is the beta.19 inversion resurrected by a state whose whole purpose is
    #: safety. The direction action is therefore preserved and this flag carries the
    #: rest.
    holds_at_zero: bool = False

    @property
    def moves_battery(self) -> bool:
        """Return whether this intent asks the battery to do anything.

        **A hold answers ``False`` and that is the honest answer**: zero energy is
        zero energy. The distinction a caller needs -- "resting inside a run" versus
        "no run at all" -- is :attr:`holds_at_zero`, not this.
        """
        return self.action != ACTION_HOLD and self.energy_ac_kwh > 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics form: flat, bounded, no nested lists."""
        return {
            "action": self.action,
            "energy_ac_kwh": round(self.energy_ac_kwh, BATTERY_KWH_PRECISION),
            "average_power_kw": round(self.average_power_kw, BATTERY_KW_PRECISION),
            "interval_hours": self.interval_hours,
            "floor_soc_percent": round(self.floor_soc_percent, BATTERY_SOC_PRECISION),
            "ceiling_soc_percent": (
                None
                if self.ceiling_soc_percent is None
                else round(self.ceiling_soc_percent, BATTERY_SOC_PRECISION)
            ),
            "energy_limit_bound": self.energy_limit_bound,
            "holds_at_zero": self.holds_at_zero,
            "horizon_minutes": self.horizon_minutes,
            "target_day": self.target_day.isoformat(),
            "start_index": self.start_index,
            "built_at": self.built_at.isoformat(),
            "reason": self.reason,
            "policy": self.policy,
            "policy_version": self.policy_version,
            "moves_battery": self.moves_battery,
            "average_power_basis": (
                "an interval average over interval_hours, not an instantaneous "
                "setpoint; the device power derived from it is quantised "
                "downwards so the commanded energy never exceeds energy_ac_kwh"
            ),
            "horizon_basis": (
                "a dead-man margin, not a delivery window: each refresh "
                "supersedes the last, so in normal operation it never expires"
            ),
        }


def translate(
    plan: BatteryPlan | None,
    *,
    now: datetime,
    horizon_minutes: int,
) -> ControlIntent | None:
    """Project one plan onto one intent, or return ``None``.

    Pure, total, and it never raises -- the same contract ``build_plan`` holds
    itself to, and for the same reason: this runs inside a refresh that must not
    be able to fail because of it.

    ``None`` means there is no intent to have, which is different from an intent
    to do nothing. A plan that reached no decision, or that could not be built
    at all, yields ``None``; a plan that decided to hold yields an intent whose
    action is a hold and whose energy is exactly zero.
    """
    if plan is None:
        return None

    decision = plan.decision
    if not decision.decided:
        return None
    if plan.target_day is None or plan.start_index is None:
        # A decision without an interval identity cannot be checked for
        # staleness, and acting on a plan of unknown vintage is exactly what the
        # staleness conditions exist to prevent.
        return None

    energy = decision.allowed_energy_ac_kwh
    if not isinstance(energy, (int, float)) or not math.isfinite(energy):
        return None
    energy = max(0.0, float(energy))

    return ControlIntent(
        action=decision.action,
        energy_ac_kwh=energy,
        average_power_kw=energy / INTERVAL_HOURS,
        interval_hours=INTERVAL_HOURS,
        floor_soc_percent=plan.reserve.configured_min_soc_percent,
        # The pack's own maximum. Phase 4 only ever discharges, so this is unused
        # on this path -- it is carried so the field has one definition rather
        # than two, and so a charge built elsewhere cannot forget it.
        ceiling_soc_percent=(
            None if plan.state is None else plan.state.limits.max_soc_percent
        ),
        energy_limit_bound=bool(
            _ENERGY_LIMIT_CONSTRAINTS.intersection(decision.constraints)
        ),
        horizon_minutes=int(horizon_minutes),
        target_day=plan.target_day,
        start_index=int(plan.start_index),
        built_at=now,
        reason=decision.reason,
        policy=decision.policy,
        policy_version=decision.policy_version,
    )
