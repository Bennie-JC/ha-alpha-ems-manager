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
from dataclasses import dataclass, replace

from .alphaess_device import (
    DISPATCH_DEADMAN_MINUTES,
    DISPATCH_MODE_SOC_CONTROL,
)
from .const import (
    ACTION_CHARGE,
    CONTROL_EXECUTABLE_DISPATCH_MODES,
    CONTROL_EXECUTABLE_DISPATCH_SIGNS,
    CONTROL_TICK_ENERGY_HORIZON_SECONDS,
    DISPATCH_LIMIT_DEADBAND,
    DISPATCH_LIMIT_DIRECTION_GATE,
    DISPATCH_LIMIT_DYNAMIC_RESERVE,
    DISPATCH_LIMIT_EXPORT_SAFETY,
    DISPATCH_LIMIT_FREE_PV_ABSORPTION,
    DISPATCH_LIMIT_GRID_LIMIT,
    DISPATCH_LIMIT_HEADROOM,
    DISPATCH_LIMIT_INVERTER_POWER,
    DISPATCH_LIMIT_MAX_DISCHARGE,
    DISPATCH_LIMIT_MIN_SOC,
    DISPATCH_LIMIT_NONE,
    DISPATCH_LIMIT_QUANTISATION,
    DISPATCH_LIMIT_REMAINING_DISCHARGE,
    DISPATCH_LIMIT_REMAINING_EXPORT,
    DISPATCH_LIMIT_REMAINING_GRID_ENERGY,
    DISPATCH_LIMIT_TICK_HORIZON,
    DISPATCH_POWER_DEADBAND_KW,
    DISPATCH_POWER_STEP_KW,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
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
        and permitted_sign(EXECUTION_INTENT_GRID_CHARGE) == -1,
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


# ===========================================================================
# beta.27: the intent-keyed direction gate
# ===========================================================================


def permitted_sign(intent: str | None) -> int | None:
    """Return the signed direction ``intent`` may command, or ``None``.

    **Keyed on the intent, because beta.27 executes two directions.** A single
    scalar could only ever describe one of them, and an intent this release has not
    validated has no entry -- so ``serve_load``, the negative-price modes and every
    unverified direction stay blocked without needing to be listed.

    ``None`` for an unknown or missing intent, which every caller must treat as a
    refusal rather than as "no constraint".
    """
    if intent is None:
        return None
    return CONTROL_EXECUTABLE_DISPATCH_SIGNS.get(intent)


def sign_matches_intent(intent: str | None, power_kw: float) -> bool:
    """Return whether a signed power is the direction ``intent`` is allowed.

    Zero matches any **known** intent: it is what the direction gate produces when
    the target would require a direction this release cannot command, and commanding
    nothing is never the wrong direction.

    An **unknown or missing** intent matches nothing at all, zero included, because
    the question this function answers is "may this be commanded under this
    authority?" and there is no authority. Deliberately stricter than
    :func:`alphaess_device.dispatch_refusal`, which does permit a zero power with no
    intent -- it has to, because the cleanup writes zero precisely when the run that
    authorised it is over.
    """
    sign = permitted_sign(intent)
    if sign is None:
        return False
    if power_kw == 0.0:
        return True
    return (power_kw < 0.0) == (sign < 0)


# ===========================================================================
# beta.27: quarter progress, and the asymmetric objectives
# ===========================================================================


def hours_remaining(seconds_remaining: float) -> float:
    """Return the remaining fraction of an hour, floored at one tick horizon.

    **Floored rather than allowed to reach zero**, and not only to avoid dividing by
    it: as a quarter closes, ``remaining / remaining_time`` diverges, and a rate
    computed from three remaining seconds describes a physical impossibility. The
    floor makes the requested rate converge on what one control interval could
    actually deliver, which is the same figure :func:`tick_energy_cap_kw` enforces.
    """
    floor = CONTROL_TICK_ENERGY_HORIZON_SECONDS / 3600.0
    return max(floor, max(0.0, seconds_remaining) / 3600.0)


def tick_energy_cap_kw(remaining_kwh: float) -> float:
    """Return the most power one control interval may be asked to deliver.

    **The overshoot guard, and it is deliberately conservative.** Target-reached is
    detected *after* measurements arrive, which is one tick too late to prevent an
    overshoot -- so the request is bounded in advance by what a single interval
    could deliver against the energy that is actually left.

    The horizon is longer than the cadence it guards, because the tick is
    "approximately" sixty seconds, a readback lands after the write, and a tick can
    be skipped for lock contention. The cost is that a target may finish a few
    watt-hours short near a quarter boundary; the alternative is spending energy
    Stage A never authorised. The shortfall is recorded and never carried forward.
    """
    if remaining_kwh <= 0.0:
        return 0.0
    return remaining_kwh / (CONTROL_TICK_ENERGY_HORIZON_SECONDS / 3600.0)


def export_rate_to_battery_kw(
    *, house_load_kw: float, pv_kw: float, export_kw: float
) -> float:
    """Return the battery discharge that produces ``export_kw`` at the meter.

    **The one conversion point between the two domains.** A meter-side export
    energy and a battery-side discharge energy are different quantities and are
    never compared; they are converted, here, through the canonical identity:

        grid = house - pv - dispatch,  so  dispatch = house - pv + export

    House consumption is supplied *in addition* to the export, and production
    reduces the discharge required. Commanding the export magnitude directly as
    battery power -- the likeliest error in this release -- would under-export by
    exactly the house load.
    """
    return house_load_kw - pv_kw + max(0.0, export_kw)


def battery_rate_to_export_kw(
    *, house_load_kw: float, pv_kw: float, battery_kw: float
) -> float:
    """Return the meter export a battery discharge of ``battery_kw`` produces.

    The inverse of :func:`export_rate_to_battery_kw`, through the same identity, so
    a battery-side ceiling can be expressed as the export it permits.
    """
    return max(0.0, battery_kw) - house_load_kw + pv_kw


@dataclass(frozen=True, slots=True)
class QuarterProgress:
    """What remains of one admitted quarter, in both domains, with time left.

    Both domains are carried because neither answers the other's question, and the
    asymmetry between them is the whole of the beta.27 contract: for a charge the
    battery figure is the objective and the grid figure a ceiling, and for an export
    it is the other way round.
    """

    seconds_remaining: float
    #: Battery-side AC energy still to deliver. Objective for a charge, ceiling for
    #: an export.
    #:
    #: **Objective-attributed, since beta.40.** Free production stored above the
    #: objective does not reduce it, because the objective is what the row promised
    #: and absorbing more than that is not progress against a smaller promise.
    battery_remaining_kwh: float
    #: Grid-side energy still authorised. Ceiling for a charge (import it may
    #: cause), objective for an export (meter export it must realise).
    grid_remaining_kwh: float
    #: Whether Stage A authorised **keeping** measured free production in this
    #: row rather than exporting it. beta.40.
    #:
    #: A verdict, because the magnitude is physical and :func:`clamp_charge_kw`
    #: already owns every physical bound. ``False`` is the inert value and is what
    #: every pre-beta.40 plan yields, so a :class:`QuarterProgress` built without
    #: it behaves exactly as beta.39 did.
    retention_authorised: bool = False

    @property
    def hours(self) -> float:
        """Return the remaining time as hours, floored at one tick horizon."""
        return hours_remaining(self.seconds_remaining)

    @property
    def battery_rate_kw(self) -> float:
        """Return the average battery power that would finish on time."""
        return max(0.0, self.battery_remaining_kwh) / self.hours

    @property
    def grid_rate_kw(self) -> float:
        """Return the average grid rate the remaining authorisation permits."""
        return max(0.0, self.grid_remaining_kwh) / self.hours


def decide_charge(
    *,
    progress: QuarterProgress,
    house_load_kw: float,
    pv_kw: float,
    limits: ChargeLimits,
    last_applied_kw: float | None,
    deadband_kw: float = DISPATCH_POWER_DEADBAND_KW,
) -> DispatchDecision:
    """Return the charge setpoint for this tick. **Battery objective, grid ceiling.**

    The contract this implements is the one already written into the publication:
    ``battery_target_kwh`` is *"Battery side. Authoritative for a charge"*, and
    ``grid_target_kwh`` is *"Present only when the meter is what the plan is aiming
    at"* -- and it is ``None`` for a charge. So the battery figure is what Stage B
    tries to realise, and the grid figure is only a ceiling on how much of it may be
    bought.

    Four consequences, each a requirement rather than a side effect:

    * **Production substitutes for planned grid energy.** A larger photovoltaic
      surplus meets the same battery objective with less grid, so unspent
      authorisation is never a deficit to consume. Treating it as a target buys
      energy the plan did not need -- measured at roughly a kilowatt-hour on a
      quarter where production outperformed.
    * **Missing production cannot unlock extra buying.** The ceiling comes from the
      authorisation, never from the battery deficit.
    * **Behind schedule still speeds up**, up to ``pv_surplus + grid_rate_cap``.
    * **Free production is still absorbed once the grid budget is spent**: the cap
      falls to the surplus alone, and the charge continues under the battery,
      headroom and reserve limits rather than pushing production to the meter.

    **beta.36 makes that fourth promise true.** It was not, and a hardware
    measurement is what exposed it. The grid authorisation was applied *twice*: once
    correctly, added to the production surplus below, and again as a bare entry in
    :data:`_CLAMP_FIELDS` bounding the **battery** power. So with the grid budget
    spent, the second application pulled the battery to zero however much free
    production was standing there.

    Measured on the reference inverter on 2026-08-31: an unfinished 0.56 kWh charge
    row, grid budget 98 % spent, PV 2.8 kW against a 1.5 kW house -- 1.3 kW of free
    surplus. ``applied_kw`` came out ``0.000`` and this function's own
    ``desired_grid_kw`` came out **-1.240**: the controller was arithmetically
    predicting that it would export 1.24 kW of production it had been asked to
    store. The same row now commands the surplus and imports ``0.060 kW`` -- exactly
    the authorisation that remained, and not a watt more.

    **The ceiling is not weakened; it is honoured in its own domain.** A charge at
    the production surplus causes *no import at all*
    (``desired_grid_kw = house - pv + applied`` is zero when ``applied`` is the
    surplus), and the attribution in ``_accrue_quarter_progress`` spends production
    first, so a PV-sourced charge never consumes grid authorisation it did not use.
    The run-level authorisation and its downward revision are kept, folded into the
    grid term below where they belong, so nothing that bounded grid import stops
    bounding it.

    **What this explains, and what it does not.** A charge command of zero becomes a
    ``None`` intent, which ``safety`` reports as unsafe by construction, and an unsafe
    verdict on an owned live dispatch was promoted to an unsuppressable abort -- so
    this path can reach the 2026-08-31 failure. Whether it *did* is **not determinable
    from that capture**: ``remaining_grid_energy`` is published by both applications
    of the authorisation, so the token cannot tell them apart, and that day's rows
    charged substantially rather than sitting at zero. The defect is real, is proven
    arithmetically in ``test_beta36_charge_domains.py``, and is fixed on its own
    merits. No claim is made that it caused that abort.

    **The economics never changed; the ceiling was simply being charged to the wrong
    meter.**

    **beta.40 adds the second domain, and it is the first term here that may raise a
    command.** beta.36 let production *substitute* for planned grid energy inside the
    row objective; it still could not let production *exceed* it, because
    ``applied_kw`` is seeded from the objective and every line after it only reduces.
    So a row sized to a forecast surplus caps the live response to forecast error.

    Measured on the reference inverter on 2026-09-03, mid-campaign, owned and
    executing: PV 3.309 kW against a 0.792 kW house -- 2.517 kW of free surplus --
    with 12.61 kWh of pack headroom and 8.527 kWh of grid authorisation still
    unspent. The row's objective had 0.037 kWh left over a floored 0.025 h, so
    ``battery_rate_kw`` was 1.490 kW, ``battery_cap_kw`` was 2.517 and never bound,
    and this function's own ``desired_grid_kw`` came out **-1.027**: it predicted
    exporting a kilowatt of production the plan had already decided was worth
    storing. The meter measured 0.942 kW going out.

    ``retention_authorised`` is Stage A's answer to that, frozen onto the row before
    opened. Same row, same objective, same authorisation: ``absorb_kw`` is 2.517,
    ``applied_kw`` becomes 2.500 after quantisation and ``desired_grid_kw`` becomes
    **-0.017**. Nothing was bought -- see the proof beside the branch.
    """
    pv_surplus_kw = max(0.0, pv_kw - house_load_kw)
    required_battery_kw = progress.battery_rate_kw

    # **One grid domain, and both bounds live in it.** The row's own remaining
    # authorisation, and the run-level remainder carrying the downward revision,
    # whichever binds harder. Taking the tighter of the two rates keeps every bound
    # the clamp used to apply -- it only stops them being charged against the
    # battery, which is a different quantity that free production also feeds.
    grid_rate_cap_kw = progress.grid_rate_kw
    if limits.remaining_grid_kw is not None:
        grid_rate_cap_kw = min(grid_rate_cap_kw, max(0.0, limits.remaining_grid_kw))

    applied_kw = required_battery_kw
    reason = DISPATCH_LIMIT_NONE

    # The grid authorisation, as a power ceiling on the battery.
    battery_cap_kw = pv_surplus_kw + grid_rate_cap_kw
    if battery_cap_kw < applied_kw - 1e-9:
        applied_kw = battery_cap_kw
        reason = DISPATCH_LIMIT_REMAINING_GRID_ENERGY

    # **beta.40, the second domain: free production, and only free production.**
    #
    # Everything above resolves the row's *objective* and is unchanged. This is
    # the one term in this function that may **raise** a command, and it is
    # bounded by the measured surplus alone -- never by the grid authorisation,
    # which is the beta.36 invariant read the other way round: a ceiling on
    # buying is not a ceiling on storing something nobody bought.
    #
    # It is a ``max`` and never a ``min``. The objective may legitimately exceed
    # the surplus because the objective may be grid-fed; applying this as a
    # ``min`` would cap total battery power by a free-production figure and
    # re-break beta.36 in mirror image.
    #
    # **How much is not decided here.** Stage A says only whether the tariff
    # prefers keeping this energy to selling it; inverter power and pack headroom
    # bound the result through :func:`clamp_charge_kw` below, exactly as they
    # bound every other charge this function commands. One clamp, one copy.
    #
    # **Why it cannot buy a watt**, for every input rather than for one capture:
    #
    #     delta = max(objective, surplus) - objective = max(0, surplus - objective)
    #           <= surplus = pv - house
    #
    # so when this branch is what bound, ``applied == surplus <= pv - house`` and
    #
    #     desired_grid = house - pv + applied <= house - pv + (pv - house) = 0
    #
    # -- export or zero, never import. And an unauthorised row leaves
    # ``absorb_kw`` at zero, so it is bit-for-bit beta.39.
    absorb_kw = pv_surplus_kw if progress.retention_authorised else 0.0
    absorbing = absorb_kw > applied_kw + 1e-9
    if absorbing:
        applied_kw = absorb_kw
        reason = DISPATCH_LIMIT_FREE_PV_ABSORPTION

    # **The physical clamps, with the grid authorisation withheld from them.**
    # Unchanged in meaning and order otherwise, and the reported vocabulary is
    # unchanged too: the branch above still names ``remaining_grid_energy`` when the
    # authorisation is what binds, so no published token moves.
    #
    # ``decide_export`` and the run-level ``decide_setpoint`` fallback are untouched:
    # neither reaches this line, and the beta.26 arithmetic the hardware accepted
    # keeps applying the clamp exactly as before.
    clamped_kw, clamp_reason = clamp_charge_kw(
        applied_kw, replace(limits, remaining_grid_kw=None)
    )
    if clamp_reason != DISPATCH_LIMIT_NONE:
        applied_kw, reason = clamped_kw, clamp_reason
    else:
        applied_kw = clamped_kw

    # **The overshoot guard, against whichever remainder this tick is spending.**
    #
    # The guard's job is "never ask for more than the energy that is actually left",
    # and against the objective remainder alone it would clamp an absorbing tick to
    # nothing the moment the objective was met -- reintroducing the exported
    # production one line after removing it. A tick storing free production is not
    # spending the objective's remainder at all, and the energy it *is* spending is
    # bounded by the pack, which ``headroom_kw`` above has already applied.
    #
    # **Keyed on ``absorbing`` and not on ``reason``**, because a clamp overwrites
    # ``reason``. Reading the token here meant a *clamped* absorbing tick lost the
    # widened guard and was then zeroed by it: 2.5 kW of surplus under a 1.0 kW
    # inverter limit came out at 1.0 and then at 0.0, with ``tick_energy_horizon``
    # printed beside it. Caught by the beta.40 Gate 1 sweep.
    spendable_kwh = progress.battery_remaining_kwh
    if absorbing:
        spendable_kwh = max(spendable_kwh, applied_kw * progress.hours)
    tick_cap_kw = tick_energy_cap_kw(spendable_kwh)
    if tick_cap_kw < applied_kw - 1e-9:
        applied_kw, reason = tick_cap_kw, DISPATCH_LIMIT_TICK_HORIZON

    return _finish(
        desired_grid_kw=house_load_kw - pv_kw + applied_kw,
        house_load_kw=house_load_kw,
        pv_kw=pv_kw,
        required_kw=-required_battery_kw,
        calculated_kw=-required_battery_kw,
        signed_kw=-applied_kw,
        reason=reason,
        last_applied_kw=last_applied_kw,
        deadband_kw=deadband_kw,
    )


def decide_export(
    *,
    progress: QuarterProgress,
    house_load_kw: float,
    pv_kw: float,
    max_discharge_kw: float | None = None,
    reserve_headroom_kwh: float | None = None,
    grid_export_limit_kw: float | None = None,
    last_applied_kw: float | None,
    deadband_kw: float = DISPATCH_POWER_DEADBAND_KW,
) -> DispatchDecision:
    """Return the export setpoint for this tick. **Meter objective, battery ceiling.**

    The mirror image of :func:`decide_charge`, and the asymmetry is the contract's:
    ``grid_target_kwh`` is *"Meter side. Present only when the meter is what the
    plan is aiming at"*, which for an export it is -- that is where the money is
    measured. So the meter figure is the objective and the battery discharge
    authorisation is the ceiling.

    Clamps are applied in :data:`DISPATCH_EXPORT_CLAMP_ORDER`. Battery-side and
    meter-side bounds are **converted** through :func:`export_rate_to_battery_kw`,
    never compared.
    """
    export_rate_kw = progress.grid_rate_kw
    # **No authorised export left means no discharge at all.** Not the discharge the
    # identity would give: with the meter target spent, ``house - pv + 0`` is the
    # power that would hold the meter at zero by supplying the house from the pack.
    # That is ``serve_load``, which this release does not execute and which has no
    # published meter target to be measured against -- so producing it here would
    # execute a blocked intent under an export authorisation, on the tick path,
    # every time an export finished early.
    if export_rate_kw <= 0.0:
        return _finish(
            desired_grid_kw=house_load_kw - pv_kw,
            house_load_kw=house_load_kw,
            pv_kw=pv_kw,
            required_kw=0.0,
            calculated_kw=0.0,
            signed_kw=0.0,
            reason=DISPATCH_LIMIT_REMAINING_EXPORT,
            last_applied_kw=last_applied_kw,
            deadband_kw=deadband_kw,
        )
    required_dispatch_kw = export_rate_to_battery_kw(
        house_load_kw=house_load_kw, pv_kw=pv_kw, export_kw=export_rate_kw
    )
    applied_kw = max(0.0, required_dispatch_kw)
    reason = DISPATCH_LIMIT_NONE

    def bind(cap_kw: float | None, why: str) -> None:
        nonlocal applied_kw, reason
        if cap_kw is None:
            return
        cap_kw = max(0.0, cap_kw)
        if cap_kw < applied_kw - 1e-9:
            applied_kw, reason = cap_kw, why

    # 1. inverter discharge limit.
    bind(max_discharge_kw, DISPATCH_LIMIT_MAX_DISCHARGE)
    # 2-3. the reserve, expressed as the discharge the remaining headroom permits.
    #      A profitable export never unlocks a reserve violation.
    if reserve_headroom_kwh is not None:
        bind(
            max(0.0, reserve_headroom_kwh) / progress.hours,
            DISPATCH_LIMIT_DYNAMIC_RESERVE,
        )
    # 4. the authorised battery discharge for this quarter.
    bind(progress.battery_rate_kw, DISPATCH_LIMIT_REMAINING_DISCHARGE)
    # 5. the authorised meter export **is the objective on this path**, so there is
    #    no separate clamp for it: ``required_dispatch_kw`` was derived from
    #    ``progress.grid_rate_kw``, which is exactly the remaining authorised export
    #    over the remaining time. A ``bind`` against the same figure could never
    #    fire, and a clamp that cannot fire is worse than none -- it reads as
    #    enforcement. :data:`DISPATCH_LIMIT_REMAINING_EXPORT` still names the
    #    quantity, and clamp 6 below is what actually bounds the meter domain.
    # 6. the overshoot guard, against whichever domain binds sooner.
    bind(
        tick_energy_cap_kw(progress.battery_remaining_kwh),
        DISPATCH_LIMIT_TICK_HORIZON,
    )
    bind(
        export_rate_to_battery_kw(
            house_load_kw=house_load_kw,
            pv_kw=pv_kw,
            export_kw=tick_energy_cap_kw(progress.grid_remaining_kwh),
        ),
        DISPATCH_LIMIT_REMAINING_EXPORT,
    )
    # 7. a known site or grid export limit, converted the same way.
    if grid_export_limit_kw is not None:
        bind(
            export_rate_to_battery_kw(
                house_load_kw=house_load_kw, pv_kw=pv_kw, export_kw=grid_export_limit_kw
            ),
            DISPATCH_LIMIT_GRID_LIMIT,
        )

    return _finish(
        desired_grid_kw=-export_rate_kw,
        house_load_kw=house_load_kw,
        pv_kw=pv_kw,
        required_kw=required_dispatch_kw,
        calculated_kw=required_dispatch_kw,
        signed_kw=applied_kw,
        reason=reason,
        last_applied_kw=last_applied_kw,
        deadband_kw=deadband_kw,
    )


def _finish(
    *,
    desired_grid_kw: float,
    house_load_kw: float,
    pv_kw: float,
    required_kw: float,
    calculated_kw: float,
    signed_kw: float,
    reason: str,
    last_applied_kw: float | None,
    deadband_kw: float,
) -> DispatchDecision:
    """Quantise, apply the deadband and hysteresis, and assemble the decision.

    Shared by both directions so the write decision cannot differ between them --
    the deadband, the zero-crossing rule and the reported reason are one
    implementation, exercised twice.
    """
    applied = quantise_kw(signed_kw)
    if reason == DISPATCH_LIMIT_NONE and abs(applied - signed_kw) > 1e-9:
        reason = DISPATCH_LIMIT_QUANTISATION

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

    return DispatchDecision(
        desired_grid_kw=desired_grid_kw,
        house_load_kw=house_load_kw,
        pv_kw=pv_kw,
        required_kw=required_kw,
        calculated_kw=calculated_kw,
        applied_kw=applied,
        achievable_grid_kw=achievable_grid_kw(
            house_load_kw=house_load_kw, pv_kw=pv_kw, applied_kw=applied
        ),
        limited_by=reason,
        update_needed=update_needed,
        update_reason=update_reason,
    )


def decide_for_intent(
    *,
    intent: str,
    progress: QuarterProgress,
    house_load_kw: float,
    pv_kw: float,
    limits: ChargeLimits,
    last_applied_kw: float | None,
    max_discharge_kw: float | None = None,
    reserve_headroom_kwh: float | None = None,
    grid_export_limit_kw: float | None = None,
    deadband_kw: float = DISPATCH_POWER_DEADBAND_KW,
) -> DispatchDecision | None:
    """Return the setpoint for an admitted intent, or ``None`` if it has none.

    The one place the two objectives are selected between, so no caller has to know
    which domain an intent's target lives in. ``None`` for anything this release
    does not execute -- a refusal, never a zero setpoint that looks like a decision.
    """
    if intent == EXECUTION_INTENT_GRID_CHARGE:
        return decide_charge(
            progress=progress,
            house_load_kw=house_load_kw,
            pv_kw=pv_kw,
            limits=limits,
            last_applied_kw=last_applied_kw,
            deadband_kw=deadband_kw,
        )
    if intent == EXECUTION_INTENT_NET_EXPORT:
        return decide_export(
            progress=progress,
            house_load_kw=house_load_kw,
            pv_kw=pv_kw,
            max_discharge_kw=max_discharge_kw,
            reserve_headroom_kwh=reserve_headroom_kwh,
            grid_export_limit_kw=grid_export_limit_kw,
            last_applied_kw=last_applied_kw,
            deadband_kw=deadband_kw,
        )
    return None
