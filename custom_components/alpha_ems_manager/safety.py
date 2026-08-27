"""Phase 4: safety eligibility, and separately, permission to execute.

Two questions that look similar and are not:

* :func:`evaluate` asks **would this be safe**. It knows nothing about the
  control mode, so it returns the same answer in shadow as in active. That is
  the whole point: shadow mode is only useful if its verdict is the real one.
* :func:`authorize` asks **may this be sent**. It knows nothing about hazards
  beyond whether the gate passed.

An earlier draft merged them, with ``mode_not_active`` as the first gate
condition. Shadow mode then stopped at that condition and reported it, which
told the user nothing about whether the command would have been safe -- the only
thing shadow exists to answer. Splitting them cannot weaken anything, because
:func:`authorize` can only ever subtract: it requires ``verdict.safe`` before
considering anything else, and a test asserts that over the full cross-product.

Both functions are pure. Every fact they need arrives on a
:class:`ControlContext` as a plain value, so the entire gate is exercisable
against synthetic state with no Home Assistant instance in sight.

The gate **never scales a command**, and it still does not. If a request reaches
it and cannot be sent safely it is refused whole, with one precise reason. A gate
that reduced a request to make it fit would have made a decision of its own, and
deciding is not its job.

What changed in beta.15 is *upstream* of the gate, not inside it. A non-exporting
discharge is now clamped to the largest safely absorbable power **before** the
command is built, by :func:`safe_discharge_power_kw` here and
:func:`alphaess_device.limit_command` there. The gate then sees an already-safe
command and passes it. If no representable command survives the clamp, the
original request reaches the gate untouched and is refused whole exactly as
before -- so ``would_export`` keeps both its meaning and its wording, and the
failure path is unchanged.

The distinction is worth keeping sharp: this module supplies a *bound* and
answers yes or no. Choosing a smaller command against that bound is the command
layer's job, and it can only ever subtract.

Export protection is measured at the meter, not reconstructed. The absorbing
capacity a discharge is checked against is ``grid_import - grid_export +
battery_discharge`` (:func:`absorbing_capacity_kw`), because a forced discharge
first displaces import and only spills onto the grid once import reaches zero.
An earlier release checked the command against the house load alone, which was
under-protective whenever PV was already covering the house: on this
installation, live samples with 3.1 kW of PV against 2.0 kW of load passed a
gate that should have refused, because the site was already exporting a kilowatt
before the battery was asked to add to it. Subtracting PV from house load would
also have caught those, but the meter needs no PV term at all -- so no answer to
the mixed DC/AC boundary question, no exposure to the vendor's PV filter lag,
and no daylight rule for a sensor that legitimately reads zero all night.

One condition that is deliberately *absent*: the aggregate energy-balance
residual. On an installation whose house-load figure is derived from one grid
meter while the balance check uses another, that residual reduces algebraically
to the difference between the two meters, plus a filter lag. The battery power
term cancels identically and the state of charge never enters it at all -- so it
carries no evidence about the readings this gate actually depends on, whatever
its magnitude. It stays in diagnostics, where it is genuinely useful, and out of
the write path, where it would be a threshold on the wrong measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .alphaess_device import DISPATCH_ENABLE
from .const import (
    ACTION_DISCHARGE,
    CONTROL_COOLDOWN_SECONDS,
    CONTROL_CUTOFF_MAX_PERCENT,
    CONTROL_CUTOFF_MIN_PERCENT,
    CONTROL_EXECUTABLE_ACTIONS,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_MAX_POWER_KW,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    EMERGENCY_STOP_MAX_ATTEMPTS,
    EXECUTION_INTENT_NET_EXPORT,
    EXPORT_REFUSE_ABOVE_DEVICE_MAXIMUM,
    EXPORT_REFUSE_BELOW_DEVICE_MINIMUM,
    EXPORT_REFUSE_CONFLICTING_FEATURE,
    EXPORT_REFUSE_DISPATCH_FOREIGN,
    EXPORT_REFUSE_INCOHERENT,
    EXPORT_REFUSE_INVERTER_LIMIT,
    EXPORT_REFUSE_MIN_SOC,
    EXPORT_REFUSE_MISSING_ENTITY,
    EXPORT_REFUSE_NO_BATTERY_ALLOWANCE,
    EXPORT_REFUSE_NO_EXPORT_TARGET,
    EXPORT_REFUSE_NO_FAILSAFE,
    EXPORT_REFUSE_NO_QUARTER,
    EXPORT_REFUSE_NOT_EXPORT_INTENT,
    EXPORT_REFUSE_NOT_OWNED,
    EXPORT_REFUSE_QUARTER_NOT_OPEN,
    EXPORT_REFUSE_RECORD_MISMATCH,
    EXPORT_REFUSE_RESERVE_FLOOR,
    EXPORT_REFUSE_SIGN,
    EXPORT_REFUSE_SITE_EXPORT_LIMIT,
    EXPORT_REFUSE_SOC_UNUSABLE,
    EXPORT_REFUSE_TICK_HORIZON,
    INHIBIT_AT_OR_BELOW_FLOOR,
    INHIBIT_BATTERY_NOT_CONFIGURED,
    INHIBIT_BATTERY_POWER_STALE,
    INHIBIT_BATTERY_POWER_UNUSABLE,
    INHIBIT_CONTROL_ENTITY_UNAVAILABLE,
    INHIBIT_CUTOFF_OUT_OF_RANGE,
    INHIBIT_DISPATCH_ACTIVE,
    INHIBIT_DURATION_OUT_OF_RANGE,
    INHIBIT_EXCESS_EXPORT_ACTIVE,
    INHIBIT_GRID_STALE,
    INHIBIT_GRID_UNUSABLE,
    INHIBIT_HOUSE_LOAD_STALE,
    INHIBIT_HOUSE_LOAD_UNUSABLE,
    INHIBIT_MISSING_CONTROL_ENTITY,
    INHIBIT_NO_FAILSAFE_AUTOMATION,
    INHIBIT_NO_PLAN,
    INHIBIT_PEAK_SHAVING_ACTIVE,
    INHIBIT_POWER_ABOVE_DEVICE_MAXIMUM,
    INHIBIT_POWER_BELOW_DEVICE_MINIMUM,
    INHIBIT_SOC_STALE,
    INHIBIT_SOC_UNUSABLE,
    INHIBIT_STALE_PLAN_AGE,
    INHIBIT_STALE_PLAN_DAY,
    INHIBIT_STALE_PLAN_INTERVAL,
    INHIBIT_WOULD_EXPORT,
    MAX_CONTROL_HORIZON_MINUTES,
    MIN_CONTROL_HORIZON_MINUTES,
    OWNERSHIP_OWNED,
    REFUSE_COOLDOWN,
    REFUSE_EMERGENCY_ATTEMPTS_SPENT,
    REFUSE_EMERGENCY_NOT_AUTHORIZED,
    REFUSE_EMERGENCY_NOT_THE_STOP,
    REFUSE_EXECUTION_NOT_ENABLED,
    REFUSE_EXECUTION_UNAVAILABLE,
    REFUSE_LIVE_ACTION_NOT_PERMITTED,
    REFUSE_MARKER_STILL_DISPATCHING,
    REFUSE_MODE_NOT_ACTIVE,
    REFUSE_NO_COMMANDS,
    REFUSE_RESET_ACTION_UNKNOWN,
    REFUSE_RESET_NOT_OWNED,
    REFUSE_RESET_WITHOUT_REASON,
    REFUSE_UNSAFE,
)
from .control import ControlIntent


@dataclass(frozen=True, slots=True)
class ControlContext:
    """Every live fact the gate needs, as plain values.

    Assembled once per refresh by the coordinator. Nothing here is read lazily,
    so the gate cannot see a different world halfway through evaluating itself.
    """

    #: The selected control mode.
    mode: str
    #: Whether the user has enabled real execution. Distinct from whether this
    #: release *can* execute, which is a build-time constant.
    execution_enabled: bool

    # -- capability -----------------------------------------------------------
    #: Required entities absent from the state machine, and present-but-unusable.
    #: Names are carried so diagnostics can say which, rather than only that.
    missing_entities: tuple[str, ...] = ()
    unavailable_entities: tuple[str, ...] = ()
    #: Whether the control surface's own restart and communication-loss reset
    #: was found and is switched on.
    failsafe_available: bool = False
    #: Whether another feature of the control surface is driving the battery.
    excess_export_active: bool = False
    peak_shaving_active: bool = False
    #: Whether any dispatch is running. Says nothing about whose it is -- see
    #: ``dispatch_owned``, which is the only thing that may answer that.
    dispatch_active: bool = False
    #: Whether the running dispatch has been *established* as Alpha EMS's own.
    #:
    #: Supplied by the controller, which holds the two pieces of evidence -- the
    #: owner marker and the persisted causal record. Deliberately a verdict rather
    #: than the evidence: this module must not be able to derive ownership, because
    #: the only derivation available from the vendor surface is parameter matching
    #: and that is worse than nothing here.
    #:
    #: ``False`` is the safe default, so a caller that forgets to supply it gets
    #: beta.18's behaviour: any dispatch inhibits.
    dispatch_owned: bool = False

    # -- configuration --------------------------------------------------------
    battery_configured: bool = False

    # -- plan -----------------------------------------------------------------
    #: An inhibit reason already resolved by the caller when no usable intent
    #: could be produced, or ``None``. The coordinator owns the plan and knows
    #: *why* there is no intent; relaying that verbatim keeps this module free of
    #: Phase-3 internals while still reporting a precise cause.
    plan_problem: str | None = None
    #: The interval and civil day the plan must describe to be current.
    current_start_index: int | None = None
    today: date | None = None
    now: datetime | None = None

    # -- readings this phase depends on ---------------------------------------
    soc_percent: float | None = None
    soc_age_seconds: float | None = None
    battery_power_w: float | None = None
    battery_power_age_seconds: float | None = None
    house_load_w: float | None = None
    house_load_age_seconds: float | None = None
    #: The grid meter, canonical and unsigned: at most one of these is non-zero.
    #: This is the boundary that *defines* export, so it is what the export check
    #: is measured against -- see :func:`absorbing_capacity_kw`.
    grid_import_w: float | None = None
    grid_export_w: float | None = None
    grid_age_seconds: float | None = None
    #: How old a reading may be before it is no longer evidence about now.
    max_source_age_seconds: float = 300.0

    # -- the quantised command, as numbers -----------------------------------
    #: Supplied by the adapter. Kept as bare values so this module needs no
    #: vendor type and cannot accidentally reach a vendor entity id.
    device_power_kw: float = 0.0
    device_cutoff_percent: int = 0
    device_duration_minutes: int = 0
    #: How far below the measured absorbing capacity a discharge must stay, in
    #: percent. Applied to the capacity, never to the command -- the command is
    #: then clamped to what remains. See :func:`safe_discharge_power_kw`.
    export_margin_percent: float = 0.0

    # -- rate limiting --------------------------------------------------------
    seconds_since_last_write: float | None = None


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """Whether a command would be safe, and if not, the one reason it is not."""

    safe: bool
    inhibit_reason: str | None
    #: Every condition evaluated, in order, as ``(name, passed)``. Kept whole for
    #: tests; diagnostics reports counts and the failure rather than the list,
    #: because a twenty-five entry list has no business in a payload capped at
    #: sixteen.
    checks: tuple[tuple[str, bool], ...] = field(default_factory=tuple)

    @property
    def checks_evaluated(self) -> int:
        """Return how many conditions were reached."""
        return len(self.checks)

    @property
    def checks_passed(self) -> int:
        """Return how many conditions passed."""
        return sum(1 for _, ok in self.checks if ok)

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        return {
            "safe": self.safe,
            "inhibit_reason": self.inhibit_reason,
            "checks_evaluated": self.checks_evaluated,
            "checks_passed": self.checks_passed,
            "basis": (
                "evaluated identically in shadow and active; no condition here "
                "depends on the control mode"
            ),
        }


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """Whether a command may actually be sent, and if not, why not."""

    authorized: bool
    refusal: str | None
    #: The gate's reason, when the refusal was that the gate refused.
    unsafe_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        return {
            "authorized": self.authorized,
            "refusal": self.refusal,
            "unsafe_reason": self.unsafe_reason,
            "execution_available": CONTROL_EXECUTION_AVAILABLE,
            "basis": (
                "can only subtract: authorization requires a safe verdict plus "
                "every further condition, so it never permits what the gate "
                "refused"
            ),
        }


def _stale(age: float | None, limit: float) -> bool:
    """Return whether a reading is too old to be evidence about now.

    ``None`` is not stale -- it means no age could be established, which the
    accompanying value check handles. Treating it as stale here would report the
    wrong reason for a source that simply does not exist.
    """
    return age is not None and age > limit


def absorbing_capacity_kw(context: ControlContext) -> float:
    """Return how much discharge power the site can absorb without exporting.

    Measured at the meter rather than reconstructed from house load and PV,
    because the meter is the instrument that *defines* export.

    A forced discharge first displaces grid import and only spills onto the grid
    once import reaches zero. Writing ``D_old`` for the discharge already
    flowing, and taking the importing case::

        grid_import  = house_load - pv - D_old
        post_export  = pv + D_new - house_load = D_new - (grid_import + D_old)

    so a new discharge is safe exactly while

        D_new <= grid_import - grid_export + D_old

    which is what this returns, floored at zero. Because import and export are
    canonical and unsigned, at most one of them is non-zero, and the subtraction
    handles the already-exporting case: a site exporting 500 W with the battery
    idle has a capacity of zero and any discharge is refused.

    Three things this deliberately does not do:

    * It does not read PV. So it needs no answer to the mixed DC/AC boundary
      question, it cannot be skewed by the vendor's low-pass filter on the PV
      signal, and it needs no daylight or staleness rule for a sensor that
      legitimately reports a constant zero all night.
    * It does not read house load. So it bounds loads no house-load sensor sees
      -- on this installation the energy-balance work exposed roughly 1.4 kW
      drawing through the meter that the inverter's own register does not.
    * It does not scale anything. It returns a bound; refusing on it is the
      caller's decision, and the command is refused whole or not at all.
    """
    net_import_w = (context.grid_import_w or 0.0) - (context.grid_export_w or 0.0)
    # ``battery_power_w`` is positive for charging, so a discharge is its
    # negation. Charging contributes nothing: it is already absorbing, and
    # counting it would credit the battery for load it is itself creating.
    discharge_now_w = max(0.0, -(context.battery_power_w or 0.0))
    return max(0.0, net_import_w + discharge_now_w) / 1000.0


def safe_discharge_power_kw(context: ControlContext) -> float:
    """Return the largest discharge power that cannot push energy onto the grid.

    The measured absorbing capacity with the configured margin taken off it, and
    the **single** definition of that bound: :func:`evaluate` compares against
    this, and :func:`alphaess_device.limit_command` clamps against it, so the
    number a command is reduced to and the number the gate checks cannot drift
    apart. Two expressions of one safety bound is one too many.

    Ordering is load-bearing and is fixed here: the capacity is measured first,
    the margin reduces the **capacity**, and only afterwards may a caller
    quantise downwards. Applying the margin to the command instead would leave
    the command at the capacity itself, and rounding before the margin would put
    a command back above the bound the margin exists to create.

    Independent of ``context.device_power_kw`` by construction -- it reads only
    the meter, the battery power and the margin. That is what lets the caller
    build one context from the *requested* command, take this bound from it, and
    then replace the one field with the limited power.

    Returns kW, never negative. Zero means no discharge is safe at all, which a
    site that is already exporting produces immediately.
    """
    capacity_kw = absorbing_capacity_kw(context)
    headroom = 1.0 - (context.export_margin_percent / 100.0)
    return max(0.0, capacity_kw * max(0.0, headroom))


def evaluate(intent: ControlIntent | None, context: ControlContext) -> SafetyVerdict:
    """Return whether the command described by ``intent`` would be safe.

    Mode-independent by construction: nothing below reads ``context.mode``.

    Conditions are ordered cheapest and most fundamental first, so the single
    reported reason is the most useful one. A missing helper is a better answer
    than a stale reading taken through it.
    """
    checks: list[tuple[str, bool]] = []

    def check(name: str, passed: bool) -> bool:
        checks.append((name, passed))
        return passed

    # -- capability: can anything be commanded at all? ----------------------
    if not check(INHIBIT_MISSING_CONTROL_ENTITY, not context.missing_entities):
        return SafetyVerdict(False, INHIBIT_MISSING_CONTROL_ENTITY, tuple(checks))
    if not check(INHIBIT_CONTROL_ENTITY_UNAVAILABLE, not context.unavailable_entities):
        return SafetyVerdict(False, INHIBIT_CONTROL_ENTITY_UNAVAILABLE, tuple(checks))
    # Without the control surface's own reset, an interrupted sequence could
    # outlive this integration: a restart or a lost connection would leave the
    # inverter in a dispatch nothing is left to clear. Alpha EMS deliberately
    # does not carry its own copy of that mechanism, so it insists on this one.
    if not check(INHIBIT_NO_FAILSAFE_AUTOMATION, context.failsafe_available):
        return SafetyVerdict(False, INHIBIT_NO_FAILSAFE_AUTOMATION, tuple(checks))

    # -- another feature is already in charge --------------------------------
    # Reported separately from a generic active dispatch so the user is told
    # which of their own features stood in the way. Alpha EMS stands down rather
    # than switching it off: the control surface's own arming sequence would
    # happily disable these, and silently overriding a setting somebody chose is
    # not a safety measure.
    if not check(INHIBIT_EXCESS_EXPORT_ACTIVE, not context.excess_export_active):
        return SafetyVerdict(False, INHIBIT_EXCESS_EXPORT_ACTIVE, tuple(checks))
    if not check(INHIBIT_PEAK_SHAVING_ACTIVE, not context.peak_shaving_active):
        return SafetyVerdict(False, INHIBIT_PEAK_SHAVING_ACTIVE, tuple(checks))
    # Any dispatch that is not *established* as ours.
    #
    # Until beta.19 this was any dispatch at all, which was correct while nothing
    # could record a writer -- and which also meant Alpha EMS inhibited itself the
    # moment it armed anything, so no command spanning more than one interval was
    # expressible. That was the second of the two blockers this gate names.
    #
    # The relaxation is strictly additive: ``dispatch_owned`` defaults to false, so
    # every path that does not supply it keeps the old behaviour. What it is *not*
    # is a parameter test -- this module cannot derive ownership and must not learn
    # how. It is handed a verdict that rests on a marker outside the vendor surface
    # plus a persisted causal record, and a foreign dispatch still stops everything
    # exactly as before.
    if not check(
        INHIBIT_DISPATCH_ACTIVE,
        not context.dispatch_active or context.dispatch_owned,
    ):
        return SafetyVerdict(False, INHIBIT_DISPATCH_ACTIVE, tuple(checks))

    # -- is there a decision to carry out? ----------------------------------
    if not check(INHIBIT_BATTERY_NOT_CONFIGURED, context.battery_configured):
        return SafetyVerdict(False, INHIBIT_BATTERY_NOT_CONFIGURED, tuple(checks))
    if context.plan_problem is not None:
        check(context.plan_problem, False)
        return SafetyVerdict(False, context.plan_problem, tuple(checks))
    if not check(INHIBIT_NO_PLAN, intent is not None):
        return SafetyVerdict(False, INHIBIT_NO_PLAN, tuple(checks))

    # -- is the decision about *now*? ---------------------------------------
    if not check(
        INHIBIT_STALE_PLAN_DAY,
        context.today is not None and intent.target_day == context.today,
    ):
        return SafetyVerdict(False, INHIBIT_STALE_PLAN_DAY, tuple(checks))
    if not check(
        INHIBIT_STALE_PLAN_INTERVAL,
        context.current_start_index is not None
        and intent.start_index == context.current_start_index,
    ):
        return SafetyVerdict(False, INHIBIT_STALE_PLAN_INTERVAL, tuple(checks))
    age_ok = False
    if context.now is not None:
        elapsed = (context.now - intent.built_at).total_seconds()
        age_ok = 0.0 <= elapsed <= intent.interval_hours * 3600.0
    if not check(INHIBIT_STALE_PLAN_AGE, age_ok):
        return SafetyVerdict(False, INHIBIT_STALE_PLAN_AGE, tuple(checks))

    # -- are the readings this phase depends on trustworthy? ----------------
    # The state of charge is the reading the floor guarantee rests on, and until
    # this phase nothing checked how old it was: a sensor that died an hour ago
    # still seeded the model as though it were current. Acceptable while nothing
    # acted on it; not acceptable now.
    if not check(INHIBIT_SOC_UNUSABLE, context.soc_percent is not None):
        return SafetyVerdict(False, INHIBIT_SOC_UNUSABLE, tuple(checks))
    if not check(
        INHIBIT_SOC_STALE,
        not _stale(context.soc_age_seconds, context.max_source_age_seconds),
    ):
        return SafetyVerdict(False, INHIBIT_SOC_STALE, tuple(checks))
    if not check(INHIBIT_BATTERY_POWER_UNUSABLE, context.battery_power_w is not None):
        return SafetyVerdict(False, INHIBIT_BATTERY_POWER_UNUSABLE, tuple(checks))
    if not check(
        INHIBIT_BATTERY_POWER_STALE,
        not _stale(context.battery_power_age_seconds, context.max_source_age_seconds),
    ):
        return SafetyVerdict(False, INHIBIT_BATTERY_POWER_STALE, tuple(checks))

    # -- a hold needs nothing further ---------------------------------------
    # Everything below constrains a command that moves the battery. Applying it
    # to a hold would inhibit doing nothing because the house-load sensor was
    # briefly quiet, which is noise dressed as caution -- and it would make the
    # "gate passed, nothing to send" state unreachable.
    if not intent.moves_battery:
        return SafetyVerdict(True, None, tuple(checks))

    if not check(INHIBIT_HOUSE_LOAD_UNUSABLE, context.house_load_w is not None):
        return SafetyVerdict(False, INHIBIT_HOUSE_LOAD_UNUSABLE, tuple(checks))
    if not check(
        INHIBIT_HOUSE_LOAD_STALE,
        not _stale(context.house_load_age_seconds, context.max_source_age_seconds),
    ):
        return SafetyVerdict(False, INHIBIT_HOUSE_LOAD_STALE, tuple(checks))

    # -- does the battery have anything to give? ----------------------------
    floor_ok = True
    if intent.action == ACTION_DISCHARGE:
        soc = context.soc_percent
        floor_ok = soc is not None and soc > intent.floor_soc_percent
    if not check(INHIBIT_AT_OR_BELOW_FLOOR, floor_ok):
        return SafetyVerdict(False, INHIBIT_AT_OR_BELOW_FLOOR, tuple(checks))

    # -- does the command fit the device? -----------------------------------
    if not check(
        INHIBIT_POWER_BELOW_DEVICE_MINIMUM,
        context.device_power_kw >= CONTROL_MIN_POWER_KW,
    ):
        return SafetyVerdict(False, INHIBIT_POWER_BELOW_DEVICE_MINIMUM, tuple(checks))
    if not check(
        INHIBIT_POWER_ABOVE_DEVICE_MAXIMUM,
        context.device_power_kw <= CONTROL_MAX_POWER_KW,
    ):
        return SafetyVerdict(False, INHIBIT_POWER_ABOVE_DEVICE_MAXIMUM, tuple(checks))
    if not check(
        INHIBIT_CUTOFF_OUT_OF_RANGE,
        CONTROL_CUTOFF_MIN_PERCENT
        <= context.device_cutoff_percent
        <= CONTROL_CUTOFF_MAX_PERCENT,
    ):
        return SafetyVerdict(False, INHIBIT_CUTOFF_OUT_OF_RANGE, tuple(checks))
    # Bounded by the *planning* range rather than the device range: a command
    # shorter than one planning interval would lapse before the next refresh
    # could renew it, leaving the battery unmanaged for most of every interval.
    if not check(
        INHIBIT_DURATION_OUT_OF_RANGE,
        MIN_CONTROL_HORIZON_MINUTES
        <= context.device_duration_minutes
        <= MAX_CONTROL_HORIZON_MINUTES,
    ):
        return SafetyVerdict(False, INHIBIT_DURATION_OUT_OF_RANGE, tuple(checks))

    # -- is the meter readable? ---------------------------------------------
    # Only a discharge can export, so only a discharge needs the meter. Gating a
    # charge on it would refuse to store energy because the grid sensor was
    # briefly quiet, which buys nothing.
    grid_usable = True
    grid_fresh = True
    if intent.action == ACTION_DISCHARGE:
        grid_usable = (
            context.grid_import_w is not None and context.grid_export_w is not None
        )
        grid_fresh = not _stale(
            context.grid_age_seconds, context.max_source_age_seconds
        )
    if not check(INHIBIT_GRID_UNUSABLE, grid_usable):
        return SafetyVerdict(False, INHIBIT_GRID_UNUSABLE, tuple(checks))
    if not check(INHIBIT_GRID_STALE, grid_fresh):
        return SafetyVerdict(False, INHIBIT_GRID_STALE, tuple(checks))

    # -- would this push energy onto the grid? ------------------------------
    # A forced discharge sets the *battery* rate, so whatever the house cannot
    # absorb leaves through the meter -- and the dispatch path does not honour
    # the configured feed-in limit, so nothing downstream will catch it.
    #
    # Still a refusal and still whole. Since beta.15 the command layer clamps a
    # discharge to :func:`safe_discharge_power_kw` before it gets here, so in the
    # ordinary case this condition passes on an already-reduced command. It fires
    # when the clamp could not produce anything representable -- no capacity at
    # all, or a safe power below the device minimum -- and then the request that
    # arrives is the original one, refused exactly as it was before.
    export_ok = True
    if intent.action == ACTION_DISCHARGE:
        export_ok = context.device_power_kw <= safe_discharge_power_kw(context)
    if not check(INHIBIT_WOULD_EXPORT, export_ok):
        return SafetyVerdict(False, INHIBIT_WOULD_EXPORT, tuple(checks))

    return SafetyVerdict(True, None, tuple(checks))


@dataclass(frozen=True, slots=True)
class ExportRequest:
    """Every fact the Live export authorisation needs, as plain values.

    **Explicit rather than derived.** Each field is a question the caller has
    already answered elsewhere -- ownership, coherence, the quarter -- and passing
    the answers in is what makes the whole checklist visible in one function
    instead of spread across the refresh. This module still derives no ownership
    and reads no entity.
    """

    #: The admitted quarter's intent. Anything but ``net_export`` is refused.
    intent: str | None
    #: Whether a ``CarriedQuarter`` has been admitted at all.
    quarter_admitted: bool
    #: Whether ``now`` falls inside that quarter's own bounds. Separate from
    #: ``quarter_admitted`` because the two failures mean different things: one is
    #: "Stage A authorised nothing", the other is "the envelope has closed".
    quarter_open: bool
    #: The controller's ownership verdict, supplied not derived.
    owned: bool
    #: Whether the caller has **proven** that the running dispatch was caused by
    #: the run this quarter was admitted under.
    #:
    #: A supplied verdict, exactly like ``dispatch_owned``, and named so it cannot
    #: be mistaken for one of ``OwnershipEvidence``'s own fields. This module
    #: derives no ownership and must never learn how: the only derivation available
    #: from the vendor surface is parameter matching, which is worse than nothing
    #: here. ``False`` is the safe default and refuses the export.
    causation_proven: bool
    #: Whether a dispatch is running that is *not* ours.
    foreign_dispatch: bool
    #: Whether the control-grade coherence bound is currently usable.
    coherent: bool
    #: Another feature of the vendor surface driving the battery.
    conflicting_feature: bool
    missing_entities: tuple[str, ...] = ()
    failsafe_available: bool = False
    #: State of charge now, and the two floors it must stay above. Both are
    #: checked, because they are different promises: one is the user's
    #: configuration and one is the planner's dynamic reserve.
    soc_percent: float | None = None
    configured_min_soc_percent: float | None = None
    reserve_headroom_kwh: float | None = None
    #: What remains authorised this quarter, in the two domains.
    battery_remaining_kwh: float = 0.0
    grid_export_remaining_kwh: float = 0.0
    #: The power the caller intends to command, positive kW at the battery.
    requested_kw: float = 0.0
    #: Bounds it must respect. ``None`` means genuinely unconstrained.
    inverter_max_discharge_kw: float | None = None
    site_export_limit_kw: float | None = None
    tick_cap_kw: float | None = None


def authorize_export(request: ExportRequest) -> SafetyVerdict:
    """Return whether a Live ``net_export`` may be commanded, and why not if not.

    **A separate path, and deliberately not a relaxation of :func:`evaluate`.**
    That gate refuses any discharge above :func:`safe_discharge_power_kw`, and it
    is right to: it was written for the Phase-3 reserve guard, where energy
    reaching the meter is an accident. An authorised net export is the opposite --
    the meter is the *objective* -- so the same question has the opposite answer,
    and the honest way to express that is a second function rather than a
    condition threaded through the first.

    So :func:`evaluate`, :func:`safe_discharge_power_kw`,
    :func:`absorbing_capacity_kw` and ``limit_command`` are untouched by beta.27.
    The reserve-guard discharge still cannot export, ``INHIBIT_WOULD_EXPORT``
    still fires on it, and the absorbing-capacity clamp still binds it. Nothing
    that path did has been widened; this path simply did not exist before.

    **This authorises an economic intent. It is not an actuator path.** Nothing
    here writes, plans a step or names an entity. An authorised export continues
    through the same ownership, execution lock, Dispatch Mode 2 builders, write
    boundary, readback verification, dead-man and stop machinery a Live charge
    uses -- because there is one physical actuator and it is the validated
    Dispatch surface with positive signed power. The helper families are never
    written for an export, and ``serve_load`` is not admitted at all.

    Fails closed on every condition. The checks are ordered cheapest and most
    fundamental first, so the single reported reason is the most useful one, and
    every one of them is reached in :data:`EXPORT_AUTHORISATION_ORDER`.
    """
    checks: list[tuple[str, bool]] = []

    def check(name: str, passed: bool) -> bool:
        checks.append((name, passed))
        return passed

    def refuse(name: str) -> SafetyVerdict:
        return SafetyVerdict(False, name, tuple(checks))

    # -- is this even an export? --------------------------------------------
    if not check(
        EXPORT_REFUSE_NOT_EXPORT_INTENT, request.intent == EXECUTION_INTENT_NET_EXPORT
    ):
        return refuse(EXPORT_REFUSE_NOT_EXPORT_INTENT)
    # ``serve_load`` lands on the check above: it has no published meter target, so
    # there is no quantity a quarter could measure it against. Blocked by having no
    # entry in the executable intents rather than by being named.
    if not check(EXPORT_REFUSE_NO_QUARTER, request.quarter_admitted):
        return refuse(EXPORT_REFUSE_NO_QUARTER)
    if not check(EXPORT_REFUSE_QUARTER_NOT_OPEN, request.quarter_open):
        return refuse(EXPORT_REFUSE_QUARTER_NOT_OPEN)

    # -- can anything be commanded at all? ----------------------------------
    if not check(EXPORT_REFUSE_MISSING_ENTITY, not request.missing_entities):
        return refuse(EXPORT_REFUSE_MISSING_ENTITY)
    if not check(EXPORT_REFUSE_NO_FAILSAFE, request.failsafe_available):
        return refuse(EXPORT_REFUSE_NO_FAILSAFE)
    if not check(EXPORT_REFUSE_CONFLICTING_FEATURE, not request.conflicting_feature):
        return refuse(EXPORT_REFUSE_CONFLICTING_FEATURE)
    if not check(EXPORT_REFUSE_DISPATCH_FOREIGN, not request.foreign_dispatch):
        return refuse(EXPORT_REFUSE_DISPATCH_FOREIGN)

    # -- is it ours, and provably? ------------------------------------------
    if not check(EXPORT_REFUSE_NOT_OWNED, request.owned):
        return refuse(EXPORT_REFUSE_NOT_OWNED)
    if not check(EXPORT_REFUSE_RECORD_MISMATCH, request.causation_proven):
        return refuse(EXPORT_REFUSE_RECORD_MISMATCH)
    if not check(EXPORT_REFUSE_INCOHERENT, request.coherent):
        return refuse(EXPORT_REFUSE_INCOHERENT)

    # -- has the battery anything to give, above both floors? ---------------
    if not check(EXPORT_REFUSE_SOC_UNUSABLE, request.soc_percent is not None):
        return refuse(EXPORT_REFUSE_SOC_UNUSABLE)
    floor = request.configured_min_soc_percent
    if not check(EXPORT_REFUSE_MIN_SOC, floor is None or request.soc_percent > floor):
        return refuse(EXPORT_REFUSE_MIN_SOC)
    # **Absolute: no export price unlocks a reserve violation.** The reserve is a
    # promise about tonight, and a profitable quarter is not a reason to break it.
    if not check(
        EXPORT_REFUSE_RESERVE_FLOOR,
        request.reserve_headroom_kwh is None or request.reserve_headroom_kwh > 0.0,
    ):
        return refuse(EXPORT_REFUSE_RESERVE_FLOOR)

    # -- is there anything left authorised, in **both** domains? ------------
    if not check(
        EXPORT_REFUSE_NO_BATTERY_ALLOWANCE, request.battery_remaining_kwh > 0.0
    ):
        return refuse(EXPORT_REFUSE_NO_BATTERY_ALLOWANCE)
    if not check(
        EXPORT_REFUSE_NO_EXPORT_TARGET, request.grid_export_remaining_kwh > 0.0
    ):
        return refuse(EXPORT_REFUSE_NO_EXPORT_TARGET)

    # -- does the requested power respect every bound? ----------------------
    # Compared, never clamped: this function authorises or refuses, and the
    # clamping belongs to :mod:`.dispatch`, which has already applied the same
    # bounds in the documented order. Checking them again here is the barrier
    # against a caller that computed the setpoint some other way.
    power = request.requested_kw
    if not check(
        EXPORT_REFUSE_INVERTER_LIMIT,
        request.inverter_max_discharge_kw is None
        or power <= request.inverter_max_discharge_kw + 1e-9,
    ):
        return refuse(EXPORT_REFUSE_INVERTER_LIMIT)
    if not check(
        EXPORT_REFUSE_SITE_EXPORT_LIMIT,
        request.site_export_limit_kw is None
        or power <= request.site_export_limit_kw + 1e-9,
    ):
        return refuse(EXPORT_REFUSE_SITE_EXPORT_LIMIT)
    if not check(
        EXPORT_REFUSE_TICK_HORIZON,
        request.tick_cap_kw is None or power <= request.tick_cap_kw + 1e-9,
    ):
        return refuse(EXPORT_REFUSE_TICK_HORIZON)
    if not check(EXPORT_REFUSE_BELOW_DEVICE_MINIMUM, power >= CONTROL_MIN_POWER_KW):
        return refuse(EXPORT_REFUSE_BELOW_DEVICE_MINIMUM)
    if not check(EXPORT_REFUSE_ABOVE_DEVICE_MAXIMUM, power <= CONTROL_MAX_POWER_KW):
        return refuse(EXPORT_REFUSE_ABOVE_DEVICE_MAXIMUM)

    # -- and the direction, last, because it is the one that cannot be wrong -
    # Positive at the battery, which becomes positive signed Dispatch power. The
    # send site refuses the value independently; this is the same statement made
    # where the authorisation is decided.
    if not check(EXPORT_REFUSE_SIGN, power > 0.0):
        return refuse(EXPORT_REFUSE_SIGN)

    return SafetyVerdict(True, None, tuple(checks))


def authorize_reset(
    *,
    ownership: str,
    stopping_action: str | None,
    stop_reason: str | None,
    steps_planned: int,
) -> ExecutionDecision:
    """Return whether an owned dispatch may be returned to rest.

    **A different question from :func:`authorize_start`, and it has to be.** That
    one asks "should we begin moving energy?" and answers it with an intent, a
    verdict, a mode and an opt-in. None of those exist on a stopping refresh, so
    asking the first question about the second operation refuses it every time --
    which is exactly what beta.24 was measured doing, on every stop path it has.

    This asks one thing: *is Alpha EMS entitled to stop this dispatch?* It is,
    when it can prove the dispatch is its own.

    What it deliberately does **not** require, each for its own reason:

    * **an intent** -- a stop is not an economic command, and inventing one to
      satisfy a gate would be lying to the gate;
    * **a safe verdict** -- if a running charge has become unsafe, stopping it *is*
      the safe action. A verdict that blocked the response to itself would be the
      most dangerous check in the system;
    * **``mode == active``** -- the user selecting Shadow or Off is the stop
      request. Refusing it for not being in Live is circular;
    * **the opt-in** -- a user revoking permission mid-run must not thereby make a
      running charge unstoppable. Revocation cannot be the thing that strands it;
    * **the cooldown** -- a rate limit may delay a start. It may never delay a stop.

    What it does require is stronger than all five put together: a dispatch this
    integration can *prove* it caused. Foreign and unproven are not that, and stay
    untouchable.
    """
    if not CONTROL_EXECUTION_AVAILABLE:
        return ExecutionDecision(False, REFUSE_EXECUTION_UNAVAILABLE)
    if ownership != OWNERSHIP_OWNED:
        return ExecutionDecision(False, REFUSE_RESET_NOT_OWNED)
    if stopping_action is None:
        # Fails closed. A missing action is never defaulted to a charge: guessing
        # what to stop is how a stop becomes a start in the other direction.
        return ExecutionDecision(False, REFUSE_RESET_ACTION_UNKNOWN)
    if stopping_action not in CONTROL_EXECUTABLE_ACTIONS:
        return ExecutionDecision(False, REFUSE_LIVE_ACTION_NOT_PERMITTED)
    if not stop_reason:
        return ExecutionDecision(False, REFUSE_RESET_WITHOUT_REASON)
    if steps_planned <= 0:
        return ExecutionDecision(False, REFUSE_NO_COMMANDS)
    return ExecutionDecision(True, None)


def emergency_self_stop_authorized(
    *,
    dispatch_active: bool,
    marker_present_and_on: bool,
    record_matches_run: bool,
    readback_compatible: bool,
    contradicted: bool,
) -> bool:
    """Return whether the one emergency actuator operation is authorised.

    **This is not ownership, and the state it belongs to is never called owned.**
    Ownership requires the marker; when the marker has gone, ownership is
    ``degraded`` and that definition is not weakened to make a stop reachable.

    But letting a dispatch we can still *prove we caused* run until the device
    dead-man expires is less safe than stopping it deliberately -- up to twenty
    minutes of uncommanded charging against one write. So the stop gets its own
    authority, granted only when causation survives the marker:

    * the dispatch is still running -- there is nothing to stop otherwise;
    * the marker is **gone**, which is what makes this the emergency case rather
      than an ordinary stop;
    * the causal record is present and matches the admitted run;
    * the device readback still matches the expected mode **and sign**;
    * nothing contradicts any of it.

    Those last three are exactly the proof the foreign-dispatch invariant demands,
    which is why granting this does not weaken it: a dispatch whose Alpha-EMS
    causation cannot *still* be proven is never touched, stopped or overwritten.

    Strictly subtractive. It grants one operation and forbids everything else --
    no power, cutoff, duration, timer re-arm, mode, photovoltaic switch, run
    adoption, or clearing evidence before verified inactivity. What may be *sent*
    under it is enforced separately by :func:`authorize_emergency_self_stop`, so
    the grant and the envelope cannot drift apart.
    """
    if not dispatch_active:
        return False
    if marker_present_and_on:
        # Not the emergency case: ownership is intact, so the ordinary stop path
        # applies and this authority must not shadow it.
        return False
    if contradicted:
        return False
    return record_matches_run and readback_compatible


def authorize_emergency_self_stop(
    *,
    authorized: bool,
    steps: tuple[str, ...],
    attempts_made: int,
) -> ExecutionDecision:
    """Return whether *this exact* step list may be sent under the emergency grant.

    **The envelope, checked against the steps rather than against the intention.**
    The grant is one operation -- ``Dispatch enable -> OFF`` -- so anything else in
    the list is refused whole. That is what stops the authority quietly widening
    into "a stop, and while we are here, the resting values": those writes touch a
    dispatch that may still be running, and one of them restarts the vendor timer.

    Bounded retries, one per physical tick. After
    :data:`EMERGENCY_STOP_MAX_ATTEMPTS` the device dead-man is left to finish the
    job, because a write that has failed three times is not going to be persuaded
    by a fourth and the backstop already exists.
    """
    if not CONTROL_EXECUTION_AVAILABLE:
        return ExecutionDecision(False, REFUSE_EXECUTION_UNAVAILABLE)
    if not authorized:
        return ExecutionDecision(False, REFUSE_EMERGENCY_NOT_AUTHORIZED)
    if attempts_made >= EMERGENCY_STOP_MAX_ATTEMPTS:
        return ExecutionDecision(False, REFUSE_EMERGENCY_ATTEMPTS_SPENT)
    if tuple(steps) != (DISPATCH_ENABLE,):
        return ExecutionDecision(False, REFUSE_EMERGENCY_NOT_THE_STOP)
    return ExecutionDecision(True, None)


def authorize_marker_release(
    *,
    marker_is_stale: bool,
    steps_planned: int,
) -> ExecutionDecision:
    """Return whether a marker with nothing behind it may be cleared.

    **Separate from :func:`authorize_reset` on purpose.** "Release a marker behind
    nothing" and "stop a dispatch we own" are different entitlements, and folding
    them together is how one quietly acquires the other's permissions -- a single
    function with a ``kind`` flag would have one condition set covering two
    operations, which is the shape of the fault this whole amendment exists to fix.

    Clearing a stale marker is not an ownership claim: there is nothing to claim.
    That is why it needs no proof and no mode, and why it must be reachable in
    Shadow and in Off -- a marker left on by a crash would otherwise latch there for
    good.

    The one thing it must never do is release a marker while a dispatch is still
    running behind it -- that would assert a conclusion nobody has, and leave a
    dispatch nothing can later prove or stop. **Which is why staleness arrives here
    as a verdict rather than as evidence.** An earlier draft took ``marker_on`` and
    ``dispatch_active`` and combined them here, which put a second definition of
    "stale" in a module that must not decide ownership at all. There is one
    definition, ``execution.stale_marker``, and this is told its answer.
    """
    if not CONTROL_EXECUTION_AVAILABLE:
        return ExecutionDecision(False, REFUSE_EXECUTION_UNAVAILABLE)
    if not marker_is_stale:
        return ExecutionDecision(False, REFUSE_MARKER_STILL_DISPATCHING)
    if steps_planned <= 0:
        return ExecutionDecision(False, REFUSE_NO_COMMANDS)
    return ExecutionDecision(True, None)


def authorize_start(
    verdict: SafetyVerdict,
    context: ControlContext,
    *,
    commands_planned: int,
    starts_or_increases: bool,
    action: str | None = None,
) -> ExecutionDecision:
    """Return whether a safe command may actually be sent.

    **Permission to *begin or continue* moving energy**, and since beta.24 that is
    all it answers. It used to authorise stops as well, and it refused every one of
    them: a stop has no economic intent, so the verdict was unsafe; the user
    selecting Shadow *is* the stop request, so the mode check was circular; and a
    rate limit that delays a stop is a rate limit doing harm. See
    :func:`authorize_reset` for the other question.

    The only mode-aware stage of the start path, and the only one that can stop a
    write for a reason that is not a hazard.

    ``CONTROL_EXECUTION_AVAILABLE`` is a build-time constant rather than a setting,
    because a release barrier a user could clear is not a barrier. Since beta.24 it
    is *derived* from :data:`CONTROL_EXECUTABLE_ACTIONS`, so "does this release
    send anything" and "which direction may it send" cannot drift apart.

    ``action`` answers the second of those questions, and this stage had no answer
    to it before beta.24. The command source falls back to the Phase-3 reserve
    guard whenever Stage B has no charge to make, and the reserve guard
    discharges -- so a direction-blind authorisation would have permitted a
    discharge on the first refresh of a charge-only release. ``None`` is refused
    rather than waved through: an authorisation that cannot name what it is
    authorising is not one.

    Ordered most-fundamental-first, so a reader is told the deepest reason rather
    than whichever one a particular refresh happened to reach.
    """
    if not verdict.safe:
        return ExecutionDecision(False, REFUSE_UNSAFE, verdict.inhibit_reason)
    if context.mode != CONTROL_MODE_ACTIVE:
        return ExecutionDecision(False, REFUSE_MODE_NOT_ACTIVE)
    if not context.execution_enabled:
        return ExecutionDecision(False, REFUSE_EXECUTION_NOT_ENABLED)
    if not CONTROL_EXECUTION_AVAILABLE:
        return ExecutionDecision(False, REFUSE_EXECUTION_UNAVAILABLE)
    if commands_planned <= 0:
        return ExecutionDecision(False, REFUSE_NO_COMMANDS)
    if action not in CONTROL_EXECUTABLE_ACTIONS:
        return ExecutionDecision(False, REFUSE_LIVE_ACTION_NOT_PERMITTED)
    # A command that reduces battery movement is exempt: reducing can only
    # reduce risk, and delaying a reduction is the one thing a rate limit must
    # never do. No previous write means nothing to cool down from, which is not
    # the same as a write that happened zero seconds ago.
    if starts_or_increases:
        since = context.seconds_since_last_write
        if since is not None and since < CONTROL_COOLDOWN_SECONDS:
            return ExecutionDecision(False, REFUSE_COOLDOWN)
    return ExecutionDecision(True, None)


#: The old name, kept so every existing caller keeps meaning what it meant.
#:
#: beta.24 split authorisation in two and this one is the *start* path. Renaming it
#: without an alias would have silently repointed every call site at a function
#: whose contract had just narrowed, which is the sort of change that looks like a
#: rename and behaves like a rewrite.
authorize = authorize_start
