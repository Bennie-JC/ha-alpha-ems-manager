"""Phase 4: the whole AlphaESS mapping, and nothing that touches Home Assistant.

This is the only module that knows which battery is downstream. It holds the
helper entity ids, the quantisation each helper imposes, and the exact ordered
command list a control intent becomes. It imports no Home Assistant module, so
every one of those facts is testable against synthetic state -- which matters
more here than anywhere else in the integration, because this is the file whose
mistakes would reach an inverter.

The thin shell that actually reads the state machine and calls services lives in
``alphaess_adapter``. Splitting them is what lets the mapping be pure: shadow
mode and the real path run *this* code, identically, and only the shell differs.

**Why the helper surface and not the registers.** The control surface writes its
dispatch as a single multi-register block, which is what makes the register
ordering safe -- there is no sequence to get wrong. Reproducing that here would
mean a second copy of a working safety mechanism, plus owning a register offset
and a scaling factor this module currently never sees. Alpha EMS writes kilowatts
and percent into helpers and lets the tested implementation do the rest.

**Every quantisation resolves downwards.** The power helper has a 0.1 kW step
and the cutoff register truncates, so a naive mapping could deliver slightly more
energy than the decision layer allowed, or stop slightly below the floor the user
set. Both are corrected in the same direction: command less power, and ask for a
cutoff one percent higher. A command can therefore under-deliver by at most one
power step over one interval, and can never overshoot either bound.

**Flash memory.** The dispatch registers this maps onto are not flash-backed, but
the schedule, cutoff-schedule, feed-in-limit and grid-safety settings alongside
them are. None of those helpers appears in this file, and a structural test reads
the real source to confirm it -- including the two whose names differ from the
ones used here by a single word.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from .const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    CONTROL_CUTOFF_MAX_PERCENT,
    CONTROL_CUTOFF_MIN_PERCENT,
    CONTROL_DURATION_STEP_MINUTES,
    CONTROL_EXECUTABLE_DISPATCH_MODES,
    CONTROL_MAX_DURATION_MINUTES,
    CONTROL_MAX_POWER_KW,
    CONTROL_MIN_DURATION_MINUTES,
    CONTROL_MIN_POWER_KW,
    CONTROL_POWER_DECIMALS,
    CONTROL_POWER_STEP_KW,
    CONTROL_REFUSE_DIRECTION_MISMATCH,
    CONTROL_REFUSE_DISPATCH_MODE,
    CONTROL_REFUSE_DISPATCH_SIGN,
    CONTROL_REFUSE_FOREIGN_FAMILY,
    CONTROL_REFUSE_NEGATIVE_MAGNITUDE,
    CONTROL_REFUSE_RAW_DISPATCH_WRITE,
    CONTROL_REFUSE_SERVICE_NOT_PERMITTED,
)
from .control import ControlIntent

# --- the control surface ------------------------------------------------------

#: Read-back sensors. What the inverter is actually doing is observable, which is
#: what lets a write be *verified* on the next refresh. It is emphatically not
#: what lets one be *attributed*: none of these records who armed it.
SENSOR_DISPATCH_START = "sensor.alphaess_dispatch_start"
SENSOR_DISPATCH_MODE = "sensor.alphaess_dispatch_mode"
SENSOR_DISPATCH_ACTIVE_POWER = "sensor.alphaess_dispatch_active_power"
SENSOR_DISPATCH_SOC = "sensor.alphaess_dispatch_soc"
SENSOR_DISPATCH_TIME = "sensor.alphaess_dispatch_time"

#: Read for reporting only. It is a flash-backed grid-safety setting, and the
#: dispatch path does not honour it -- which is exactly why the export condition
#: exists in software instead.
SENSOR_MAX_FEED_TO_GRID = "sensor.alphaess_max_feed_to_grid"

#: The restart and communication-loss reset. Required before any command is
#: considered eligible: it covers the two cases Alpha EMS cannot cover from
#: inside its own process.
AUTOMATION_DISPATCH_RESET_FULL = "automation.alphaess_dispatch_reset_full"
#: Tears a dispatch down once the battery stops moving. Reported rather than
#: required: without it a command simply runs to its duration, which is safe,
#: just less prompt.
AUTOMATION_HOLD_MONITOR = "automation.alphaess_cutoff_soc_hold_monitoring"

#: Whether ownership can be established **from the AlphaESS surface alone**.
#:
#: Still false, and still not a policy choice -- it remains a property of that
#: surface. There is exactly one arming path per direction and it is driven
#: entirely by helper *values*, so a dispatch armed from a dashboard and one armed
#: by a service call leave byte-identical helper states, register values, timer and
#: read-backs. Nothing in the vendor package records a writer.
#:
#: Matching power, cutoff, duration or mode is therefore not evidence. It is worse
#: than no evidence: someone watching the shadow recommendation is exactly the
#: person who would arm those same figures by hand, so a parameter-match test would
#: be most confident precisely when it was most likely to be wrong. A call context
#: does not help either -- it cannot separate this integration from any other
#: automation, and a restart discards it.
#:
#: **This constant is kept at false on purpose, and no release has flipped it.**
#: What beta.19 added was a way to stop needing it: an owner marker written outside
#: the vendor package (:data:`BOOLEAN_EXECUTION_OWNER`), which records the one fact
#: the surface cannot. beta.20 supplies the second factor that marker was always
#: meant to be paired with -- a persisted causal record tied to a real device start
#: -- so ownership is positive evidence rather than inference. The inference this
#: constant describes stays forbidden either way, and a mutation test catches
#: anything that starts deriving ownership from parameters again.
OWNERSHIP_PROVABLE: bool = False

#: Features of the control surface that drive the battery on their own. If any of
#: them is on, Alpha EMS stands down rather than switching it off.
#:
#: **All six, since beta.25.** The repository modelled four; the package source
#: shows ``AlphaESS Dispatch`` turns off six families before arming and cancels
#: their timers. Two of them -- force import and force export -- were missing, and
#: a feature Alpha EMS does not know about is a feature it would have silently
#: destroyed by arming over it.
BOOLEAN_EXCESS_EXPORT = "input_boolean.alphaess_helper_excess_export"
BOOLEAN_PEAK_SHAVING = "input_boolean.alphaess_helper_peak_shaving"
BOOLEAN_FORCE_IMPORT = "input_boolean.alphaess_helper_force_import"
BOOLEAN_FORCE_EXPORT = "input_boolean.alphaess_helper_force_export"

#: The exact six the vendor automation disables, with the activation boolean each
#: one is recognised by. Ordered so diagnostics name them consistently.
#:
#: The charge and discharge families appear here as well as in :data:`FAMILIES`:
#: they are conflicts when somebody *else* turned them on, and ours when the
#: causal record says so. The distinction is ownership, never the entity.
CONFLICTING_FAMILIES: tuple[tuple[str, str], ...] = (
    ("force_charging", "input_boolean.alphaess_helper_force_charging"),
    ("force_discharging", "input_boolean.alphaess_helper_force_discharging"),
    ("force_import", BOOLEAN_FORCE_IMPORT),
    ("force_export", BOOLEAN_FORCE_EXPORT),
    ("excess_export", BOOLEAN_EXCESS_EXPORT),
    ("peak_shaving", BOOLEAN_PEAK_SHAVING),
)

#: The hold and pause companions the package cancels alongside each family.
#: Read for diagnosis only: a companion on with its family off is not a conflict,
#: because nothing is driving the battery.
CONFLICTING_COMPANIONS: tuple[str, ...] = (
    "input_boolean.alphaess_helper_force_charging_hold",
    "input_boolean.alphaess_helper_force_discharging_hold",
    "input_boolean.alphaess_helper_excess_export_pause",
    "input_boolean.alphaess_helper_peak_shaving_pause",
)

# --- the writable Dispatch surface -------------------------------------------
#
# **Read from the published Hillview package, not guessed.** Every entity below
# appears in ``integration_alpha_ess.yaml``; the register each one drives is noted
# because the encoding is what constrains the design rather than decorating it.
#
# The ``sensor.alphaess_dispatch_*`` entities are the device own readback and are
# **read-only** -- writing one is refused at the send site as a raw-dispatch write,
# and has been since Phase 4.

#: Arms the dispatch. Edge-triggered: ``on`` arms, and ``off`` triggers the
#: package own ``AlphaESS Dispatch Reset``, which writes Dispatch Start = 0. So no
#: reset button is needed and none is added.
DISPATCH_ENABLE = "input_boolean.alphaess_helper_dispatch"

#: The mode. An ``input_select`` whose *label* the package parses the number out
#: of, so the exact strings are load-bearing.
DISPATCH_MODE_SELECT = "input_select.alphaess_helper_dispatch_mode"

#: Signed power, -20..20 kW in steps of 0.1. Register ``0x0881 = 32000 + watts``.
#:
#: **Negative charges and positive discharges**, which is the opposite convention
#: from the helper families: those take a positive magnitude and carry direction in
#: which family was written. The two surfaces must never share a sign rule.
#:
#: Honoured in modes 1, 2, 3 and 5 only. In any other mode the package writes the
#: register as a bare ``32000``, which is zero watts -- so a mode outside that set
#: is not a controllable kW primitive at all.
DISPATCH_POWER = "input_number.alphaess_helper_dispatch_power"

#: Cutoff state of charge, 4..100 percent in steps of 1. Register
#: ``0x0886 = percent / 0.392``. Live in **mode 2 only**.
DISPATCH_CUTOFF_SOC = "input_number.alphaess_helper_dispatch_cutoff_soc"

#: Duration in minutes, 0..480 in steps of **5**. Register
#: ``0x0887 = minutes * 60``.
#:
#: Writing it while the dispatch is on rewrites the register *and* performs
#: ``timer.cancel`` + ``timer.start`` -- so the dead-man is re-armable live with no
#: enable toggle. But the automation triggers on a **state change**, so writing the
#: same value fires nothing. See :data:`DISPATCH_DEADMAN_MINUTES`.
DISPATCH_DURATION = "input_number.alphaess_helper_dispatch_duration"

#: Whether photovoltaic production is enabled during the dispatch. The package
#: ships it **on**, and on is the fail-safe state.
DISPATCH_PV_SWITCH = "input_boolean.alphaess_helper_dispatch_pv_switch"

#: The dead-man, readable. ``finishes_at`` is compared across refreshes so a
#: re-arm is measured rather than assumed.
DISPATCH_TIMER = "timer.alphaess_helper_dispatch_timer"

#: Every writable Dispatch entity, for the send-site subset test.
DISPATCH_ENTITIES: tuple[str, ...] = (
    DISPATCH_ENABLE,
    DISPATCH_MODE_SELECT,
    DISPATCH_POWER,
    DISPATCH_CUTOFF_SOC,
    DISPATCH_DURATION,
    DISPATCH_PV_SWITCH,
)

#: The mode labels, exactly as the package spells them.
#:
#: The number is parsed out of the label, so a label that is merely *close*
#: selects a different mode or none at all. They are written here once and never
#: rebuilt from the number.
DISPATCH_MODE_LABELS: dict[int, str] = {
    2: "State of Charge Control (2)",
    6: "Optimise Consumption (6)",
    7: "Maximise Consumption (7)",
}

#: The one mode beta.25 may select in Live, and the only one that takes both a
#: signed power and a live cutoff.
DISPATCH_MODE_SOC_CONTROL: int = 2

#: The dead-man, and the alternation that makes it re-armable.
#:
#: **The alternation is a workaround for a state-change trigger, not a policy.**
#: The vendor automation fires on the ``input_number`` changing state, so writing
#: the same duration twice re-arms nothing and the run would expire silently.
#: There is no cleaner path: the package exposes no ``script:`` section and no
#: service or event that re-triggers the duration automation.
#:
#: Both values sit on the helper 5-minute step. The semantic dead-man remains
#: approximately twenty minutes; the twenty-five is not a longer run, not more
#: energy and not a different horizon.
DISPATCH_DEADMAN_MINUTES: tuple[int, int] = (20, 25)

#: The owner marker. Not an AlphaESS helper, and that is the whole point.
#:
#: Every AlphaESS arming path is driven by helper values, so the vendor surface
#: cannot record who armed a dispatch. This one lives outside the package and
#: records exactly that: Alpha EMS turns it on as the **first** step of arming and
#: off as the **last** step of resetting, so a dispatch running without it is
#: somebody else's by construction rather than by inference.
#:
#: It costs no new permitted service -- ``turn_on`` and ``turn_off`` are already in
#: the closed set of three -- and it is never written in shadow.
BOOLEAN_EXECUTION_OWNER = "input_boolean.alpha_ems_dispatch_owner"


@dataclass(frozen=True, slots=True)
class HelperFamily:
    """The five helpers that arm one direction, plus its timer.

    ``activate`` is listed separately from the parameters because the order
    matters: turning it on is what triggers the write, so it has to observe
    settled values. See :func:`plan_commands`.
    """

    activate: str
    hold: str
    power: str
    cutoff_soc: str
    duration: str
    timer: str

    @property
    def parameters(self) -> tuple[str, ...]:
        """Return the helpers that must be set before activation."""
        return (self.power, self.cutoff_soc, self.duration, self.hold)

    @property
    def entities(self) -> tuple[str, ...]:
        """Return every entity in this family."""
        return (*self.parameters, self.activate, self.timer)


#: A battery discharge. The power helper is the **battery** discharge rate, so
#: the figure written is the figure the decision layer computed, in the units it
#: computed it in. The grid consequence is derived by the inverter, not commanded
#: here -- which is the entire reason the export condition is needed, since
#: whatever the house cannot absorb leaves through the meter.
DISCHARGE_FAMILY = HelperFamily(
    activate="input_boolean.alphaess_helper_force_discharging",
    hold="input_boolean.alphaess_helper_force_discharging_hold",
    power="input_number.alphaess_helper_force_discharging_power",
    cutoff_soc="input_number.alphaess_helper_force_discharging_cutoff_soc",
    duration="input_number.alphaess_helper_force_discharging_duration",
    timer="timer.alphaess_helper_force_discharging_timer",
)

#: A battery charge. Built and tested, and reached by nothing that ships: no
#: policy in this integration emits a charge, and a test asserts that over the
#: real shipped-policy list. It exists so a later phase has somewhere to land and
#: so the direction mapping is exercised now rather than written under pressure.
CHARGE_FAMILY = HelperFamily(
    activate="input_boolean.alphaess_helper_force_charging",
    hold="input_boolean.alphaess_helper_force_charging_hold",
    power="input_number.alphaess_helper_force_charging_power",
    cutoff_soc="input_number.alphaess_helper_force_charging_cutoff_soc",
    duration="input_number.alphaess_helper_force_charging_duration",
    timer="timer.alphaess_helper_force_charging_timer",
)

#: The direction map, and the one place it is decided.
#:
#: A discharge is a *battery* rate and a charge is a *battery* rate. The control
#: surface also offers grid-rate equivalents, which compensate for house load and
#: generation internally; mapping a battery decision onto one of those would
#: command a grid quantity from a battery figure and be wrong by the size of the
#: house load. Neither appears anywhere in this integration, and a structural
#: test confirms it.
FAMILIES: dict[str, HelperFamily] = {
    ACTION_DISCHARGE: DISCHARGE_FAMILY,
    ACTION_CHARGE: CHARGE_FAMILY,
}

#: Everything the adapter must find before a command can be considered.
REQUIRED_ENTITIES: tuple[str, ...] = (
    # **The owner marker, and its absence here was the beta.24 ownership fault.**
    # ``discover`` walks exactly this tuple, so an entity missing from it is an
    # entity nothing refuses to execute without. On an installation that had never
    # created the helper the arm wrote ``turn_on`` to a non-existent entity, the
    # write reported success, the causal record could never match, and every later
    # refresh read ``foreign`` -- a charge Alpha EMS could start and provably never
    # own, sustain or stop. It is listed first because it is the first thing the
    # arm writes and the first thing that must exist.
    BOOLEAN_EXECUTION_OWNER,
    SENSOR_DISPATCH_START,
    SENSOR_DISPATCH_MODE,
    SENSOR_DISPATCH_ACTIVE_POWER,
    SENSOR_DISPATCH_SOC,
    SENSOR_DISPATCH_TIME,
    AUTOMATION_DISPATCH_RESET_FULL,
    *DISCHARGE_FAMILY.entities,
    *CHARGE_FAMILY.entities,
)


# --- quantisation -------------------------------------------------------------


def device_power_kw(energy_ac_kwh: float, interval_hours: float) -> float:
    """Return the largest commandable power that does not over-deliver.

    The invariant, and the only thing this function promises:

        ``device_power_kw(e, h) * h <= e``

    It holds for every input, with equality exactly when the energy is a whole
    number of power steps. That is the safe direction: commanding less power can
    only leave the battery further from its floor.

    Dividing by a step of 0.1 does not land on integers -- ``0.3 / 0.1`` is
    ``2.9999999999999996`` -- so the floor needs a nudge, or a whole step is
    lost. The nudge could in principle overshoot, so the result is checked
    against the invariant and walked back.

    That check is a **measured backstop that does not fire**. Swept over every
    energy from 0 to 5 kWh in 1 Wh steps it walked back exactly zero times,
    because the only interval length this integration uses is a quarter hour --
    ``0.25`` exactly -- and multiplying by a power of two is lossless, so the
    comparison is exact. It is kept because the promise above must not quietly
    depend on that coincidence: a later phase that ever planned over a different
    interval would need it, and would get it for free.
    """
    if not math.isfinite(energy_ac_kwh) or not math.isfinite(interval_hours):
        return 0.0
    if energy_ac_kwh <= 0.0 or interval_hours <= 0.0:
        return 0.0

    # **Bounded by what the inverter can express.** The rolling controller is
    # allowed to raise power to catch up on a late run -- that is its job -- and
    # with no headroom cap in force a run late in its window asks for
    # ``remaining / remaining_hours``, which grows without limit as the denominator
    # shrinks. A probe measured a real campaign reaching 21.68 kW against a 20 kW
    # register.
    #
    # Clamping here rather than in the controller keeps the two concerns apart: how
    # much energy is wanted is a Stage-B question, and what the register can hold is
    # a property of the device. It is also the safe direction -- the invariant above
    # is ``power * h <= e``, and lowering the power can only strengthen it.
    #
    # Without this the safety gate refused the command outright for exceeding the
    # device maximum, so a late charge did not run at 20 kW; it did not run at all.
    energy_ac_kwh = min(energy_ac_kwh, CONTROL_MAX_POWER_KW * interval_hours)
    steps = math.floor(energy_ac_kwh / interval_hours / CONTROL_POWER_STEP_KW + 1e-9)
    power = round(steps * CONTROL_POWER_STEP_KW, CONTROL_POWER_DECIMALS)
    while steps > 0 and power * interval_hours > energy_ac_kwh:
        steps -= 1
        power = round(steps * CONTROL_POWER_STEP_KW, CONTROL_POWER_DECIMALS)
    return max(0.0, min(power, CONTROL_MAX_POWER_KW))


def device_cutoff_percent(floor_soc_percent: float) -> int:
    """Return the **discharge** cutoff, so the device never stops below the floor.

    The device's cutoff register truncates, so asking for exactly the floor lands
    a fraction of a percent below it. One extra percent compensates, and one is
    provably enough because the worst-case truncation loss is smaller than a
    percent.

    Clamped into the helper's own range afterwards. A user who set no reserve at
    all still gets the helper's minimum, which stops *higher* than they asked --
    conservative, and the decision layer's own clamp remains what actually bounds
    the energy.

    **A charge must not use this.** Its rounding protects a *lower* bound, and on
    a charge the cutoff is an *upper* one, so the +1 here would permit charging a
    percent past the ceiling. See :func:`device_charge_cutoff_percent`.
    """
    if not math.isfinite(floor_soc_percent):
        return CONTROL_CUTOFF_MAX_PERCENT
    target = math.ceil(floor_soc_percent) + 1
    return min(CONTROL_CUTOFF_MAX_PERCENT, max(CONTROL_CUTOFF_MIN_PERCENT, target))


def device_charge_cutoff_percent(ceiling_soc_percent: float | None) -> int | None:
    """Return the **charge** cutoff, or ``None`` when none can be established.

    Measured on the real installation: for a charge the cutoff is an *upper* state
    of charge -- a run with cutoff 90 % while the pack was below 90 % charged
    normally. So this is the mirror of :func:`device_cutoff_percent` in two ways,
    and both matter.

    **The rounding direction flips.** The register truncates, which lands at or
    slightly *below* the figure asked for. For a lower bound that is the dangerous
    direction and one percent is added to compensate; for an upper bound it is the
    *conservative* direction -- it stops a fraction early -- so nothing is added.
    Adding one here would permit charging a percent above the ceiling.

    **There is no fallback.** ``None`` in, ``None`` out, and the caller refuses the
    command. Substituting the discharge floor would write "stop at 21 %" to a pack
    at 61 %; substituting zero forbids charging; substituting 100 % charges past a
    ceiling the user chose. A refusal costs one window and says why.
    """
    if ceiling_soc_percent is None or not math.isfinite(ceiling_soc_percent):
        return None
    target = math.floor(ceiling_soc_percent)
    if target < CONTROL_CUTOFF_MIN_PERCENT:
        # Below the helper's own range there is nothing truthful to write: the
        # device cannot express it, and clamping upward would charge past it.
        return None
    return min(CONTROL_CUTOFF_MAX_PERCENT, target)


def device_duration_minutes(horizon_minutes: int) -> int:
    """Return the duration to command, snapped to the helper's step.

    A dead-man margin rather than a delivery window, so it is snapped to the
    nearest step and clamped into the device range. Whether it is long enough to
    outlive a planning interval is a safety question, and it is asked by the
    gate rather than answered by silently raising it here.
    """
    if horizon_minutes <= 0:
        return CONTROL_MIN_DURATION_MINUTES
    step = CONTROL_DURATION_STEP_MINUTES
    snapped = round(horizon_minutes / step) * step
    return min(
        CONTROL_MAX_DURATION_MINUTES,
        max(CONTROL_MIN_DURATION_MINUTES, snapped),
    )


# --- the command --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceCommand:
    """One intent, expressed in the numbers the device accepts.

    Deliberately a separate type from :class:`~.control.ControlIntent`, and the
    rename from ``average_power_kw`` to ``power_kw`` is the boundary: on one side
    it is an interval average that describes physics, on this side it is a
    setpoint that describes an instruction.
    """

    action: str
    power_kw: float
    cutoff_soc_percent: int
    duration_minutes: int
    #: Whether to leave the device forcing after its cutoff is reached.
    #:
    #: Always ``False``. Named for the device concept it is, because the decision
    #: layer's own "hold" means the opposite thing -- *do not move the battery* --
    #: and letting the two words meet would be a genuine hazard. Left off so the
    #: device tears its own dispatch down once the battery stops moving, which is
    #: one more automatic route back to normal operation.
    device_hold_flag: bool
    #: Carried through so a read-back that stopped at the cutoff can be told
    #: apart from one that stopped because the energy limit bound.
    energy_limit_bound: bool
    #: The energy this command is allowed to deliver, and what it actually will.
    allowed_energy_ac_kwh: float
    commanded_energy_ac_kwh: float
    #: How long the energy figures above are measured over, in hours. Carried so
    #: :func:`limit_command` can recompute them from a reduced power without
    #: dividing one by the other, which would be undefined at zero power and
    #: lossy everywhere else.
    interval_hours: float = 0.0
    #: The power quantisation alone produced, before any safety clamp. Equal to
    #: ``power_kw`` unless a clamp reduced it, which is what
    #: :attr:`safety_limited` reads.
    requested_power_kw: float = 0.0

    @property
    def safety_limited(self) -> bool:
        """Return whether a safety bound reduced this command's power.

        A reduction, never a substitution: the action, the cutoff and the
        duration are untouched, and only the power and the energy that follows
        from it move -- downwards.
        """
        return self.power_kw < self.requested_power_kw - 1e-12

    @property
    def moves_battery(self) -> bool:
        """Return whether this command asks the battery to do anything."""
        return self.action in FAMILIES and self.power_kw > 0.0

    @property
    def undelivered_energy_ac_kwh(self) -> float:
        """Return the energy quantisation gave up, always ``>= 0``."""
        return max(0.0, self.allowed_energy_ac_kwh - self.commanded_energy_ac_kwh)

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        return {
            "action": self.action,
            "power_kw": self.power_kw,
            "cutoff_soc_percent": self.cutoff_soc_percent,
            "duration_minutes": self.duration_minutes,
            "device_hold_flag": self.device_hold_flag,
            "energy_limit_bound": self.energy_limit_bound,
            "allowed_energy_ac_kwh": round(self.allowed_energy_ac_kwh, 4),
            "commanded_energy_ac_kwh": round(self.commanded_energy_ac_kwh, 4),
            "undelivered_energy_ac_kwh": round(self.undelivered_energy_ac_kwh, 4),
            "requested_power_kw": self.requested_power_kw,
            "safety_limited": self.safety_limited,
            "moves_battery": self.moves_battery,
            "quantisation_rule": (
                "power floored to the helper step so commanded energy never "
                "exceeds allowed energy; cutoff raised one percent because the "
                "device register truncates"
            ),
            "safety_limit_rule": (
                "a non-exporting discharge is clamped down to the largest step "
                "at or below the safely absorbable power, then its commanded "
                "energy is recomputed from the reduced power over the same "
                "duration; the duration and the cutoff are never changed to "
                "compensate"
            ),
            "hold_flag_note": (
                "device_hold_flag is the inverter's post-cutoff behaviour and is "
                "unrelated to a hold recommendation, which means the opposite"
            ),
        }


def build_command(intent: ControlIntent) -> DeviceCommand:
    """Quantise one intent into the numbers the device accepts.

    Pure and total. A hold, or an action with no direction mapping, yields a
    command that moves nothing rather than an error: there is always an answer to
    "what would you send", and for a hold the answer is "nothing".
    """
    if intent.action not in FAMILIES:
        return DeviceCommand(
            action=intent.action,
            power_kw=0.0,
            cutoff_soc_percent=device_cutoff_percent(intent.floor_soc_percent),
            duration_minutes=device_duration_minutes(intent.horizon_minutes),
            device_hold_flag=False,
            energy_limit_bound=intent.energy_limit_bound,
            allowed_energy_ac_kwh=intent.energy_ac_kwh,
            commanded_energy_ac_kwh=0.0,
            interval_hours=intent.interval_hours,
            requested_power_kw=0.0,
        )

    power = device_power_kw(intent.energy_ac_kwh, intent.interval_hours)
    cutoff = _cutoff_for(intent)
    if cutoff is None:
        # A charge with no establishable ceiling. Returned as a command that moves
        # nothing, so it is refused by the ordinary "does this move the battery"
        # path rather than by a special case, and no substituted bound is written.
        return DeviceCommand(
            action=intent.action,
            power_kw=0.0,
            cutoff_soc_percent=CONTROL_CUTOFF_MAX_PERCENT,
            duration_minutes=device_duration_minutes(intent.horizon_minutes),
            device_hold_flag=False,
            energy_limit_bound=intent.energy_limit_bound,
            allowed_energy_ac_kwh=intent.energy_ac_kwh,
            commanded_energy_ac_kwh=0.0,
            interval_hours=intent.interval_hours,
            requested_power_kw=0.0,
        )
    return DeviceCommand(
        action=intent.action,
        power_kw=power,
        cutoff_soc_percent=cutoff,
        duration_minutes=device_duration_minutes(intent.horizon_minutes),
        device_hold_flag=False,
        energy_limit_bound=intent.energy_limit_bound,
        allowed_energy_ac_kwh=intent.energy_ac_kwh,
        commanded_energy_ac_kwh=power * intent.interval_hours,
        interval_hours=intent.interval_hours,
        requested_power_kw=power,
    )


def _cutoff_for(intent: ControlIntent) -> int | None:
    """Return the cutoff for this intent's direction, or ``None`` to refuse.

    The one place the two semantics meet, and they never mix: a discharge asks the
    floor helper, a charge asks the ceiling helper, and neither can reach the
    other's. That separation is the whole fix -- beta.19 called the floor helper
    for both directions.
    """
    if intent.action == ACTION_CHARGE:
        return device_charge_cutoff_percent(intent.ceiling_soc_percent)
    return device_cutoff_percent(intent.floor_soc_percent)


def limit_command(command: DeviceCommand, max_power_kw: float) -> DeviceCommand:
    """Return the command reduced to at most ``max_power_kw``, or unchanged.

    Pure and total, and it can only ever **subtract**: the returned power is at
    most the power that went in, and the ceiling is applied through the same
    :func:`device_power_kw` contract that quantises everything else, so it is
    floored to a helper step and never rounded up past the bound.

    Only a discharge is limited. Only a discharge can export, and clamping a
    charge on an export bound would refuse to store energy for a reason that
    cannot apply to it.

    **Refusing is preferred to rounding up.** When nothing representable
    survives -- no capacity, or a safe power below :data:`CONTROL_MIN_POWER_KW`
    -- the command is returned *unchanged* rather than reduced to zero or raised
    to the minimum step. That is deliberate on both counts: the gate then refuses
    the original request whole, with the original ``would_export`` reason and the
    original figures, so nothing about the failure path or its wording changed in
    beta.15. Reducing it to zero here would have moved the refusal into this
    module and reported the wrong reason.

    The energy figures are recomputed from the reduced power over the **same**
    duration. The duration is never extended to make up the difference: this
    architecture has no delivery guarantee to preserve, each refresh supersedes
    the last, and stretching a command to compensate would be this module
    inventing a schedule. ``allowed_energy_ac_kwh`` is untouched, so the energy
    given up simply appears in ``undelivered_energy_ac_kwh``.
    """
    if command.action != ACTION_DISCHARGE or command.power_kw <= 0.0:
        return command
    if not math.isfinite(max_power_kw):
        # No provable bound is not a licence to send the request. Left for the
        # gate, which refuses it.
        return command
    if max_power_kw >= command.power_kw:
        return command

    hours = command.interval_hours
    if hours <= 0.0 or not math.isfinite(hours):  # pragma: no cover - build sets it
        return command

    limited = device_power_kw(max(0.0, max_power_kw) * hours, hours)
    if limited < CONTROL_MIN_POWER_KW or limited >= command.power_kw:
        return command

    return replace(
        command,
        power_kw=limited,
        commanded_energy_ac_kwh=limited * hours,
    )


# --- the command list ---------------------------------------------------------

SERVICE_SET_VALUE = ("input_number", "set_value")
#: The one service beta.25 adds, and it is added because the mode is an
#: ``input_select`` whose label the package parses. Nothing else needs it.
#: ``input_button.press`` is deliberately **not** added: turning the enable
#: boolean off already triggers the package reset.
SERVICE_SELECT_OPTION = ("input_select", "select_option")
SERVICE_TURN_ON = ("input_boolean", "turn_on")
SERVICE_TURN_OFF = ("input_boolean", "turn_off")

#: Every service this integration is permitted to call, as a closed set. A
#: structural test compares the real calls in the package against exactly this.
PERMITTED_SERVICES: frozenset[tuple[str, str]] = frozenset(
    {SERVICE_SET_VALUE, SERVICE_TURN_ON, SERVICE_TURN_OFF, SERVICE_SELECT_OPTION}
)


@dataclass(frozen=True, slots=True)
class CommandStep:
    """One service call, described without making it."""

    domain: str
    service: str
    entity_id: str
    value: float | None = None
    #: The option to select, for ``input_select.select_option``. Kept apart from
    #: :attr:`value` rather than overloading it: the dispatch mode is chosen by a
    #: *label* the vendor package parses a number out of, so a float would be the
    #: wrong type and a stringified float would select nothing at all.
    option: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        payload: dict[str, Any] = {
            "service": f"{self.domain}.{self.service}",
            "entity_id": self.entity_id,
        }
        if self.option is not None:
            payload["option"] = self.option
        if self.value is not None:
            payload["value"] = self.value
        return payload


def plan_commands(command: DeviceCommand) -> tuple[CommandStep, ...]:
    """Return the exact ordered steps a command becomes.

    Pure, and the single mapping both shadow and the real path use. Whatever
    shadow reports here is precisely what would have been sent.

    **Parameters first, activation last.** Turning the activation boolean on is
    what triggers the device write, so it must observe settled values. The happy
    consequence is that an interrupted sequence is inert rather than dangerous:
    the numbers mean nothing until the boolean changes, so a partial run commands
    nothing at all.

    Stopping is a **separate function** -- see :func:`plan_reset`. It is not a
    branch here, because the two sequences run in opposite orders for opposite
    reasons: arming settles its parameters before switching on, and resetting
    switches off before disturbing anything. Folding them together is how one of
    them ends up in the other's order.

    **The marker goes first, before any parameter.** If the sequence is interrupted
    after the marker and before activation, the result is a marker on with no
    dispatch running -- which reads as a stale marker and is cleared safely. The
    opposite order would leave a dispatch running with no marker, which reads as
    somebody else's and could never be stopped.
    """
    if not command.moves_battery:
        # Nothing to arm. Stopping is plan_reset's job, and a hold reaching here
        # must not quietly become one.
        return ()

    return plan_marker_claim() + plan_arm_parameters(command)


def plan_marker_claim() -> tuple[CommandStep, ...]:
    """Return stage one of the arm: claim ownership, and nothing else.

    **Separated since beta.24.1 so the claim can be verified before anything is
    armed.** A marker write that silently did nothing -- the entity absent, or
    unavailable -- used to be indistinguishable from one that worked, because the
    next step in the same list carried on regardless and the activation at the end
    of it started a dispatch nothing could prove was ours.

    Alone, this step is inert: no parameter is set and no activation is issued, so
    a sequence that stops here has claimed ownership of nothing and is cleared by
    the ordinary stale-marker path.
    """
    return (CommandStep(*SERVICE_TURN_ON, BOOLEAN_EXECUTION_OWNER),)


def plan_arm_parameters(command: DeviceCommand) -> tuple[CommandStep, ...]:
    """Return stage two of the arm: the parameters, then activation.

    Reachable only once :func:`plan_marker_claim` has been verified by readback.
    **Activation is still last**, for the reason it always was: it is what triggers
    the device write, so it must observe settled values.
    """
    if not command.moves_battery:
        return ()
    family = FAMILIES[command.action]
    return (
        CommandStep(*SERVICE_SET_VALUE, family.power, command.power_kw),
        CommandStep(
            *SERVICE_SET_VALUE,
            family.cutoff_soc,
            float(command.cutoff_soc_percent),
        ),
        CommandStep(
            *SERVICE_SET_VALUE,
            family.duration,
            float(command.duration_minutes),
        ),
        CommandStep(
            *(SERVICE_TURN_ON if command.device_hold_flag else SERVICE_TURN_OFF),
            family.hold,
        ),
        CommandStep(*SERVICE_TURN_ON, family.activate),
    )


def plan_sustain(command: DeviceCommand) -> tuple[CommandStep, ...]:
    """Return the steps that keep an already-armed dispatch alive.

    **The dead-man refresh, and it is a different sequence from arming because it
    has a different job.** The controller refreshes every fifteen minutes against a
    twenty-minute dead-man, so a run continues only because each refresh re-arms
    it. What that needs is the duration rewritten and activation re-issued; it does
    **not** need the power rewritten, and writing a helper a value it already holds
    is a service call that buys nothing.

    The cutoff *is* re-asserted. It is an upper bound on state of charge, and if the
    configured ceiling has moved since the run was armed, a sustain that skipped it
    would keep charging against the old one. One call to keep a safety bound current
    is worth making.

    No marker step: ownership is established by the time a sustain is reachable, and
    turning an already-on boolean on again says nothing. No power step, which is the
    whole point -- see :func:`plan_commands` for the arming sequence, which writes
    everything and is used whenever the power has materially moved.

    **Activation is last here too.** Same reason as arming: it is what triggers the
    device write, so it must observe settled values.
    """
    if not command.moves_battery:
        return ()
    family = FAMILIES[command.action]
    return (
        CommandStep(
            *SERVICE_SET_VALUE,
            family.cutoff_soc,
            float(command.cutoff_soc_percent),
        ),
        CommandStep(
            *SERVICE_SET_VALUE,
            family.duration,
            float(command.duration_minutes),
        ),
        CommandStep(
            *(SERVICE_TURN_ON if command.device_hold_flag else SERVICE_TURN_OFF),
            family.hold,
        ),
        CommandStep(*SERVICE_TURN_ON, family.activate),
    )


def write_refusal(command: DeviceCommand, steps: tuple[CommandStep, ...]) -> str | None:
    """Return why this command's step list must not be sent.

    A thin reading of :func:`action_refusal`, which is where the checks live. The
    two exist separately because a **reset** has no ``DeviceCommand`` -- a stopping
    refresh builds no command at all -- and the alternative was handing this
    function an object shaped like one, which is a lie told to a safety check.
    """
    return action_refusal(command.action, steps)


def dispatch_refusal(steps: tuple[CommandStep, ...]) -> str | None:
    """Return why a Dispatch step list must not be sent, or ``None`` if it may be.

    **The sign is part of the barrier, not a downstream check.** On the raw
    Dispatch surface direction is a *value*, not an entity, so the entity subset
    test that keeps a discharge off the helper families cannot see a wrong-way
    dispatch at all. This is the check that can.

    Two refusals:

    * a **positive** power. Negative charges and positive discharges, so a
      positive figure is the one thing beta.25 must never command. Zero is
      permitted and has to be: it is what the direction gate produces when the
      grid target would require a discharge, and it is what the cleanup writes.
      Holding the battery still is the physical meaning of "do not discharge".
    * a **mode** outside the executable set, compared by the exact package label.
      The package parses the number out of the label, so a near-miss string
      selects a different mode or none at all -- and modes 6 and 7 are not
      controllable kW primitives in any case, because the package writes the power
      register as a bare 32000 for anything outside modes 1, 2, 3 and 5.

    Reads no action field and trusts no caller, for the same reason
    :func:`action_refusal` does: "impossible" is a property of today code.
    """
    executable = {
        DISPATCH_MODE_LABELS[mode]
        for mode in CONTROL_EXECUTABLE_DISPATCH_MODES
        if mode in DISPATCH_MODE_LABELS
    }
    for step in steps:
        if step.entity_id == DISPATCH_POWER and (step.value or 0.0) > 0.0:
            return CONTROL_REFUSE_DISPATCH_SIGN
        if step.entity_id == DISPATCH_MODE_SELECT and step.option not in executable:
            return CONTROL_REFUSE_DISPATCH_MODE
    return None


def action_refusal(action: str, steps: tuple[CommandStep, ...]) -> str | None:
    """Return why this step list must not be sent, or ``None`` if it may be.

    **Checked against the real entity list, not against the intention that built
    it.** Layers above make a wrong-direction write impossible; this exists because
    "impossible" is a property of today's code, and the whole point of a boundary
    check is to survive tomorrow's.

    Five refusals, each naming a mistake a future edit could plausibly make:

    * a step touching the *other* direction's family -- the mistake that would
      turn a charge into a discharge;
    * a write to the raw dispatch surface, whose power field is **signed** and
      whose sign convention is the opposite of the helpers'. Alpha EMS writes
      helper families only, and mixing the two conventions is the classic version
      of this error;
    * a negative helper magnitude. The helper takes an unsigned battery rate --
      measured: +1.0 kW charges -- so a negative value there is either a sign
      confusion or a raw-surface value that has leaked in;
    * a service outside the closed permitted set;
    * an action with no family at all, arriving with steps attached.

    Any of them refuses the **entire** list. There are no partial writes: the
    activation step is last precisely so an interrupted sequence is inert, and
    honouring half a malformed command would throw that away.
    """
    if not steps:
        return None

    family = FAMILIES.get(action)
    if family is None:
        return CONTROL_REFUSE_DIRECTION_MISMATCH

    permitted = set(family.entities) | {BOOLEAN_EXECUTION_OWNER}
    foreign_families = [
        other for other_action, other in FAMILIES.items() if other_action != action
    ]
    foreign_entities = {
        entity for other in foreign_families for entity in other.entities
    }
    raw_surface = {
        SENSOR_DISPATCH_START,
        SENSOR_DISPATCH_MODE,
        SENSOR_DISPATCH_ACTIVE_POWER,
        SENSOR_DISPATCH_SOC,
        SENSOR_DISPATCH_TIME,
        SENSOR_MAX_FEED_TO_GRID,
    }

    for step in steps:
        if (step.domain, step.service) not in PERMITTED_SERVICES:
            return CONTROL_REFUSE_SERVICE_NOT_PERMITTED
        if step.entity_id in raw_surface:
            return CONTROL_REFUSE_RAW_DISPATCH_WRITE
        if step.entity_id in foreign_entities:
            return CONTROL_REFUSE_FOREIGN_FAMILY
        if step.entity_id not in permitted:
            return CONTROL_REFUSE_FOREIGN_FAMILY
        if step.entity_id == family.power and (step.value is None or step.value < 0.0):
            return CONTROL_REFUSE_NEGATIVE_MAGNITUDE

    return None


def plan_reset(action: str) -> tuple[CommandStep, ...]:
    """Return the exact ordered steps that end an owned dispatch.

    **Deactivation first**, which is the mirror of arming and for the mirrored
    reason. Turning the activation boolean off is what stops the device write, so
    doing it first means an interrupted reset leaves the dispatch *off* with some
    parameters still populated -- inert, and cleaned up by the next attempt. The
    other order would clear the parameters of a dispatch that was still running.

    **Setting power to zero is not a stop**, and this deliberately does more than
    that. A dispatch left armed at zero power still holds a duration, a cutoff and
    a timer, and the next run would inherit them -- so a short run following a long
    one would silently acquire the long one's dead-man. Every field a run depends
    on is returned to a resting value.

    **The marker goes last.** Until it is off, the dispatch is still owned, and
    releasing ownership before finishing the reset would leave Alpha EMS unable to
    finish its own cleanup.

    Only ever called for a dispatch ownership has been *established* for. Nothing
    here checks that, because the check belongs to the layer that knows the
    evidence -- but a reset issued without it would be exactly the "touch a
    dispatch it did not start" this project promises never to do.
    """
    return plan_reset_deactivate(action) + plan_reset_cleanup(action)


def plan_reset_deactivate(action: str | None) -> tuple[CommandStep, ...]:
    """Return stage one of the stop: switch the dispatch off, and nothing else.

    **Separated since beta.24.1 so inactivity is verified before anything else is
    written.** The rest of the reset disturbs fields a *running* dispatch depends
    on, and one of them makes it worse rather than better: writing the duration
    helper restarts the vendor package's timer, so a cleanup issued against a
    dispatch that did not actually stop would extend the very run it was trying to
    end.

    Empty when there is no direction to reset -- there is nothing to switch off,
    and the marker release in :func:`plan_reset_cleanup` is the whole operation.
    """
    family = FAMILIES.get(action)
    if family is None:
        return ()
    return (CommandStep(*SERVICE_TURN_OFF, family.activate),)


def plan_reset_cleanup(action: str | None) -> tuple[CommandStep, ...]:
    """Return stage two of the stop: resting values, then the marker.

    Reachable only once the dispatch has been *observed* inactive. **The marker
    goes last**: until it is off the dispatch is still owned, and releasing
    ownership before finishing the cleanup would leave Alpha EMS unable to finish
    its own.
    """
    family = FAMILIES.get(action)
    if family is None:
        # No direction to reset. The marker is still released, because a marker
        # with nothing behind it is the stale case and clearing it is safe.
        return (CommandStep(*SERVICE_TURN_OFF, BOOLEAN_EXECUTION_OWNER),)
    return (
        # Leave nothing a later run could inherit. Power to zero, and the
        #    dead-man to its own minimum rather than to zero -- the helper refuses
        #    values below its range, and a refused write is not a cleared field.
        CommandStep(*SERVICE_SET_VALUE, family.power, 0.0),
        CommandStep(
            *SERVICE_SET_VALUE,
            family.duration,
            float(CONTROL_MIN_DURATION_MINUTES),
        ),
        CommandStep(
            *SERVICE_SET_VALUE,
            family.cutoff_soc,
            float(CONTROL_CUTOFF_MIN_PERCENT),
        ),
        CommandStep(*SERVICE_TURN_OFF, family.hold),
        # Release ownership, having finished with it.
        CommandStep(*SERVICE_TURN_OFF, BOOLEAN_EXECUTION_OWNER),
    )


# --- the Dispatch sequences ---------------------------------------------------
#
# **A separate surface, and separate builders.** The helper families take a
# positive magnitude and carry direction in which family was written; raw Dispatch
# takes a signed power. Nothing below is reachable from the family builders above
# and nothing above is reachable from here, which is how the two sign conventions
# are kept from ever meeting in one expression.


def dispatch_mode_step(mode: int) -> CommandStep:
    """Return the step that selects a dispatch mode by its exact package label.

    Raises for a mode this integration has no label for, because a select that
    silently sends nothing is worse than one that fails loudly: the package parses
    the number out of the label, so a near-miss string chooses a different mode or
    none at all.
    """
    label = DISPATCH_MODE_LABELS.get(mode)
    if label is None:
        raise KeyError(f"no package label is known for dispatch mode {mode}")
    return CommandStep(*SERVICE_SELECT_OPTION, DISPATCH_MODE_SELECT, option=label)


def plan_dispatch_arm(
    *,
    mode: int,
    power_kw: float,
    cutoff_soc_percent: int,
    duration_minutes: int,
    pv_enabled: bool = True,
) -> tuple[CommandStep, ...]:
    """Return stage two of a Dispatch arm: parameters settled, **enable last**.

    Stage one is the ownership claim, which is :func:`plan_marker_claim` and is
    shared with the helper path -- the marker is not part of either surface.

    The enable is last for the reason it always was: it is edge-triggered, so it
    is what makes the settled values take effect. The happy consequence is that an
    interrupted sequence is inert rather than dangerous -- the numbers mean nothing
    until the boolean changes, so a partial run commands nothing at all.

    The PV switch is asserted rather than assumed. Its fail-safe state is **on**,
    and a previous run of ours may have left it off; re-asserting one boolean is
    cheaper than reasoning about who set it last.
    """
    return (
        dispatch_mode_step(mode),
        CommandStep(*SERVICE_SET_VALUE, DISPATCH_POWER, power_kw),
        CommandStep(*SERVICE_SET_VALUE, DISPATCH_CUTOFF_SOC, float(cutoff_soc_percent)),
        CommandStep(*SERVICE_SET_VALUE, DISPATCH_DURATION, float(duration_minutes)),
        CommandStep(
            *(SERVICE_TURN_ON if pv_enabled else SERVICE_TURN_OFF), DISPATCH_PV_SWITCH
        ),
        CommandStep(*SERVICE_TURN_ON, DISPATCH_ENABLE),
    )


def plan_dispatch_power(power_kw: float) -> tuple[CommandStep, ...]:
    """Return the one step a physical correction is allowed to be.

    **One entity, one write.** A power correction does not touch the duration --
    that would re-arm the dead-man on a cadence the economics never chose -- and
    does not touch the enable, because the dispatch stays on for the whole run.
    """
    return (CommandStep(*SERVICE_SET_VALUE, DISPATCH_POWER, power_kw),)


def plan_dispatch_cutoff(cutoff_soc_percent: int) -> tuple[CommandStep, ...]:
    """Return the step that moves the cutoff ceiling, live in mode 2 only."""
    return (
        CommandStep(*SERVICE_SET_VALUE, DISPATCH_CUTOFF_SOC, float(cutoff_soc_percent)),
    )


def plan_dispatch_rearm(duration_minutes: int) -> tuple[CommandStep, ...]:
    """Return the step that re-arms the device dead-man.

    Writing the duration while the dispatch is on rewrites the register **and**
    performs ``timer.cancel`` plus ``timer.start``, so no enable toggle is needed.
    But the vendor automation triggers on the ``input_number`` changing *state*, so
    the value has to differ from the one already there -- see
    :func:`dispatch.deadman_minutes`, which is what chooses it.
    """
    return (
        CommandStep(*SERVICE_SET_VALUE, DISPATCH_DURATION, float(duration_minutes)),
    )


def plan_dispatch_stop() -> tuple[CommandStep, ...]:
    """Return stage one of a Dispatch stop: switch it off, and nothing else.

    Turning the enable off triggers the package own ``AlphaESS Dispatch Reset``,
    which writes Dispatch Start = 0 -- so no reset button is needed and none is
    added.

    **This is also the whole of the emergency self-stop.** That authority grants
    exactly this one operation, so it is exactly this function, and there is no
    second definition of "the narrow stop" for it to drift away from.
    """
    return (CommandStep(*SERVICE_TURN_OFF, DISPATCH_ENABLE),)


def plan_dispatch_cleanup() -> tuple[CommandStep, ...]:
    """Return stage two of a Dispatch stop: resting values, then the marker.

    Reachable only once the dispatch has been *observed* inactive. Setting power
    to zero is not a stop, and this deliberately does more: a dispatch left armed
    at zero still holds a duration, a cutoff and a timer, and the next run would
    inherit them -- so a short run following a long one would silently acquire the
    long one dead-man.

    The PV switch is restored to **on**, its fail-safe state, and the marker goes
    last: until it is off the dispatch is still owned, and releasing ownership
    before finishing the cleanup would leave Alpha EMS unable to finish its own.
    """
    return (
        CommandStep(*SERVICE_SET_VALUE, DISPATCH_POWER, 0.0),
        CommandStep(
            *SERVICE_SET_VALUE, DISPATCH_DURATION, float(CONTROL_MIN_DURATION_MINUTES)
        ),
        CommandStep(
            *SERVICE_SET_VALUE, DISPATCH_CUTOFF_SOC, float(CONTROL_CUTOFF_MIN_PERCENT)
        ),
        CommandStep(*SERVICE_TURN_ON, DISPATCH_PV_SWITCH),
        CommandStep(*SERVICE_TURN_OFF, BOOLEAN_EXECUTION_OWNER),
    )


def plan_release_marker() -> tuple[CommandStep, ...]:
    """Return the single step that clears a stale marker.

    A marker on with no dispatch running. Clearing it is not an ownership claim --
    there is nothing to claim -- and it is the one write that is safe without one.
    """
    return (CommandStep(*SERVICE_TURN_OFF, BOOLEAN_EXECUTION_OWNER),)
