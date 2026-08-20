"""The battery's physical model, and the one place its limits are enforced.

This module imports nothing from Home Assistant, so every rule below can be
tested against synthetic state. It makes no decision: what the battery *should*
do is :mod:`policy`, and what would happen next is :mod:`simulation`.

The electrical boundary, which is the load-bearing decision here
------------------------------------------------------------------

``energy_kwh``, ``soc_percent`` and ``capacity_kwh`` are **DC-side** quantities:
the state of the pack itself. Charge and discharge powers, the grid residual and
every energy the household sees are **AC-side**.

**Efficiency is applied exactly once, at the moment energy crosses that
boundary, and never in state-of-charge arithmetic.** Getting this wrong is the
single most expensive mistake available in this file: it is baked into a value
the user typed, it is the frame every other quantity is defined against, and a
self-consistent model with the boundary flipped passes every round-trip test that
can be written while carrying a systematic bias of a few percent -- comfortably
inside the noise of an integer-percent state-of-charge sensor, so it would take
weeks of field data to notice.

``test_battery_model.py`` pins it with one assertion that fails if an efficiency
ever migrates into the state-of-charge arithmetic: 10 kWh of AC energy charged at
90 % round trip raises stored DC energy by exactly 9.486832980505138 kWh, and
discharging all of that returns exactly 9.0 kWh AC.

Why a request carries a mode instead of a sign
----------------------------------------------

Both of the following were real defects in the first draft of this model, and
both are arithmetic rather than oversight:

* A **negative requested power** created energy. ``min(-1.0, max_discharge)``
  returns ``-1.0``, the non-negativity guards sit on the available energy rather
  than on the request, and -0.25 kWh of AC energy became +0.2635 kWh of stored DC
  energy -- an effective efficiency of 1.054. A negative is exactly what arrives
  if a caller ever passes a raw battery-power sensor, which
  :func:`normalization.split_battery_power` exists to sanitise.

* **Charging and discharging in the same interval** destroyed energy invisibly.
  Two independently firing rules asking for 4 kW each leave the grid residual
  exactly equal to the load -- identical to doing nothing -- while stored energy
  falls by ``1/eta - eta`` = 0.10541 kWh. Over ninety-six intervals that is
  10.1 kWh, an entire pack, with a perfectly balanced grid trace and nothing in
  the only output a consumer reads to show it. Phase 1 *assumed* this away
  (``energy_balance.py``: "Charging while discharging is impossible, so at most
  one battery term is ever non-zero"); this module has to enforce it.

:class:`BatteryRequest` makes both unrepresentable rather than checked: the mode
carries the direction and the magnitude is validated non-negative. Signed power
exists only where ``Planned Battery Power`` is published, and never inside this
module -- exactly as ``PowerFlows`` resolves source signs once at the edge "so
nothing downstream ever has to reason about signs again".

Two floors, and why they are not the same floor
-----------------------------------------------

``configured_min_soc_percent`` is what the user set. It is the **hard** floor:
:func:`apply_request` clamps here and nothing may cross it.

``effective_min_soc_percent`` is the **policy** target. In Phase 3 the two are
numerically identical, so the user-visible promise holds exactly today. They are
kept apart because Phase 7 will raise the effective reserve dynamically, and
Phase 8 needs to be able to say "a price spike justifies dipping into the
reserve, but never below the floor the user set". Merging two names later costs
nothing; splitting one that is already persisted is expensive.

This module therefore never reads ``effective_min_soc_percent``, and a static
test in ``test_phase_three_boundaries.py`` enforces that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_HOLD,
    BATTERY_MAX_SOC_PERCENT,
    CONSTRAINT_MAX_CHARGE_POWER,
    CONSTRAINT_MAX_DISCHARGE_POWER,
    CONSTRAINT_MAX_SOC,
    CONSTRAINT_MIN_SOC,
    MAX_BATTERY_CAPACITY_KWH,
    MAX_BATTERY_POWER_KW,
    MAX_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    MIN_BATTERY_CAPACITY_KWH,
    MIN_BATTERY_POWER_KW,
    MIN_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    MODE_CHARGE,
    MODE_DISCHARGE,
    MODE_IDLE,
    QUARTER_MINUTES,
    REASON_INVALID_EFFICIENCY,
    REASON_MISSING_CAPACITY,
    REASON_MISSING_POWER_LIMITS,
    RESERVE_CONFIGURED,
    SOC_NOISE_BAND_PERCENT,
)

#: Hours in one interval, derived rather than passed in.
#:
#: Every quarter-hour is exactly fifteen minutes of real time. Daylight saving
#: changes how many a civil day *contains* -- 92, 96 or 100 -- never how long one
#: lasts. Accepting a duration as a parameter invites ``1.0``, ``900`` or, worst,
#: ``0.91666`` "for the short DST hour", which is precisely the class of mistake
#: the interval-identity design exists to prevent. There is no parameter.
INTERVAL_HOURS: float = QUARTER_MINUTES / 60.0


def sanitize_soc_percent(value: float | None) -> float | None:
    """Return a plausible state of charge in percent, or ``None`` if unusable.

    A narrow band either side of 0 and 100 is sensor noise and is clamped back
    in. Anything further out is not a number, it is an unreadable source, and it
    is refused -- the same distinction :func:`normalization.sanitize_ev_w` draws,
    and for a sharper reason here.

    A reading of -20 % looks harmless and is not. The charge headroom of a
    10 kWh pack would be computed as ``(100 - -20) / 100 * 10`` = 12 kWh, so a
    single bad sample would permit filling the pack past its own capacity. The
    two clamps in :func:`apply_request` look symmetrical and are not, which is
    exactly why the refusal belongs here rather than there.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    if value < -SOC_NOISE_BAND_PERCENT:
        return None
    if value > BATTERY_MAX_SOC_PERCENT + SOC_NOISE_BAND_PERCENT:
        return None
    return min(BATTERY_MAX_SOC_PERCENT, max(0.0, value))


def _finite(value: Any) -> float | None:
    """Return a usable float, or ``None``. Booleans and non-finites are refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# -- limits ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatteryLimits:
    """The hardware facts a plan has to respect.

    Constructed only through :func:`build_limits`, which is what guarantees the
    ranges every division in this module relies on.
    """

    #: Usable capacity, DC side, strictly positive.
    capacity_kwh: float
    #: Maximum charge and discharge power, AC side, strictly positive.
    max_charge_kw: float
    max_discharge_kw: float
    #: Upper state-of-charge bound in percent. Internal in Phase 3.
    max_soc_percent: float
    #: Efficiency of one boundary crossing, in ``(0, 1]``.
    #:
    #: Two fields, even though Phase 3 derives both from a single configured
    #: round-trip figure. Keeping them separate from the first release is the
    #: cheap hedge: photovoltaic charging never crosses the AC boundary at all,
    #: so Phase 5 needs asymmetry, and Phase 9 may want to *learn* these. Either
    #: is additive here and would otherwise mean changing every call site.
    charge_efficiency: float
    discharge_efficiency: float

    @property
    def round_trip_efficiency(self) -> float:
        """Return the AC-to-AC round-trip efficiency these two imply."""
        return self.charge_efficiency * self.discharge_efficiency

    def energy_for_soc(self, soc_percent: float) -> float:
        """Return the DC energy a state of charge corresponds to."""
        return soc_percent / 100.0 * self.capacity_kwh

    def soc_for_energy(self, energy_kwh: float) -> float:
        """Return the state of charge a DC energy corresponds to."""
        return energy_kwh / self.capacity_kwh * 100.0


def build_limits(
    *,
    capacity_kwh: Any,
    max_charge_kw: Any,
    max_discharge_kw: Any,
    round_trip_efficiency_percent: Any,
    max_soc_percent: float = BATTERY_MAX_SOC_PERCENT,
) -> tuple[BatteryLimits | None, str | None]:
    """Return validated limits, or ``None`` and the reason they are unusable.

    Every guard here exists because the alternative is worse than a refusal:

    * a capacity of zero raises on the division that derives a state of charge,
      and a *negative* capacity is worse than raising -- it inverts every
      comparison in this module and yields a battery that silently does nothing
      while reporting perfectly valid zeros;
    * an efficiency above 1 turns 10 kWh in into 10.5 kWh out, and no clamp
      downstream would catch it;
    * an efficiency of exactly zero raises on the divisions in
      :func:`apply_request`, because dividing a float by zero raises in Python
      rather than yielding an infinity;
    * a percentage of ``0.90`` where ``90`` belongs would otherwise model a
      plausible-looking battery that loses 90 % of everything, which is why the
      accepted range starts at 50 rather than at 0.

    Nothing is defaulted. A missing capacity or power limit is reported as
    missing, because a limit inferred from a capacity at an assumed C-rate would
    be an invented hardware property and would produce a plan the inverter
    cannot execute.
    """
    capacity = _finite(capacity_kwh)
    if capacity is None or not (
        MIN_BATTERY_CAPACITY_KWH <= capacity <= MAX_BATTERY_CAPACITY_KWH
    ):
        return None, REASON_MISSING_CAPACITY

    charge = _finite(max_charge_kw)
    discharge = _finite(max_discharge_kw)
    if (
        charge is None
        or discharge is None
        or not (MIN_BATTERY_POWER_KW <= charge <= MAX_BATTERY_POWER_KW)
        or not (MIN_BATTERY_POWER_KW <= discharge <= MAX_BATTERY_POWER_KW)
    ):
        return None, REASON_MISSING_POWER_LIMITS

    percent = _finite(round_trip_efficiency_percent)
    if percent is None or not (
        MIN_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT
        <= percent
        <= MAX_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT
    ):
        return None, REASON_INVALID_EFFICIENCY

    ceiling = _finite(max_soc_percent)
    if ceiling is None or not (0.0 < ceiling <= BATTERY_MAX_SOC_PERCENT):
        return None, REASON_MISSING_CAPACITY

    # Split symmetrically. It is the only split that reproduces the measured
    # round trip while remaining agnostic between the two directions, and no user
    # knows the halves separately. Documented as a derivation, not as a fact
    # about the hardware: the resulting 5.13 % per crossing happens to agree with
    # ``BALANCE_CONVERSION_LOSS_FRACTION`` to within 0.13 %, which is a genuine
    # independent check on the default.
    one_way = math.sqrt(percent / 100.0)
    return (
        BatteryLimits(
            capacity_kwh=capacity,
            max_charge_kw=charge,
            max_discharge_kw=discharge,
            max_soc_percent=ceiling,
            charge_efficiency=one_way,
            discharge_efficiency=one_way,
        ),
        None,
    )


# -- reserve -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatteryReserve:
    """The floor the engine obeys, and where it came from.

    Built only through a factory, which is what makes the invariant structural:
    the ``max`` that stops a dynamic reserve dropping below the user's setting
    lives inside :func:`dynamic_reserve` rather than at a call site, so a later
    phase cannot forget it.
    """

    #: The user's setting. The hard floor. Never overwritten, never crossed.
    configured_min_soc_percent: float
    #: What the policy aims at. Equal to the configured value in Phase 3.
    effective_min_soc_percent: float
    #: ``RESERVE_CONFIGURED`` or ``RESERVE_DYNAMIC``.
    source: str

    @property
    def raised_above_configured(self) -> bool:
        """Return whether something has raised the floor above the user's."""
        return self.effective_min_soc_percent > self.configured_min_soc_percent


def static_reserve(configured_min_soc_percent: float) -> BatteryReserve:
    """Return the Phase-3 reserve: the user's setting, and nothing else.

    Phase 7 will add a ``dynamic_reserve`` beside this one. It must return
    ``max(configured, dynamic)`` and must compute that maximum *here*, not in its
    caller, so that a dynamic reserve is structurally incapable of lowering the
    floor the user chose.
    """
    floor = max(0.0, min(BATTERY_MAX_SOC_PERCENT, configured_min_soc_percent))
    return BatteryReserve(
        configured_min_soc_percent=floor,
        effective_min_soc_percent=floor,
        source=RESERVE_CONFIGURED,
    )


# -- state -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatteryState:
    """Where the battery is, in the units the model reasons in.

    ``energy_kwh`` is the one stored quantity and everything else is derived.
    Not because of floating-point drift -- measured at about 1.4e-14 kWh over a
    hundred sequential intervals, ten orders of magnitude below the two decimals
    anything is reported to -- but for three structural reasons:

    * energy is the conserved quantity, and every clamp in this module is
      natively an energy;
    * it matches the rest of the project, where ``BalanceSample`` stores watts
      and derives its verdicts, and ``LoadForecast`` stores intervals and derives
      its total;
    * it removes a whole bug class. Storing a state of charge *and* mutating an
      energy leaves the two consistent only for as long as every step remembers
      to recompute, which a reviewer has to verify by eye and a refactor can
      quietly break. Derived, divergence is unrepresentable.
    """

    #: Stored DC energy.
    energy_kwh: float
    limits: BatteryLimits
    reserve: BatteryReserve

    @property
    def soc_percent(self) -> float:
        """Return the state of charge implied by the stored energy."""
        return self.limits.soc_for_energy(self.energy_kwh)

    @property
    def floor_energy_kwh(self) -> float:
        """Return the DC energy at the user's configured floor.

        The **configured** minimum, deliberately. This is the hard limit, and a
        policy reserve raised by a later phase must not be able to move it.
        """
        return self.limits.energy_for_soc(self.reserve.configured_min_soc_percent)

    @property
    def ceiling_energy_kwh(self) -> float:
        """Return the DC energy at the upper state-of-charge bound."""
        return self.limits.energy_for_soc(self.limits.max_soc_percent)

    @property
    def usable_energy_kwh(self) -> float:
        """Return DC energy above the hard floor. Never negative."""
        return max(0.0, self.energy_kwh - self.floor_energy_kwh)

    @property
    def deliverable_energy_kwh(self) -> float:
        """Return the AC energy the usable DC energy would deliver.

        Smaller than :attr:`usable_energy_kwh` by one boundary crossing. Both are
        published, separately and under names that say which is which, because
        conflating them is the most likely way to over-promise a reserve.

        Optimistic by construction, and knowingly so: a single efficiency figure
        overstates what a real inverter delivers at low power, and the inverter's
        own auxiliary draw is not modelled at all. Both biases push the same way,
        so this is an upper bound.
        """
        return self.usable_energy_kwh * self.limits.discharge_efficiency

    @property
    def headroom_energy_kwh(self) -> float:
        """Return DC energy below the ceiling. Never negative."""
        return max(0.0, self.ceiling_energy_kwh - self.energy_kwh)

    @property
    def at_or_below_floor(self) -> bool:
        """Return whether there is nothing left to give above the hard floor."""
        return self.usable_energy_kwh <= 0.0

    @property
    def below_floor(self) -> bool:
        """Return whether the pack is *under* the user's configured floor."""
        return self.energy_kwh < self.floor_energy_kwh


def build_state(
    *,
    soc_percent: float | None,
    limits: BatteryLimits,
    reserve: BatteryReserve,
) -> BatteryState | None:
    """Seed a state from a sanitised state-of-charge reading, or ``None``.

    Seeded from the state of charge every refresh rather than carried forward.
    That is what keeps a mid-session capacity change honest: the pack did not
    change, so re-deriving the energy from the reading is right, while carrying
    an energy across would make the state of charge jump.
    """
    clean = sanitize_soc_percent(soc_percent)
    if clean is None:
        return None
    return BatteryState(
        energy_kwh=limits.energy_for_soc(clean), limits=limits, reserve=reserve
    )


# -- requests ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatteryRequest:
    """One interval's request: a direction, and a non-negative AC magnitude.

    Constructed through :meth:`idle`, :meth:`charge` and :meth:`discharge`, which
    is what makes a signed magnitude and a simultaneous charge-and-discharge
    unrepresentable rather than merely invalid. See the module docstring for the
    two defects this shape exists to eliminate.
    """

    mode: str
    #: AC power, always ``>= 0``.
    power_kw: float

    @classmethod
    def idle(cls) -> BatteryRequest:
        """Return a request to do nothing."""
        return cls(mode=MODE_IDLE, power_kw=0.0)

    @classmethod
    def charge(cls, power_kw: float) -> BatteryRequest:
        """Return a charge request, refusing a non-finite or negative magnitude."""
        return cls._directed(MODE_CHARGE, power_kw)

    @classmethod
    def discharge(cls, power_kw: float) -> BatteryRequest:
        """Return a discharge request, refusing a non-finite or negative one."""
        return cls._directed(MODE_DISCHARGE, power_kw)

    @classmethod
    def _directed(cls, mode: str, power_kw: float) -> BatteryRequest:
        """Return a directed request, degrading anything unusable to idle."""
        magnitude = _finite(power_kw)
        if magnitude is None or magnitude <= 0.0:
            # A non-finite magnitude must not become a zero-power request of the
            # requested direction: ``NaN`` survives ``min`` in one argument order
            # and not the other, so it would reach the stored energy and poison
            # every interval after it. Idle is the honest degradation.
            return cls.idle()
        return cls(mode=mode, power_kw=magnitude)

    @property
    def is_idle(self) -> bool:
        """Return whether this request asks for nothing."""
        return self.mode == MODE_IDLE or self.power_kw <= 0.0


# -- the single clamp --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntervalOutcome:
    """What one interval actually did, after every limit was applied.

    ``charge_ac_kwh`` and ``discharge_ac_kwh`` are both non-negative and at most
    one of them is ever non-zero, mirroring ``PowerFlows``. The energy is the
    primitive; the average power is a reading of it.
    """

    #: What was asked for, kept so a plan can explain what it wanted.
    request: BatteryRequest
    #: The direction actually taken. ``MODE_IDLE`` when everything clamped away.
    mode: str
    #: AC energy into and out of the battery. Both ``>= 0``, at most one non-zero.
    charge_ac_kwh: float
    discharge_ac_kwh: float
    #: Stored DC energy at the end of the interval.
    end_energy_kwh: float
    #: Which limits bound this interval. Empty when nothing did.
    constraints: tuple[str, ...] = ()

    @property
    def allowed_energy_ac_kwh(self) -> float:
        """Return the AC energy that crossed the boundary, in either direction."""
        return self.charge_ac_kwh + self.discharge_ac_kwh

    @property
    def average_power_kw(self) -> float:
        """Return the interval-average AC power, unsigned.

        An *average*. In the final partial interval before the floor a real
        device delivers full power for part of the interval and nothing for the
        rest, so this is not an instantaneous setpoint and must not be published
        as one.
        """
        return self.allowed_energy_ac_kwh / INTERVAL_HOURS

    @property
    def action(self) -> str:
        """Return the action this outcome represents."""
        if self.mode == MODE_CHARGE:
            return ACTION_CHARGE
        if self.mode == MODE_DISCHARGE:
            return ACTION_DISCHARGE
        return ACTION_HOLD

    @property
    def clamped(self) -> bool:
        """Return whether any limit reduced the request."""
        return bool(self.constraints)


def _clamp_energy(value: float, *, lower: float, upper: float) -> float:
    """Return ``value`` confined to a band that already contains the start.

    The band is widened to include wherever the interval began, so a pack that
    starts below the floor is left where it is rather than being quietly topped
    up to the floor. If this ever moved a state of charge somewhere it had not
    been, it would be fabricating energy.

    **A backstop, and measured to be one.** Neutralising this function and
    sweeping 7840 combinations of capacity, floor, efficiency, starting state and
    request magnitude -- including two hundred repeated applications of each, so
    any drift would accumulate -- produced an overshoot of exactly zero in both
    directions. The band invariant holds from the arithmetic in
    :func:`apply_request` itself, which clamps the energy in DC terms before
    converting back.

    It is kept because it is two comparisons, and because it turns an invariant
    that currently happens to hold into one that cannot stop holding if the
    conversion order is ever changed. It is not what makes the model safe today,
    and claiming otherwise would misdirect the next person to read this.
    """
    return min(max(value, lower), upper)


def apply_request(state: BatteryState, request: BatteryRequest) -> IntervalOutcome:
    """Apply one request to one interval. **The only place limits are enforced.**

    No policy, simulator, entity or coordinator may re-implement any of this.
    A second copy of a safety limit is a second thing to keep in step, and the
    first time the two disagreed it would be the copy that got believed.

    The ordering is not incidental:

    * the **power** limit is applied in AC terms, because that is the side the
      inverter's nameplate and the grid residual are denominated in;
    * the **energy** limit is applied in DC terms, because the state of charge is
      the physical state of the pack and the available window is a window in it;
    * the conversion back to AC happens *after* the energy clamp, so the two
      conversions are exact inverses and an allowed power fed back in reproduces
      itself.

    Clamping the AC energy against ``available * eta`` instead would be
    arithmetically equivalent -- measured across two hundred thousand request
    magnitudes the two orderings differ by at most 9e-16 kWh, and neither ever
    exceeds the available energy. The DC-first ordering is preferred because the
    energy limit *is* a DC quantity and expressing it as one keeps the units
    honest, not because the alternative is unsafe.

    So it is this ordering, and not the backstop in :func:`_clamp_energy`, that
    keeps every trajectory inside its band. The sweep in ``test_battery_model``
    is what actually holds that down.

    One counter-intuitive consequence worth knowing before touching this: in the
    ceiling-bound charge case the imported AC energy is ``headroom /
    charge_efficiency``, which *decreases* as efficiency rises. A more efficient
    charger imports less to fill the same headroom. Physically right, and it
    breaks any sweep that assumes otherwise.
    """
    limits = state.limits
    lower = min(state.floor_energy_kwh, state.energy_kwh)
    upper = max(state.ceiling_energy_kwh, state.energy_kwh)

    if request.is_idle:
        return IntervalOutcome(
            request=request,
            mode=MODE_IDLE,
            charge_ac_kwh=0.0,
            discharge_ac_kwh=0.0,
            end_energy_kwh=_clamp_energy(state.energy_kwh, lower=lower, upper=upper),
        )

    constraints: list[str] = []

    if request.mode == MODE_DISCHARGE:
        power = request.power_kw
        if power > limits.max_discharge_kw:
            power = limits.max_discharge_kw
            constraints.append(CONSTRAINT_MAX_DISCHARGE_POWER)

        energy_ac = power * INTERVAL_HOURS
        energy_dc = energy_ac / limits.discharge_efficiency
        available = state.usable_energy_kwh
        if energy_dc > available:
            energy_dc = available
            constraints.append(CONSTRAINT_MIN_SOC)
        energy_ac = energy_dc * limits.discharge_efficiency

        end = _clamp_energy(state.energy_kwh - energy_dc, lower=lower, upper=upper)
        return IntervalOutcome(
            request=request,
            mode=MODE_DISCHARGE if energy_ac > 0.0 else MODE_IDLE,
            charge_ac_kwh=0.0,
            discharge_ac_kwh=energy_ac,
            end_energy_kwh=end,
            constraints=tuple(constraints),
        )

    power = request.power_kw
    if power > limits.max_charge_kw:
        power = limits.max_charge_kw
        constraints.append(CONSTRAINT_MAX_CHARGE_POWER)

    energy_ac = power * INTERVAL_HOURS
    energy_dc = energy_ac * limits.charge_efficiency
    headroom = state.headroom_energy_kwh
    if energy_dc > headroom:
        energy_dc = headroom
        constraints.append(CONSTRAINT_MAX_SOC)
    energy_ac = energy_dc / limits.charge_efficiency

    end = _clamp_energy(state.energy_kwh + energy_dc, lower=lower, upper=upper)
    return IntervalOutcome(
        request=request,
        mode=MODE_CHARGE if energy_ac > 0.0 else MODE_IDLE,
        charge_ac_kwh=energy_ac,
        discharge_ac_kwh=0.0,
        end_energy_kwh=end,
        constraints=tuple(constraints),
    )


def advance(state: BatteryState, outcome: IntervalOutcome) -> BatteryState:
    """Return the state the battery is in after an outcome."""
    return BatteryState(
        energy_kwh=outcome.end_energy_kwh,
        limits=state.limits,
        reserve=state.reserve,
    )


# -- grid residual -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GridEnergy:
    """One interval's grid exchange, unsigned, at most one side non-zero.

    Shaped exactly like ``split_grid_power``: a signed field would reintroduce
    the sign reasoning ``PowerFlows`` was built to end ("Mixed source sign
    conventions are resolved before a ``PowerFlows`` is constructed, so nothing
    downstream ever has to reason about signs again"). Grid sign is the one thing
    this project has a tested opinion about -- it is a user-facing configuration
    option precisely because it is the field's most common source of error.

    Import and export are also priced differently, so any later cost layer has
    to split them anyway, and a signed daily sum is not a cost basis but would be
    read as one.
    """

    import_kwh: float = 0.0
    export_kwh: float = 0.0


def split_grid_energy(
    *,
    load_ac_kwh: float,
    charge_ac_kwh: float,
    discharge_ac_kwh: float,
    pv_ac_kwh: float = 0.0,
) -> GridEnergy:
    """Return the grid exchange implied by one interval's AC flows.

    ``pv_ac_kwh`` defaults to zero, which is what every caller passed before a
    photovoltaic forecast existed and what a caller without one still passes. It
    is a *forecast* of production, not an invention of one: with no forecast the
    term stays absent and the figures remain the battery-only counterfactual they
    always were -- for a household with solar, simulated import then substantially
    exceeds reality, because the real array is covering load the model cannot see.
    That caveat has to be stated wherever these figures appear, and it stops being
    true only for the intervals a real forecast covers.

    Every term is netted here, in AC energy, before anything is converted. That is
    the same rule that makes :class:`BatteryRequest` refuse a signed power:
    netting after conversion destroys energy invisibly, because a charge and a
    discharge of equal size are not inverse operations once efficiency is applied.

    Export can be non-zero either because discharge exceeds the remaining load or
    because production does, and the second is now a real case rather than a
    surprising artefact.

    A grid limit is not modelled. Adding one later turns this into a fixed-point
    problem, because a grid clamp constrains the battery request, which changes
    the residual. Worth knowing before attempting it.
    """
    net = load_ac_kwh - pv_ac_kwh + charge_ac_kwh - discharge_ac_kwh
    return GridEnergy(import_kwh=max(0.0, net), export_kwh=max(0.0, -net))


# -- reporting ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatteryInputs:
    """The Phase-3 inputs as they were read, for diagnostics.

    Reported whether or not they were usable, so a download says which field is
    missing rather than only that something is.
    """

    soc_percent: float | None
    capacity_kwh: float | None
    max_charge_kw: float | None
    max_discharge_kw: float | None
    round_trip_efficiency_percent: float | None
    configured_min_soc_percent: float
    #: Present only for the coherence note; never used to derive stored energy.
    #: Positive for charging, already resolved out of the user's own sign
    #: convention, so it can be read without knowing that setting.
    battery_power_w: float | None = None
