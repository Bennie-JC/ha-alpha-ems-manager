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
    CONTROL_MAX_DURATION_MINUTES,
    CONTROL_MIN_DURATION_MINUTES,
    CONTROL_MIN_POWER_KW,
    CONTROL_POWER_DECIMALS,
    CONTROL_POWER_STEP_KW,
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
#: **This constant is kept at false on purpose, and beta.19 does not flip it.**
#: What beta.19 adds is a way to stop needing it: an owner marker written outside
#: the vendor package (:data:`BOOLEAN_EXECUTION_OWNER`), which records the one fact
#: the surface cannot. Ownership is then positive evidence rather than inference,
#: and the inference this constant describes stays forbidden -- a mutation test
#: catches anything that starts deriving ownership from parameters again.
OWNERSHIP_PROVABLE: bool = False

#: Features of the control surface that drive the battery on their own. If either
#: is on, Alpha EMS stands down rather than switching it off.
BOOLEAN_EXCESS_EXPORT = "input_boolean.alphaess_helper_excess_export"
BOOLEAN_PEAK_SHAVING = "input_boolean.alphaess_helper_peak_shaving"

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

    steps = math.floor(energy_ac_kwh / interval_hours / CONTROL_POWER_STEP_KW + 1e-9)
    power = round(steps * CONTROL_POWER_STEP_KW, CONTROL_POWER_DECIMALS)
    while steps > 0 and power * interval_hours > energy_ac_kwh:
        steps -= 1
        power = round(steps * CONTROL_POWER_STEP_KW, CONTROL_POWER_DECIMALS)
    return max(0.0, power)


def device_cutoff_percent(floor_soc_percent: float) -> int:
    """Return the cutoff to command so the device never stops below the floor.

    The device's cutoff register truncates, so asking for exactly the floor lands
    a fraction of a percent below it. One extra percent compensates, and one is
    provably enough because the worst-case truncation loss is smaller than a
    percent.

    Clamped into the helper's own range afterwards. A user who set no reserve at
    all still gets the helper's minimum, which stops *higher* than they asked --
    conservative, and the decision layer's own clamp remains what actually bounds
    the energy.
    """
    if not math.isfinite(floor_soc_percent):
        return CONTROL_CUTOFF_MAX_PERCENT
    target = math.ceil(floor_soc_percent) + 1
    return min(CONTROL_CUTOFF_MAX_PERCENT, max(CONTROL_CUTOFF_MIN_PERCENT, target))


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
    return DeviceCommand(
        action=intent.action,
        power_kw=power,
        cutoff_soc_percent=device_cutoff_percent(intent.floor_soc_percent),
        duration_minutes=device_duration_minutes(intent.horizon_minutes),
        device_hold_flag=False,
        energy_limit_bound=intent.energy_limit_bound,
        allowed_energy_ac_kwh=intent.energy_ac_kwh,
        commanded_energy_ac_kwh=power * intent.interval_hours,
        interval_hours=intent.interval_hours,
        requested_power_kw=power,
    )


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
SERVICE_TURN_ON = ("input_boolean", "turn_on")
SERVICE_TURN_OFF = ("input_boolean", "turn_off")

#: Every service this integration is permitted to call, as a closed set. A
#: structural test compares the real calls in the package against exactly this.
PERMITTED_SERVICES: frozenset[tuple[str, str]] = frozenset(
    {SERVICE_SET_VALUE, SERVICE_TURN_ON, SERVICE_TURN_OFF}
)


@dataclass(frozen=True, slots=True)
class CommandStep:
    """One service call, described without making it."""

    domain: str
    service: str
    entity_id: str
    value: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        payload: dict[str, Any] = {
            "service": f"{self.domain}.{self.service}",
            "entity_id": self.entity_id,
        }
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

    family = FAMILIES[command.action]
    return (
        CommandStep(*SERVICE_TURN_ON, BOOLEAN_EXECUTION_OWNER),
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
    family = FAMILIES.get(action)
    if family is None:
        # No direction to reset. The marker is still released, because a marker
        # with nothing behind it is the stale case and clearing it is safe.
        return (CommandStep(*SERVICE_TURN_OFF, BOOLEAN_EXECUTION_OWNER),)
    return (
        # 1. Stop the dispatch.
        CommandStep(*SERVICE_TURN_OFF, family.activate),
        # 2. Leave nothing a later run could inherit. Power to zero, and the
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
        # 3. Release ownership, having finished with it.
        CommandStep(*SERVICE_TURN_OFF, BOOLEAN_EXECUTION_OWNER),
    )


def plan_release_marker() -> tuple[CommandStep, ...]:
    """Return the single step that clears a stale marker.

    A marker on with no dispatch running. Clearing it is not an ownership claim --
    there is nothing to claim -- and it is the one write that is safe without one.
    """
    return (CommandStep(*SERVICE_TURN_OFF, BOOLEAN_EXECUTION_OWNER),)
