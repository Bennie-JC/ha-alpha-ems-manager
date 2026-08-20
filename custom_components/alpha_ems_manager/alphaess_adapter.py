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
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback

from .alphaess_device import (
    AUTOMATION_DISPATCH_RESET_FULL,
    AUTOMATION_HOLD_MONITOR,
    BOOLEAN_EXCESS_EXPORT,
    BOOLEAN_PEAK_SHAVING,
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    OWNERSHIP_PROVABLE,
    REQUIRED_ENTITIES,
    SENSOR_DISPATCH_ACTIVE_POWER,
    SENSOR_DISPATCH_MODE,
    SENSOR_DISPATCH_SOC,
    SENSOR_DISPATCH_START,
    SENSOR_DISPATCH_TIME,
    SENSOR_MAX_FEED_TO_GRID,
    CommandStep,
)
from .const import (
    CONTROL_EXECUTION_AVAILABLE,
    MAX_CONTROL_EVENTS_REPORTED,
)
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
            "owned": False,
            "ownership_provable": OWNERSHIP_PROVABLE,
            "ownership_note": (
                "nothing in the control surface records who armed a dispatch, so "
                "an active dispatch is treated as someone else's and is never "
                "modified or cancelled; matching parameters are not evidence"
            ),
        }


@callback
def _state_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return an entity's state string, or ``None`` when it does not exist."""
    state = hass.states.get(entity_id)
    return None if state is None else state.state


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
    return DeviceSnapshot(
        dispatch_active=bool(start) or bool(active_modes),
        dispatch_start=start,
        dispatch_mode=_numeric_state(hass, SENSOR_DISPATCH_MODE),
        dispatch_power_w=_numeric_state(hass, SENSOR_DISPATCH_ACTIVE_POWER),
        dispatch_soc_percent=_numeric_state(hass, SENSOR_DISPATCH_SOC),
        dispatch_time_s=_numeric_state(hass, SENSOR_DISPATCH_TIME),
        active_modes=active_modes,
    )


async def async_execute(hass: HomeAssistant, steps: tuple[CommandStep, ...]) -> int:
    """Send a planned command, and return how many steps were sent.

    **Never reached in this release.** The pipeline refuses first, and this
    refuses again: the barrier is checked here so that the only way to command an
    inverter is to change a constant in a source file, not to make a mistake in a
    call site.

    The order the steps arrive in is the order they are sent, and the planner
    guarantees the activation boolean is last -- so an interruption partway
    through leaves inert parameters rather than a half-formed command.
    """
    if not CONTROL_EXECUTION_AVAILABLE:
        raise ControlExecutionUnavailable(
            "control execution is not available in this release; the whole "
            "pipeline is built and validated, but no command may reach the "
            "inverter until ownership and continuation are resolved"
        )

    sent = 0
    for step in steps:
        data: dict[str, Any] = {"entity_id": step.entity_id}
        if step.value is not None:
            data["value"] = step.value
        await hass.services.async_call(step.domain, step.service, data, blocking=True)
        sent += 1
    _LOGGER.debug("Control command sent as %d steps", sent)
    return sent
