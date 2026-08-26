"""Phase 4: the thin shell that reads the state machine and, one day, writes.

Everything interesting about the AlphaESS mapping -- which helper, which
direction, which quantisation, which order -- lives in ``alphaess_device`` and is
pure. This file only fetches values and, in some future release, calls services.
Keeping the split means shadow mode and the real path share the whole mapping and
differ solely in whether this shell is reached.

Nothing here writes anything in this release. :func:`async_execute` exists,
is imported, and is unit-tested, but it refuses to run: the release barrier is
checked inside it as well as before it, so a caller that forgot to authorize
cannot get through. That is the difference between execution being disabled and
execution being unreachable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback

from .alphaess_device import (
    AUTOMATION_DISPATCH_RESET_FULL,
    AUTOMATION_HOLD_MONITOR,
    BOOLEAN_EXCESS_EXPORT,
    BOOLEAN_EXECUTION_OWNER,
    BOOLEAN_PEAK_SHAVING,
    CHARGE_FAMILY,
    CONFLICTING_FAMILIES,
    DISCHARGE_FAMILY,
    DISPATCH_CUTOFF_SOC,
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_ENTITIES,
    DISPATCH_MODE_LABELS,
    DISPATCH_MODE_SELECT,
    DISPATCH_POWER,
    DISPATCH_PV_SWITCH,
    DISPATCH_TIMER,
    FAMILIES,
    OWNERSHIP_PROVABLE,
    REQUIRED_ENTITIES,
    SENSOR_DISPATCH_ACTIVE_POWER,
    SENSOR_DISPATCH_MODE,
    SENSOR_DISPATCH_SOC,
    SENSOR_DISPATCH_START,
    SENSOR_DISPATCH_TIME,
    SENSOR_MAX_FEED_TO_GRID,
    CommandStep,
    dispatch_refusal,
)
from .const import (
    CONTROL_EXECUTABLE_ACTIONS,
    CONTROL_EXECUTABLE_DISPATCH_MODES,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_REFUSE_ACTION_NOT_EXECUTABLE,
    MARKER_ABSENT,
    MARKER_OFF,
    MARKER_ON,
    MARKER_UNAVAILABLE,
    MAX_CONTROL_EVENTS_REPORTED,
)
from .execution import instant_of
from .normalization import parse_numeric

_LOGGER = logging.getLogger(__name__)

#: States that mean "this entity is here but is telling you nothing".
_UNUSABLE_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})


class ControlExecutionUnavailable(RuntimeError):
    """Raised when execution is attempted in a release that cannot execute.

    Unreachable through the pipeline, which refuses long before this point. It
    exists so that the barrier is enforced at the last possible moment as well as
    the first, and so a future caller who wires past the authorization step gets
    a loud failure instead of a quiet inverter command.
    """


class ControlActionNotPermitted(RuntimeError):
    """Raised when a step would write outside the actions this release executes.

    **The last interlock, and the only one that reads the wire rather than the
    intention.** Every check above it reasons about a ``DeviceCommand``; this one
    compares entity ids against the set beta.24 may touch, so a command that lies
    about its own action is refused anyway.

    One subset test catches a discharge, an export, a raw-dispatch write, the
    wrong helper family and an entity nobody recognises. It carries the offending
    ids, so a reader is told *what* was refused rather than merely that something
    was.
    """

    def __init__(self, reason: str, entity_ids: tuple[str, ...]) -> None:
        """Store the refusal and the steps that caused it."""
        permitted = sorted(CONTROL_EXECUTABLE_ACTIONS) or ["nothing"]
        super().__init__(
            f"{reason}: this release executes {', '.join(permitted)} and these "
            f"steps fall outside it: {', '.join(entity_ids)}"
        )
        self.reason = reason
        self.entity_ids = entity_ids


@dataclass(frozen=True, slots=True)
class DeviceCapability:
    """What of the control surface was actually found.

    Names are carried rather than counted so a *renamed* helper is visible as a
    specific absence instead of a vague shortfall -- which is the difference
    between "your package moved on" and "something is wrong".
    """

    missing: tuple[str, ...]
    unavailable: tuple[str, ...]
    failsafe_available: bool
    failsafe_state: str | None
    hold_monitor_available: bool
    excess_export_active: bool
    peak_shaving_active: bool
    #: The raw states of the two feature booleans, carried the way
    #: ``failsafe_state`` already is.
    #:
    #: Needed because neither boolean is in ``REQUIRED_ENTITIES``, so neither
    #: appears in ``missing`` or ``unavailable`` -- which meant an *unreadable*
    #: Excess Export boolean was indistinguishable from one switched off. That is
    #: the unsafe direction for the surplus-absorption question: Excess Export
    #: sends production to the grid rather than the battery, so reading it as off
    #: when it cannot be read at all would claim stored energy that is being
    #: exported.
    excess_export_state: str | None = None
    peak_shaving_state: str | None = None
    max_feed_to_grid_percent: float | None = None

    @property
    def feature_flags_present(self) -> bool:
        """Return whether both feature booleans exist on this installation.

        False means the vendor package is absent, so neither feature exists and
        neither can be suppressing anything.
        """
        return (
            self.excess_export_state is not None and self.peak_shaving_state is not None
        )

    @property
    def feature_flags_readable(self) -> bool:
        """Return whether both feature booleans could actually be read.

        A boolean that exists and reads ``unavailable`` could be hiding a feature
        that is switched on, which is a different answer from one that is absent.
        """
        return all(
            state is not None and state not in _UNUSABLE_STATES
            for state in (self.excess_export_state, self.peak_shaving_state)
        )

    @property
    def ready(self) -> bool:
        """Return whether a command could be considered at all."""
        return not self.missing and not self.unavailable and self.failsafe_available

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form.

        Both name lists are capped with a total beside them. A missing package
        means every required entity is absent at once, which is more entries than
        any list in this payload is allowed to carry.
        """
        return {
            "ready": self.ready,
            "required_entities": len(REQUIRED_ENTITIES),
            "missing": list(self.missing[:MAX_CONTROL_EVENTS_REPORTED]),
            "missing_total": len(self.missing),
            "unavailable": list(self.unavailable[:MAX_CONTROL_EVENTS_REPORTED]),
            "unavailable_total": len(self.unavailable),
            "failsafe_automation": AUTOMATION_DISPATCH_RESET_FULL,
            "failsafe_available": self.failsafe_available,
            "failsafe_state": self.failsafe_state,
            "hold_monitor_available": self.hold_monitor_available,
            "excess_export_active": self.excess_export_active,
            "peak_shaving_active": self.peak_shaving_active,
            "max_feed_to_grid_percent": self.max_feed_to_grid_percent,
            "max_feed_to_grid_note": (
                "read and reported only; it is a flash-backed grid-safety "
                "setting and the dispatch path does not honour it, which is why "
                "export is checked in software instead"
            ),
        }


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """What the inverter is doing, as far as it can be observed.

    Enough to *verify* a command on the next refresh. Not enough to *attribute*
    one: see :data:`~.alphaess_device.OWNERSHIP_PROVABLE`.
    """

    dispatch_active: bool
    dispatch_start: float | None
    dispatch_mode: float | None
    dispatch_power_w: float | None
    dispatch_soc_percent: float | None
    dispatch_time_s: float | None
    #: Which activation booleans are on, if any.
    active_modes: tuple[str, ...]
    #: Whether the owner marker is on. Read since beta.19, and the only positive
    #: evidence that Alpha EMS armed what is running. ``None`` when the marker
    #: entity does not exist, which is not the same as "off": a missing marker
    #: means ownership cannot be established at all.
    owner_marker: bool | None = None
    #: Whether the charge family's dead-man timer is running, and when it ends.
    #:
    #: **Read since beta.24, and read for one reason: to check a claim instead of
    #: trusting it.** A run continues only because each refresh re-arms it, and
    #: whether re-activating an already-active dispatch refreshes that timer is a
    #: property of the control surface rather than of this integration. With
    #: ``finishes_at`` in hand the controller compares one refresh against the
    #: next and *knows*, rather than assuming and finding out when a charge stops
    #: early.
    #:
    #: ``None`` for either field means the timer could not be read, which is
    #: treated as "no evidence it advanced" rather than as agreement.
    charge_timer_active: bool | None = None
    charge_timer_finishes_at: datetime | None = None
    # --- the Dispatch surface, read back -------------------------------------
    #
    # **Read since beta.25, and read because ownership now depends on it.** The
    # signed power is the only place direction lives on this surface, so a
    # readback that did not carry it could not tell an owned charge from somebody
    # else's discharge.
    #
    #: Whether the Dispatch enable helper is on. ``None`` when it cannot be read,
    #: which is no evidence rather than agreement.
    dispatch_enabled: bool | None = None
    #: The selected mode, matched against the labels this integration knows.
    #: ``None`` for a label we have no name for -- which is a mode we cannot own.
    dispatch_selected_mode: int | None = None
    #: The commanded signed power, kW. Negative charges, positive discharges.
    dispatch_setpoint_kw: float | None = None
    dispatch_cutoff_percent: float | None = None
    dispatch_duration_minutes: float | None = None
    #: Whether photovoltaic production is enabled during the dispatch.
    dispatch_pv_enabled: bool | None = None
    #: The Dispatch dead-man, read rather than assumed.
    dispatch_timer_active: bool | None = None
    dispatch_timer_finishes_at: datetime | None = None
    #: The conflicting families currently on, by name. Read for the pre-arm gate,
    #: and never written -- standing down is the response, not switching them off.
    conflicting_active: tuple[str, ...] = ()
    #: The marker as a typed state, for diagnosis rather than for attribution.
    #: Beside :attr:`owner_marker` rather than replacing it -- ownership is still
    #: computed from the boolean, and beta.25 does not change that arithmetic.
    owner_marker_state: str = MARKER_ABSENT

    @property
    def marker_present(self) -> bool:
        """Return whether the marker entity exists to be read."""
        return self.owner_marker is not None

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        return {
            "dispatch_active": self.dispatch_active,
            "dispatch_start": self.dispatch_start,
            "dispatch_mode": self.dispatch_mode,
            "dispatch_power_w": self.dispatch_power_w,
            "dispatch_soc_percent": self.dispatch_soc_percent,
            "dispatch_time_s": self.dispatch_time_s,
            "active_modes": list(self.active_modes[:MAX_CONTROL_EVENTS_REPORTED]),
            "owner_marker": self.owner_marker,
            "owner_marker_state": self.owner_marker_state,
            "owner_marker_entity": BOOLEAN_EXECUTION_OWNER,
            "owner_marker_rule": (
                "absent means the helper does not exist, which is not off: "
                "without it ownership cannot be established at all, so the "
                "capability reports it missing and no charge may be armed"
            ),
            "charge_timer_active": self.charge_timer_active,
            "charge_timer_finishes_at": (
                None
                if self.charge_timer_finishes_at is None
                else self.charge_timer_finishes_at.isoformat()
            ),
            "charge_timer_entity": CHARGE_FAMILY.timer,
            "dispatch_enabled": self.dispatch_enabled,
            "dispatch_selected_mode": self.dispatch_selected_mode,
            "dispatch_setpoint_kw": self.dispatch_setpoint_kw,
            "dispatch_cutoff_percent": self.dispatch_cutoff_percent,
            "dispatch_duration_minutes": self.dispatch_duration_minutes,
            "dispatch_pv_enabled": self.dispatch_pv_enabled,
            "dispatch_timer_active": self.dispatch_timer_active,
            "dispatch_timer_finishes_at": (
                None
                if self.dispatch_timer_finishes_at is None
                else self.dispatch_timer_finishes_at.isoformat()
            ),
            "dispatch_timer_entity": DISPATCH_TIMER,
            "conflicting_active": list(self.conflicting_active),
            "dispatch_sign_rule": (
                "negative charges and positive discharges. the opposite "
                "convention from the helper families, which take a positive "
                "magnitude and carry direction in which family was written"
            ),
            "charge_timer_rule": (
                "the device dead-man, read rather than assumed. a sustaining "
                "re-arm must move finishes_at forward; if it does not, the run "
                "is ending whatever the controller believes, so it is stopped "
                "deliberately instead of silently"
            ),
            # Ownership of a *running* dispatch, from the marker alone. The
            # controller requires a matching causal record on top of this before
            # it will act, so this field is evidence rather than a verdict.
            "owned": bool(self.dispatch_active and self.owner_marker),
            "ownership_provable": OWNERSHIP_PROVABLE,
            "ownership_note": (
                "the vendor surface still records no writer, so parameters are "
                "never evidence. since beta.19 ownership rests on a marker "
                "outside that surface plus a persisted causal record, and both "
                "are required -- a dispatch running without the marker is "
                "someone else's and is never modified, reset or cancelled"
            ),
        }


@callback
def _state_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return an entity's state string, or ``None`` when it does not exist."""
    state = hass.states.get(entity_id)
    return None if state is None else state.state


@callback
def _boolean_state(hass: HomeAssistant, entity_id: str) -> bool | None:
    """Return whether a boolean helper is on, or ``None`` if unreadable."""
    state = _state_of(hass, entity_id)
    if state is None or state in _UNUSABLE_STATES:
        return None
    return state == STATE_ON


@callback
def conflicting_families_active(hass: HomeAssistant) -> tuple[str, ...]:
    """Return the names of the conflicting families that are currently on.

    **All six the vendor automation disables**, read so Alpha EMS can stand down
    rather than let ``AlphaESS Dispatch`` silently switch off a feature the user
    chose. Read only: nothing here is ever written.
    """
    return tuple(
        name
        for name, entity in CONFLICTING_FAMILIES
        if _state_of(hass, entity) == STATE_ON
    )


@callback
def dispatch_mode_of(label: str | None) -> int | None:
    """Return the mode number a package label names, or ``None``.

    Matched against the labels this integration knows rather than parsed out of
    the string. A label we have no name for is a mode we cannot claim to own, and
    pulling a number out of arbitrary text is how a readback comes to agree with
    something it never understood.
    """
    if label is None:
        return None
    for mode, known in DISPATCH_MODE_LABELS.items():
        if label == known:
            return mode
    return None


@callback
def dispatch_readback_compatible(
    snapshot: DeviceSnapshot, *, expected_mode: int, expected_sign: int
) -> bool:
    """Return whether the running Dispatch matches what we believe we armed.

    **Mode *and* sign, and the sign is the half a helper-family check could not
    express.** On this surface direction is a signed number rather than a choice
    of entity, so a readback agreeing on mode alone would call somebody else's
    discharge our charge.

    Fails closed on anything unreadable: no evidence is not agreement.
    """
    if snapshot.dispatch_selected_mode != expected_mode:
        return False
    setpoint = snapshot.dispatch_setpoint_kw
    if setpoint is None:
        return False
    if setpoint == 0.0:
        # Zero is a value we legitimately command -- the direction gate produces
        # it -- and it is compatible with either sign because it asserts neither.
        return True
    return (setpoint < 0.0) == (expected_sign < 0)


@callback
def marker_state(hass: HomeAssistant) -> str:
    """Return the owner marker's state as one of :data:`MARKER_STATES`.

    **Four distinguishable facts, because beta.24 had one boolean and it hid the
    fault.** ``owner_marker`` was ``None`` for a missing helper and ``False`` for
    one that is off, and every reader that mattered treated both as "not ours" --
    which is correct for attribution and useless for diagnosis. A user whose
    charge never owned anything needs to be told the helper does not exist, not
    that a marker is off.

    Never ``unverified``: that state is a property of a *write*, not of a reading,
    and only the staged arm can observe it.
    """
    state = hass.states.get(BOOLEAN_EXECUTION_OWNER)
    if state is None:
        return MARKER_ABSENT
    if state.state in _UNUSABLE_STATES:
        return MARKER_UNAVAILABLE
    return MARKER_ON if state.state == STATE_ON else MARKER_OFF


@callback
def marker_verified_on(hass: HomeAssistant) -> bool:
    """Return whether the marker can be *read back* as on, right now.

    The verification stage-one of the arm is gated on. Deliberately a positive
    test: anything other than a readable, present, on marker is a failure, so a
    new failure mode cannot pass by not being listed.
    """
    return marker_state(hass) == MARKER_ON


@callback
def _numeric_state(hass: HomeAssistant, entity_id: str) -> float | None:
    """Return an entity's state as a finite number, or ``None``."""
    return parse_numeric(_state_of(hass, entity_id))


@callback
def discover(hass: HomeAssistant) -> DeviceCapability:
    """Report which parts of the control surface are present and usable.

    Absence is a finding, not a failure: this integration must load and keep
    learning on an installation that has no control surface at all, so nothing
    here raises and nothing here requires the package to exist.
    """
    missing: list[str] = []
    unusable: list[str] = []
    for entity_id in REQUIRED_ENTITIES:
        state = _state_of(hass, entity_id)
        if state is None:
            missing.append(entity_id)
        elif state in _UNUSABLE_STATES:
            unusable.append(entity_id)

    failsafe = _state_of(hass, AUTOMATION_DISPATCH_RESET_FULL)
    monitor = _state_of(hass, AUTOMATION_HOLD_MONITOR)

    return DeviceCapability(
        missing=tuple(missing),
        unavailable=tuple(unusable),
        # Present *and* switched on. An automation that exists but is off will
        # not clear a dispatch after a restart, which is the one thing this
        # integration is relying on it for.
        failsafe_available=failsafe == STATE_ON,
        failsafe_state=failsafe,
        hold_monitor_available=monitor == STATE_ON,
        excess_export_active=_state_of(hass, BOOLEAN_EXCESS_EXPORT) == STATE_ON,
        peak_shaving_active=_state_of(hass, BOOLEAN_PEAK_SHAVING) == STATE_ON,
        excess_export_state=_state_of(hass, BOOLEAN_EXCESS_EXPORT),
        peak_shaving_state=_state_of(hass, BOOLEAN_PEAK_SHAVING),
        max_feed_to_grid_percent=_numeric_state(hass, SENSOR_MAX_FEED_TO_GRID),
    )


@callback
def read_snapshot(hass: HomeAssistant) -> DeviceSnapshot:
    """Read back what the inverter is currently doing."""
    start = _numeric_state(hass, SENSOR_DISPATCH_START)
    active_modes = tuple(
        family.activate
        for family in (DISCHARGE_FAMILY, CHARGE_FAMILY)
        if _state_of(hass, family.activate) == STATE_ON
    )
    # A dispatch counts as running if the register says so *or* an activation
    # boolean is on. The two can disagree for a second or two while the control
    # surface settles, and during that window the safe reading is the pessimistic
    # one.
    marker = _state_of(hass, BOOLEAN_EXECUTION_OWNER)
    dispatch_timer = hass.states.get(DISPATCH_TIMER)
    dispatch_running = (
        None if dispatch_timer is None else dispatch_timer.state == "active"
    )
    dispatch_finishes = (
        None
        if dispatch_timer is None
        else instant_of(dispatch_timer.attributes.get("finishes_at"))
    )
    timer_state = hass.states.get(CHARGE_FAMILY.timer)
    timer_active = None if timer_state is None else timer_state.state == "active"
    finishes_at = None
    if timer_state is not None:
        finishes_at = instant_of(timer_state.attributes.get("finishes_at"))
    return DeviceSnapshot(
        dispatch_active=bool(start) or bool(active_modes),
        dispatch_start=start,
        dispatch_mode=_numeric_state(hass, SENSOR_DISPATCH_MODE),
        dispatch_power_w=_numeric_state(hass, SENSOR_DISPATCH_ACTIVE_POWER),
        dispatch_soc_percent=_numeric_state(hass, SENSOR_DISPATCH_SOC),
        dispatch_time_s=_numeric_state(hass, SENSOR_DISPATCH_TIME),
        active_modes=active_modes,
        # ``None`` when the helper does not exist. Absent is not off: without the
        # marker there is no way to establish ownership, and the controller must
        # treat a running dispatch as foreign rather than assume it is free.
        owner_marker=None if marker is None else marker == STATE_ON,
        charge_timer_active=timer_active,
        charge_timer_finishes_at=finishes_at,
        owner_marker_state=marker_state(hass),
        dispatch_enabled=_boolean_state(hass, DISPATCH_ENABLE),
        dispatch_selected_mode=dispatch_mode_of(_state_of(hass, DISPATCH_MODE_SELECT)),
        dispatch_setpoint_kw=_numeric_state(hass, DISPATCH_POWER),
        dispatch_cutoff_percent=_numeric_state(hass, DISPATCH_CUTOFF_SOC),
        dispatch_duration_minutes=_numeric_state(hass, DISPATCH_DURATION),
        dispatch_pv_enabled=_boolean_state(hass, DISPATCH_PV_SWITCH),
        dispatch_timer_active=dispatch_running,
        dispatch_timer_finishes_at=dispatch_finishes,
        conflicting_active=conflicting_families_active(hass),
    )


@callback
def steps_outside_capability(steps: tuple[CommandStep, ...]) -> tuple[str, ...]:
    """Return the entity ids this release may not write, in order.

    Built from :data:`CONTROL_EXECUTABLE_ACTIONS` rather than from a hardcoded
    family, so widening the barrier later widens this with it and cannot leave a
    stale interlock behind. The owner marker is always permitted: it is not a
    direction, it is how a direction becomes attributable, and releasing it is the
    one write that is safe without a claim.
    """
    permitted = {BOOLEAN_EXECUTION_OWNER}
    for action in CONTROL_EXECUTABLE_ACTIONS:
        family = FAMILIES.get(action)
        if family is not None:
            permitted.update(family.entities)
    # **The Dispatch surface is permitted by entity and constrained by value.**
    # Direction on this surface is a signed *number*, not a choice of entity, so
    # an entity subset test cannot express charge-only here at all -- that is
    # :func:`dispatch_refusal`'s job, and it is checked beside this one rather
    # than instead of it. Gated on the mode barrier so emptying that constant
    # closes this surface too, and cannot leave a stale interlock behind.
    if CONTROL_EXECUTABLE_DISPATCH_MODES:
        permitted.update(DISPATCH_ENTITIES)
    return tuple(step.entity_id for step in steps if step.entity_id not in permitted)


async def async_execute(hass: HomeAssistant, steps: tuple[CommandStep, ...]) -> int:
    """Send a planned command, and return how many steps were sent.

    **Never reached in this release.** The pipeline refuses first, and this
    refuses again: the barrier is checked here so that the only way to command an
    inverter is to change a constant in a source file, not to make a mistake in a
    call site.

    The order the steps arrive in is the order they are sent, and the planner
    guarantees the activation boolean is last -- so an interruption partway
    through leaves inert parameters rather than a half-formed command.

    **Two refusals, and the second is the one that matters in beta.24.** The first
    is the release barrier, unchanged. The second is a subset test on the entity
    ids: this release charges, so the only entities it may write are the charge
    family and the owner marker. It reads no action field and trusts no caller,
    which is why it survives a defect upstream -- a discharge that somehow reaches
    here names discharge entities, and naming them is enough to be refused.
    """
    if not CONTROL_EXECUTION_AVAILABLE:
        raise ControlExecutionUnavailable(
            "control execution is not available in this release; the whole "
            "pipeline is built and validated, but no command may reach the "
            "inverter until ownership and continuation are resolved"
        )
    outside = steps_outside_capability(steps)
    if outside:
        raise ControlActionNotPermitted(CONTROL_REFUSE_ACTION_NOT_EXECUTABLE, outside)
    # The value half of the same boundary: a positive dispatch power or a mode
    # outside the executable set, refused here whatever built the list.
    refusal = dispatch_refusal(steps)
    if refusal is not None:
        raise ControlActionNotPermitted(
            refusal, tuple(step.entity_id for step in steps)
        )

    sent = 0
    for step in steps:
        data: dict[str, Any] = {"entity_id": step.entity_id}
        if step.value is not None:
            data["value"] = step.value
        if step.option is not None:
            data["option"] = step.option
        await hass.services.async_call(step.domain, step.service, data, blocking=True)
        sent += 1
    _LOGGER.debug("Control command sent as %d steps", sent)
    return sent
