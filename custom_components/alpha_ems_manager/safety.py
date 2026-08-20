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

The gate **never scales a command.** If a request cannot be sent safely it is
refused whole, with one precise reason. A gate that reduced a request to make it
fit would have made a decision of its own, and deciding is not its job.

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

from .const import (
    ACTION_DISCHARGE,
    CONTROL_COOLDOWN_SECONDS,
    CONTROL_CUTOFF_MAX_PERCENT,
    CONTROL_CUTOFF_MIN_PERCENT,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_MAX_POWER_KW,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    INHIBIT_AT_OR_BELOW_FLOOR,
    INHIBIT_BATTERY_NOT_CONFIGURED,
    INHIBIT_BATTERY_POWER_STALE,
    INHIBIT_BATTERY_POWER_UNUSABLE,
    INHIBIT_CONTROL_ENTITY_UNAVAILABLE,
    INHIBIT_CUTOFF_OUT_OF_RANGE,
    INHIBIT_DISPATCH_ACTIVE,
    INHIBIT_DURATION_OUT_OF_RANGE,
    INHIBIT_EXCESS_EXPORT_ACTIVE,
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
    REFUSE_COOLDOWN,
    REFUSE_EXECUTION_NOT_ENABLED,
    REFUSE_EXECUTION_UNAVAILABLE,
    REFUSE_MODE_NOT_ACTIVE,
    REFUSE_NO_COMMANDS,
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
    #: Whether any dispatch is running. Ownership is never inferred, so this
    #: being true means "someone else's", full stop.
    dispatch_active: bool = False

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
    #: How old a reading may be before it is no longer evidence about now.
    max_source_age_seconds: float = 300.0

    # -- the quantised command, as numbers -----------------------------------
    #: Supplied by the adapter. Kept as bare values so this module needs no
    #: vendor type and cannot accidentally reach a vendor entity id.
    device_power_kw: float = 0.0
    device_cutoff_percent: int = 0
    device_duration_minutes: int = 0
    #: How far below the live house load a discharge must stay, in percent.
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
    # Any dispatch at all. Not "any dispatch that is not ours": nothing in the
    # control surface records who armed one, so there is no sound test for
    # ownership and none is attempted. Matching power, cutoff, duration or mode
    # proves nothing -- a person watching the shadow recommendation is exactly
    # who would arm the same figures by hand.
    if not check(INHIBIT_DISPATCH_ACTIVE, not context.dispatch_active):
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

    # -- would this push energy onto the grid? ------------------------------
    # A forced discharge sets the *battery* rate, so whatever the house cannot
    # absorb leaves through the meter -- and the dispatch path does not honour
    # the configured feed-in limit, so nothing downstream will catch it. The
    # whole command is refused rather than trimmed to fit: trimming it would be
    # this module choosing a different energy than the decision layer allowed.
    export_ok = True
    if intent.action == ACTION_DISCHARGE:
        house_kw = (context.house_load_w or 0.0) / 1000.0
        headroom = 1.0 - (context.export_margin_percent / 100.0)
        export_ok = context.device_power_kw <= house_kw * max(0.0, headroom)
    if not check(INHIBIT_WOULD_EXPORT, export_ok):
        return SafetyVerdict(False, INHIBIT_WOULD_EXPORT, tuple(checks))

    return SafetyVerdict(True, None, tuple(checks))


def authorize(
    verdict: SafetyVerdict,
    context: ControlContext,
    *,
    commands_planned: int,
    starts_or_increases: bool,
) -> ExecutionDecision:
    """Return whether a safe command may actually be sent.

    The only mode-aware stage, and the only one that can stop a write for a
    reason that is not a hazard.

    ``CONTROL_EXECUTION_AVAILABLE`` is checked here and is false in this
    release, so nothing reaches the inverter however the rest of the pipeline
    answers. It is a build-time constant rather than a setting because a release
    barrier a user could clear is not a barrier.
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
    # A command that reduces battery movement is exempt: reducing can only
    # reduce risk, and delaying a reduction is the one thing a rate limit must
    # never do. No previous write means nothing to cool down from, which is not
    # the same as a write that happened zero seconds ago.
    if starts_or_increases:
        since = context.seconds_since_last_write
        if since is not None and since < CONTROL_COOLDOWN_SECONDS:
            return ExecutionDecision(False, REFUSE_COOLDOWN)
    return ExecutionDecision(True, None)
