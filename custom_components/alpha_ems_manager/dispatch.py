"""The physical setpoint, as arithmetic. Pure, total, and the only place signs live.

**Why this module exists.** Until beta.25 a battery setpoint chosen at the top of a
quarter stood for the whole quarter, because writes only happen on a full refresh
and refreshes happen on quarter boundaries. Production and house load move
underneath it, so a fixed 1.3 kW charge caused both unintended import *and*
unintended export on the same afternoon. Following a **grid** target with live
measurements is the fix, and the arithmetic for it is here rather than in the
coordinator so it can be tested without a Home Assistant instance.

**Stage B decides how, never what.** Nothing here reads a price, a forecast or a
plan. It is handed a grid target Stage A already chose and a set of bounds, and it
returns the setpoint that pursues that target now. In particular it may **reduce**,
**hold** or **stop** -- it may never increase grid import to catch up a battery
target that has fallen behind. If more grid energy is economically required, Stage A
must publish a revised target.

The one canonical identity, and every sign in the release derives from it::

    grid = house - pv - dispatch

    grid > 0      importing            grid < 0      exporting
    dispatch > 0  battery discharging  dispatch < 0  battery charging

so, rearranged::

    required_dispatch_kw = house_load_kw - pv_kw - desired_grid_kw

**This is the opposite convention from the helper families**, which take a positive
magnitude and carry direction in *which family* was written. The raw Dispatch
surface is signed. Mixing the two would command a charge as a discharge, so the two
never meet: helper families are built in ``alphaess_device``, and signed dispatch
arithmetic exists only here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .alphaess_device import (
    DISPATCH_DEADMAN_MINUTES,
    DISPATCH_MODE_SOC_CONTROL,
)
from .const import (
    ACTION_CHARGE,
    CONTROL_EXECUTABLE_DISPATCH_MODES,
    CONTROL_EXECUTABLE_DISPATCH_SIGN,
    DISPATCH_LIMIT_DEADBAND,
    DISPATCH_LIMIT_DIRECTION_GATE,
    DISPATCH_LIMIT_DYNAMIC_RESERVE,
    DISPATCH_LIMIT_EXPORT_SAFETY,
    DISPATCH_LIMIT_GRID_LIMIT,
    DISPATCH_LIMIT_HEADROOM,
    DISPATCH_LIMIT_INVERTER_POWER,
    DISPATCH_LIMIT_MIN_SOC,
    DISPATCH_LIMIT_NONE,
    DISPATCH_LIMIT_QUANTISATION,
    DISPATCH_LIMIT_REMAINING_GRID_ENERGY,
    DISPATCH_POWER_DEADBAND_KW,
    DISPATCH_POWER_STEP_KW,
    TICK_APPLIED,
    TICK_SKIPPED_DEADBAND,
)

#: The clamp slots, in the contractual order, paired with the reason each reports.
#: A test ties this sequence to :data:`DISPATCH_CLAMP_ORDER`, so the order a reader
#: is promised and the order actually applied cannot drift apart.
_CLAMP_FIELDS: tuple[tuple[str, str], ...] = (
    ("inverter_kw", DISPATCH_LIMIT_INVERTER_POWER),
    ("min_soc_kw", DISPATCH_LIMIT_MIN_SOC),
    ("reserve_kw", DISPATCH_LIMIT_DYNAMIC_RESERVE),
    ("remaining_grid_kw", DISPATCH_LIMIT_REMAINING_GRID_ENERGY),
    ("headroom_kw", DISPATCH_LIMIT_HEADROOM),
    ("export_safety_kw", DISPATCH_LIMIT_EXPORT_SAFETY),
    ("grid_limit_kw", DISPATCH_LIMIT_GRID_LIMIT),
)


def required_dispatch_kw(
    *, house_load_kw: float, pv_kw: float, desired_grid_kw: float
) -> float:
    """Return the signed battery power that would put the meter on target.

    The canonical identity, rearranged, and the **only** place it appears. Negative
    charges, positive discharges.

    A sign crossing inside one quarter is an ordinary result, not a new economic
    decision: with the grid target fixed at +0.5 kW, a cloud passing takes a 2.6 kW
    house and 2.9 kW of production from a -0.8 kW charge to a +0.6 kW discharge
    without Stage A having said anything. Whether that crossing may be *commanded*
    is a separate question, answered by the direction gate.
    """
    return house_load_kw - pv_kw - desired_grid_kw


def achievable_grid_kw(
    *, house_load_kw: float, pv_kw: float, applied_kw: float
) -> float:
    """Return the meter reading the *applied* setpoint actually produces.

    The same identity forwards, from the final clamped figure -- so a target the
    controller cannot reach is visible rather than implied. A plan wanting +0.5 kW
    that computes -7.0 and can only apply -3.0 achieves +4.5, and saying so is the
    difference between a diagnosable clamp and a mystery.
    """
    return house_load_kw - pv_kw - applied_kw


def quantise_kw(power_kw: float, step_kw: float = DISPATCH_POWER_STEP_KW) -> float:
    """Return the nearest commandable power **no larger in magnitude**.

    Toward zero for either sign, which is uniformly the conservative direction: a
    charge commanded slightly smaller buys slightly less, and a discharge commanded
    slightly smaller delivers slightly less. Rounding outward would over-deliver in
    both directions.

    Floats make this less obvious than it looks -- ``0.3 / 0.1`` is
    ``2.9999999999999996`` -- so the division is nudged before flooring and the
    result is checked against the promise and walked back if the nudge overshot.
    """
    if step_kw <= 0.0:  # pragma: no cover - defensive
        return power_kw
    magnitude = abs(power_kw)
    steps = math.floor(magnitude / step_kw + 1e-9)
    quantised = steps * step_kw
    # **The walk-back needs the same tolerance the nudge did**, or it undoes it.
    # ``3 * 0.1`` is ``0.30000000000000004``, which is genuinely greater than
    # ``0.3`` -- so a bare ``>`` here dropped every exact multiple back a whole
    # step, turning a commanded 2.3 kW into 2.2 and a 0.8 into 0.7.
    if quantised > magnitude + 1e-9:  # pragma: no cover - measured never to fire
        quantised = (steps - 1) * step_kw
    return math.copysign(quantised, power_kw) if quantised else 0.0


@dataclass(frozen=True, slots=True)
class ChargeLimits:
    """Every bound Stage B may place on a charge, as positive kW magnitudes.

    ``None`` means *unconstrained* and never zero -- the distinction matters,
    because a missing reading must not silently become a prohibition or a licence.

    **Every field can only reduce.** None of them may raise the setpoint, and none
    of them may increase grid import: a battery target that has fallen behind lets
    Stage B reduce or stop, never catch up.

    :attr:`remaining_grid_kw` is clamp four and is the **grid** authorisation, not
    the battery remainder. That is the correction that matters most in this class:
    ``battery_target_kwh`` is ``expected_pv_to_battery_kwh +
    expected_grid_to_battery_kwh``, a forecast composite, so bounding a grid-power
    controller with it stops absorption the moment production runs ahead of
    forecast and pushes free photovoltaic energy out to the meter at an export
    price the optimizer had already judged worse than storing it. The pack is
    bounded by :attr:`headroom_kw` and :attr:`reserve_kw`, which are different
    questions and are asked separately.
    """

    inverter_kw: float | None = None
    min_soc_kw: float | None = None
    reserve_kw: float | None = None
    remaining_grid_kw: float | None = None
    headroom_kw: float | None = None
    export_safety_kw: float | None = None
    grid_limit_kw: float | None = None


def clamp_charge_kw(magnitude_kw: float, limits: ChargeLimits) -> tuple[float, str]:
    """Return the permitted charge magnitude and the bound that **binds** it.

    The reported reason names the constraint the final figure actually came from,
    not the first one encountered on the way down. Those differ whenever two
    bounds both bite -- a 6.2 kW request under a 5.0 kW grid budget and 4.0 kW of
    headroom is limited by *headroom*, and saying "grid budget" because it was
    checked earlier would send a reader to the wrong constraint.

    :data:`DISPATCH_CLAMP_ORDER` still decides ties, so a reader is told the
    same thing twice rather than something arbitrary.
    """
    applied = max(0.0, magnitude_kw)
    reason = DISPATCH_LIMIT_NONE
    for field, limit_reason in _CLAMP_FIELDS:
        bound = getattr(limits, field)
        if bound is None:
            continue
        bound = max(0.0, bound)
        if bound < applied - 1e-9:
            applied = bound
            reason = limit_reason
    return applied, reason


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """One physical tick, decided and explained.

    Every figure a reader needs to reconstruct the tick without rerunning it, and
    the intermediate steps are kept apart on purpose: ``calculated`` before the
    clamps and ``applied`` after them is what makes a clamp visible.
    """

    desired_grid_kw: float
    house_load_kw: float
    pv_kw: float
    required_kw: float
    calculated_kw: float
    applied_kw: float
    achievable_grid_kw: float
    limited_by: str
    update_needed: bool
    update_reason: str

    def as_dict(self) -> dict[str, float | bool | str]:
        """Return the bounded diagnostics form."""
        return {
            "desired_grid_kw": round(self.desired_grid_kw, 3),
            "house_load_kw": round(self.house_load_kw, 3),
            "pv_kw": round(self.pv_kw, 3),
            "required_dispatch_kw": round(self.required_kw, 3),
            "calculated_dispatch_kw": round(self.calculated_kw, 3),
            "applied_dispatch_kw": round(self.applied_kw, 3),
            "achievable_grid_kw": round(self.achievable_grid_kw, 3),
            "dispatch_limited_by": self.limited_by,
            "update_needed": self.update_needed,
            "update_reason": self.update_reason,
        }


def crosses_zero(last_kw: float, candidate_kw: float, deadband_kw: float) -> bool:
    """Return whether ``candidate_kw`` may take the setpoint across zero.

    **The deadband alone is not enough near zero.** A setpoint sitting at -0.1 kW
    with the deadband at 0.2 would still be free to oscillate to +0.1 and back on
    sensor noise, because each hop is within the band from the other side. So once
    the applied setpoint has a sign, reversing it requires the new value to clear
    the deadband *on the far side of zero*.

    Noise cannot flip the sign; a real reversal can. Returns ``True`` when the
    crossing is permitted, which includes the case where there is no sign to
    reverse.
    """
    if last_kw == 0.0 or candidate_kw == 0.0:
        return True
    if (last_kw > 0.0) == (candidate_kw > 0.0):
        return True
    return abs(candidate_kw) >= deadband_kw


def decide(
    *,
    desired_grid_kw: float,
    house_load_kw: float,
    pv_kw: float,
    limits: ChargeLimits,
    last_applied_kw: float | None,
    charge_only: bool = True,
    deadband_kw: float = DISPATCH_POWER_DEADBAND_KW,
) -> DispatchDecision:
    """Return the setpoint this tick should command, and whether to write it.

    The whole of Stage B physical control, in one pure function:

    1. the canonical identity gives the required signed dispatch;
    2. the direction gate refuses a sign this release cannot execute;
    3. the clamps reduce the magnitude, never raise it;
    4. the figure is quantised to the device step;
    5. the deadband and the zero-crossing hysteresis decide whether to write.

    ``charge_only`` is the beta.25 envelope. When set, a required *discharge* is
    clamped to zero rather than commanded -- and zero is still a legitimate
    setpoint, because holding the battery still is exactly what "do not export" and
    "do not discharge" mean physically. Refusing to write zero would leave the last
    charge running into the reversal it was told to stop.
    """
    required = required_dispatch_kw(
        house_load_kw=house_load_kw, pv_kw=pv_kw, desired_grid_kw=desired_grid_kw
    )

    calculated = required
    reason = DISPATCH_LIMIT_NONE
    if charge_only and required > 0.0:
        # A discharge this release may not command. Held at zero, not inverted.
        calculated = 0.0
        reason = DISPATCH_LIMIT_DIRECTION_GATE
        applied = 0.0
    else:
        magnitude, reason = clamp_charge_kw(abs(required), limits)
        applied = -magnitude if required < 0.0 else magnitude

    quantised = quantise_kw(applied)
    if reason == DISPATCH_LIMIT_NONE and abs(quantised - applied) > 1e-9:
        # Only when nothing else reduced it. Quantisation always shaves something,
        # so reporting it above a real constraint would bury the real constraint.
        reason = DISPATCH_LIMIT_QUANTISATION
    applied = quantised

    # **The write decision, and it owns the reason when it holds.** If the applied
    # figure is the *previous* setpoint rather than the calculated one, the reason
    # the two differ is the deadband -- not whatever clamp shaped a value that was
    # then not sent. Letting quantisation win here reported ``quantisation`` for
    # every held tick, which is true of every tick and therefore says nothing.
    held = quantise_kw(last_applied_kw) if last_applied_kw is not None else None
    if held is None:
        update_needed, update_reason = True, TICK_APPLIED
    elif not crosses_zero(last_applied_kw or 0.0, applied, deadband_kw):
        update_needed, update_reason = False, TICK_SKIPPED_DEADBAND
        applied, reason = held, DISPATCH_LIMIT_DEADBAND
    elif abs(applied - held) >= deadband_kw:
        update_needed, update_reason = True, TICK_APPLIED
    else:
        update_needed, update_reason = False, TICK_SKIPPED_DEADBAND
        applied, reason = held, DISPATCH_LIMIT_DEADBAND
    limited_by = reason

    return DispatchDecision(
        desired_grid_kw=desired_grid_kw,
        house_load_kw=house_load_kw,
        pv_kw=pv_kw,
        required_kw=required,
        calculated_kw=calculated,
        applied_kw=applied,
        achievable_grid_kw=achievable_grid_kw(
            house_load_kw=house_load_kw, pv_kw=pv_kw, applied_kw=applied
        ),
        limited_by=limited_by,
        update_needed=update_needed,
        update_reason=update_reason,
    )


@dataclass(frozen=True, slots=True)
class ModeChoice:
    """Which dispatch mode an economic action becomes, and why."""

    mode: int | None
    reason: str
    #: Whether this release may physically command it. Modelled and reported
    #: either way -- a gated mode is still planned, explained and tested.
    executable: bool


def mode_for(action: str | None, *, signed_power_kw: float) -> ModeChoice:
    """Return the dispatch mode for an economic action. Pure, and price-blind.

    **Mode selection happens after Stage A has decided what to do.** Nothing here
    reads a price, and in particular nothing reads the *sign* of a price: a rule
    shaped "if the price is negative then pick mode N" would be an economic
    decision taken in the execution layer, which is the one thing Stage B may not
    do. The action arrives already chosen; this only maps it onto the surface.

    Mode 2 is the only controllable kW primitive on offer. Modes 6 and 7 are
    modelled and reported but are **not** rate primitives at all: the package
    writes the power register as a bare ``32000`` -- zero watts -- for any mode
    outside 1, 2, 3 and 5, so commanding a kW figure in mode 7 commands nothing.
    That corrects the assumption that mode 7 is a throttled consumption mode.
    """
    if action is None:
        return ModeChoice(None, "no_action", False)
    if action != ACTION_CHARGE:
        # Discharge, export and curtailment are planned and explained; none is
        # executable in beta.25, and the mode they would need is not selected
        # here so that a defect upstream cannot smuggle one through.
        return ModeChoice(None, f"{action}_not_executable", False)
    if signed_power_kw >= 0.0:
        # A charge must be negative on this surface. A non-negative figure is
        # either a rounding artefact or a sign error, and neither may be sent.
        return ModeChoice(
            DISPATCH_MODE_SOC_CONTROL, "charge_requires_negative_power", False
        )
    return ModeChoice(
        DISPATCH_MODE_SOC_CONTROL,
        "state_of_charge_control_negative_power",
        DISPATCH_MODE_SOC_CONTROL in CONTROL_EXECUTABLE_DISPATCH_MODES
        and CONTROL_EXECUTABLE_DISPATCH_SIGN < 0,
    )


def deadman_minutes(previous: float | None) -> int:
    """Return the duration to write so the vendor automation actually fires.

    **The alternation is a workaround for a state-change trigger, not a policy.**
    ``AlphaESS_Update_Dispatch_Duration`` triggers on the ``input_number``
    changing state, so writing the same value re-arms nothing and the run would
    expire silently mid-charge. There is no cleaner path: the package exposes no
    ``script:`` section and no service or event that re-triggers it.

    So the written figure alternates between the two values in
    :data:`DISPATCH_DEADMAN_MINUTES`, both on the helper five-minute step. The
    semantic dead-man stays at about twenty minutes: the twenty-five is not a
    longer run, not more energy and not a different planning horizon, and nothing
    downstream may read it as one.
    """
    low, high = DISPATCH_DEADMAN_MINUTES
    if previous is not None and abs(previous - float(low)) < 0.5:
        return high
    return low
