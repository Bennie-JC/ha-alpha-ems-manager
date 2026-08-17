"""Optional energy-balance sanity check.

After sign normalisation the instantaneous flows should roughly satisfy::

    PV + grid import + battery discharge  ~=  house load + battery charge + grid export

This is a *data-quality signal*, not a metering settlement. It never rejects a
learning interval; it only feeds the confidence score and diagnostics.

Why a single sample is not enough
---------------------------------

The four sources do not share a clock. A house-load template sensor can publish
a fresh 1197 W the instant a kettle switches on, while the battery and grid
meters still report the pre-kettle world for a few seconds. The identity then
reads ``supply 9 W vs demand 1197 W`` -- a 99 % error produced entirely by
timing, with nothing whatsoever wrong with the configuration.

Two mechanisms separate that from a real fault:

**Coherence gating** rejects samples whose sources are too far apart in time, or
where one has stopped reporting altogether. Such samples are *skipped*: not
counted as a pass, not counted as a failure, and never warned about. This
catches a dead or badly lagging source.

**Sustained-failure debounce** is what actually filters transients. A load step
resolves within seconds, so it can produce at most one failing sample at the
one-minute sampling cadence. A wrong sign convention or a mis-selected entity
fails *every* sample. Requiring several consecutive coherent failures before
warning therefore keeps the check sensitive to real faults while silent about
timing noise.

Why the identity cannot close exactly
-------------------------------------

Once the timing artefacts are gone, a residual remains, and it is physical. The
six flows are not all measured at the same electrical boundary: on a hybrid
inverter PV and battery power are DC-side quantities, while house load and grid
are AC-side ones. Energy crossing that boundary is reduced by the conversion
stage, and the inverter's own auxiliary draw is supplied from one of the sources
without ever appearing as house load. The grid and house-load figures may also
come from two different instruments.

:func:`evaluate_balance` therefore compares the residual against an allowance
built from those three causes rather than against a flat percentage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .const import (
    BALANCE_ABSOLUTE_FLOOR_W,
    BALANCE_BASE_ALLOWANCE_W,
    BALANCE_CONVERSION_LOSS_FRACTION,
    BALANCE_GROSS_FAULT_FLOOR_W,
    BALANCE_GROSS_FAULT_MULTIPLE,
    BALANCE_MAX_SOURCE_AGE_SECONDS,
    BALANCE_MAX_SOURCE_SKEW_SECONDS,
    BALANCE_METERING_TOLERANCE,
    BALANCE_MODE_ACTIVE_W,
    BALANCE_SUSTAINED_FAILURES,
)
from .normalization import PowerFlows

#: Outcome of one attempted balance evaluation.
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED_INCOHERENT = "skipped_incoherent"
OUTCOME_UNAVAILABLE = "unavailable"

#: Which term of the allowance is the largest, for diagnostics.
REASON_BASE = "base_allowance"
REASON_CONVERSION = "conversion_loss"
REASON_METERING = "metering"

#: Mode label for a system with nothing meaningful flowing.
MODE_IDLE = "idle"


@dataclass(frozen=True, slots=True)
class SourceCoherence:
    """How well aligned in time the participating sources are."""

    #: Spread between the newest and oldest source report, in seconds.
    skew_seconds: float
    #: Age of the oldest source report, in seconds.
    oldest_age_seconds: float
    #: Number of sources that contributed a timestamp.
    source_count: int

    @property
    def coherent(self) -> bool:
        """Return whether the sources are aligned enough to compare."""
        if self.source_count < 2:
            # With a single timestamped source there is nothing to be skewed
            # against, so the sample is accepted on its age alone.
            return self.oldest_age_seconds <= BALANCE_MAX_SOURCE_AGE_SECONDS
        return (
            self.skew_seconds <= BALANCE_MAX_SOURCE_SKEW_SECONDS
            and self.oldest_age_seconds <= BALANCE_MAX_SOURCE_AGE_SECONDS
        )

    def as_dict(self) -> dict[str, float | int | bool]:
        """Return a plain mapping for the diagnostics payload."""
        return {
            "skew_seconds": round(self.skew_seconds, 1),
            "oldest_age_seconds": round(self.oldest_age_seconds, 1),
            "source_count": self.source_count,
            "coherent": self.coherent,
        }


def measure_coherence(reported_at: list[datetime], now: datetime) -> SourceCoherence:
    """Return the timing spread of the sources that produced a reading."""
    if not reported_at:
        return SourceCoherence(skew_seconds=0.0, oldest_age_seconds=0.0, source_count=0)
    newest = max(reported_at)
    oldest = min(reported_at)
    return SourceCoherence(
        skew_seconds=max(0.0, (newest - oldest).total_seconds()),
        oldest_age_seconds=max(0.0, (now - oldest).total_seconds()),
        source_count=len(reported_at),
    )


def infer_balance_mode(flows: PowerFlows) -> str:
    """Return a short label for what the system is currently doing.

    Diagnostics only -- no entity, no decision depends on this. It exists so a
    residual can be attributed to an operating mode: a mismatch that appears
    only while the battery is converting points somewhere quite different from
    one that is present on plain grid import.

    The label reads ``sources->sinks``, for example ``pv+battery->house`` or
    ``grid->house+battery``. Direction disambiguates the shared names.
    """

    def active(candidates: tuple[tuple[str, float | None], ...]) -> list[str]:
        """Return the names of the flows carrying meaningful power."""
        return [
            name
            for name, value in candidates
            if value is not None and value >= BALANCE_MODE_ACTIVE_W
        ]

    active_sources = active(
        (
            ("pv", flows.pv_w),
            ("grid", flows.grid_import_w),
            ("battery", flows.battery_discharge_w),
        )
    )
    active_sinks = active(
        (
            ("house", flows.house_load_w),
            ("battery", flows.battery_charge_w),
            ("grid", flows.grid_export_w),
        )
    )
    if not active_sources and not active_sinks:
        return MODE_IDLE
    return f"{'+'.join(active_sources) or 'none'}->{'+'.join(active_sinks) or 'none'}"


@dataclass(frozen=True, slots=True)
class BalanceSample:
    """One evaluation of the energy-balance identity."""

    supply_w: float
    demand_w: float
    residual_w: float
    relative_error: float
    within_tolerance: bool
    #: Largest residual the physics permits for these flows, in watts.
    allowed_residual_w: float = 0.0
    #: Gross DC-side power, the quantity the conversion-loss term scales with.
    dc_power_w: float = 0.0
    #: AC-side power scale, the quantity the metering term scales with.
    ac_power_w: float = 0.0
    #: Which allowance term dominates, one of the ``REASON_*`` constants.
    tolerance_reason: str = REASON_BASE
    #: Operating-mode label from :func:`infer_balance_mode`.
    mode: str = MODE_IDLE
    #: The normalised flows this verdict was reached from.
    flows: PowerFlows | None = None
    coherence: SourceCoherence | None = None

    @property
    def outcome(self) -> str:
        """Return whether this sample passed, failed, or was skipped."""
        if self.coherence is not None and not self.coherence.coherent:
            return OUTCOME_SKIPPED_INCOHERENT
        return OUTCOME_PASSED if self.within_tolerance else OUTCOME_FAILED

    @property
    def eligible(self) -> bool:
        """Return whether this sample counts toward the pass rate."""
        return self.outcome in (OUTCOME_PASSED, OUTCOME_FAILED)

    @property
    def excess_w(self) -> float:
        """Return how far the residual overshoots its allowance, in watts."""
        return max(0.0, abs(self.residual_w) - self.allowed_residual_w)

    @property
    def gross_fault_suspected(self) -> bool:
        """Return whether the residual is too large to be a boundary effect.

        A wrong entity or an inverted sign convention does not shade a residual
        by a few percent, it doubles or deletes a kilowatt-scale term. Requiring
        both a large multiple of the allowance *and* a large absolute overshoot
        keeps the stronger wording for those, so a moderate residual -- the kind
        that mixed AC-side and DC-side measurement genuinely produces -- is
        reported without implying the configuration is wrong.
        """
        if self.allowed_residual_w <= 0:
            return abs(self.residual_w) >= BALANCE_GROSS_FAULT_FLOOR_W
        return (
            abs(self.residual_w)
            >= self.allowed_residual_w * BALANCE_GROSS_FAULT_MULTIPLE
            and abs(self.residual_w) >= BALANCE_GROSS_FAULT_FLOOR_W
        )

    def as_dict(self) -> dict[str, object]:
        """Return a plain mapping for the diagnostics payload."""
        payload: dict[str, object] = {
            "supply_w": round(self.supply_w, 1),
            "demand_w": round(self.demand_w, 1),
            "residual_w": round(self.residual_w, 1),
            "allowed_residual_w": round(self.allowed_residual_w, 1),
            "relative_error": round(self.relative_error, 4),
            "within_tolerance": self.within_tolerance,
            "outcome": self.outcome,
            "mode": self.mode,
            "tolerance_reason": self.tolerance_reason,
            "dc_power_w": round(self.dc_power_w, 1),
            "ac_power_w": round(self.ac_power_w, 1),
            "gross_fault_suspected": self.gross_fault_suspected,
        }
        if self.flows is not None:
            payload["flows_w"] = {
                "house_load": _rounded(self.flows.house_load_w),
                "pv": _rounded(self.flows.pv_w),
                "battery_charge": _rounded(self.flows.battery_charge_w),
                "battery_discharge": _rounded(self.flows.battery_discharge_w),
                "grid_import": _rounded(self.flows.grid_import_w),
                "grid_export": _rounded(self.flows.grid_export_w),
            }
        if self.coherence is not None:
            payload["coherence"] = self.coherence.as_dict()
        return payload


def _rounded(value: float | None) -> float | None:
    """Return ``value`` rounded for diagnostics, preserving ``None``."""
    return None if value is None else round(value, 1)


def evaluate_balance(
    flows: PowerFlows, coherence: SourceCoherence | None = None
) -> BalanceSample | None:
    """Evaluate the balance identity, or return ``None`` if it cannot be.

    Every component must be present. A partial snapshot would produce a large
    apparent residual that says nothing about data quality.

    The verdict is an absolute allowance built from three terms, each standing
    for a physically distinct reason the identity cannot close exactly::

        dc_power = pv + battery_charge + battery_discharge
        ac_power = max(supply, demand)

        allowed  = BALANCE_BASE_ALLOWANCE_W
                 + BALANCE_CONVERSION_LOSS_FRACTION * dc_power
                 + BALANCE_METERING_TOLERANCE       * ac_power

        pass     = abs(residual) <= allowed

    ``dc_power`` is the *gross* DC-side flow, not the net. On a hybrid inverter
    PV and battery power are DC quantities while house load and grid are AC
    ones, so every DC watt is taxed by a conversion stage on its way across the
    boundary -- and PV charging a battery is taxed twice, by the MPPT stage and
    the battery DC-DC stage, which is why the two terms are summed rather than
    netted.

    Charging while discharging is impossible, so at most one battery term is
    ever non-zero and the sum is not double-counting the battery itself.

    The previous rule was a flat 15 % of ``max(supply, demand, 250 W)``. This one
    is deliberately *tighter* everywhere above about 700 W -- 15 % of 10 kW is a
    1.5 kW allowance, large enough to hide a mis-selected entity -- while being
    correctly *looser* in the low-power conversion modes where a real inverter's
    efficiency curve genuinely falls away. Below the old 250 W floor the two
    agree to within a couple of watts, so known-good overnight behaviour is
    unchanged.

    ``relative_error`` is still computed and reported, but only as a
    human-readable figure for logs and diagnostics. It is not the gate.
    """
    components = (
        flows.house_load_w,
        flows.pv_w,
        flows.battery_charge_w,
        flows.battery_discharge_w,
        flows.grid_import_w,
        flows.grid_export_w,
    )
    if any(component is None for component in components):
        return None

    house, pv, charge, discharge, imported, exported = components
    supply = pv + imported + discharge  # type: ignore[operator]
    demand = house + charge + exported  # type: ignore[operator]
    residual = supply - demand

    dc_power = pv + charge + discharge  # type: ignore[operator]
    ac_power = max(supply, demand)

    conversion_term = BALANCE_CONVERSION_LOSS_FRACTION * dc_power
    metering_term = BALANCE_METERING_TOLERANCE * ac_power
    allowed = BALANCE_BASE_ALLOWANCE_W + conversion_term + metering_term

    reason = REASON_BASE
    largest = BALANCE_BASE_ALLOWANCE_W
    if conversion_term > largest:
        reason, largest = REASON_CONVERSION, conversion_term
    if metering_term > largest:
        reason = REASON_METERING

    return BalanceSample(
        supply_w=supply,
        demand_w=demand,
        residual_w=residual,
        relative_error=abs(residual) / max(supply, demand, BALANCE_ABSOLUTE_FLOOR_W),
        within_tolerance=abs(residual) <= allowed,
        allowed_residual_w=allowed,
        dc_power_w=dc_power,
        ac_power_w=ac_power,
        tolerance_reason=reason,
        mode=infer_balance_mode(flows),
        flows=flows,
        coherence=coherence,
    )


@dataclass(slots=True)
class BalanceMonitor:
    """In-memory tally and debounce state for the balance check.

    Deliberately not persisted. Consecutive-failure state describes what is
    happening *right now*; carrying it across a restart could warn about a
    condition that has long since been fixed, and a freshly started integration
    should always begin from a clean slate.
    """

    eligible_samples: int = 0
    passed_samples: int = 0
    failed_samples: int = 0
    skipped_incoherent_samples: int = 0
    unavailable_samples: int = 0
    consecutive_failures: int = 0
    #: Highest consecutive-failure run seen this session, for diagnostics.
    worst_consecutive_failures: int = 0
    last_sample: BalanceSample | None = None
    last_coherent_sample: BalanceSample | None = None
    last_warning: str | None = None
    _warned_for_current_run: bool = field(default=False, repr=False)

    @property
    def pass_rate(self) -> float | None:
        """Return passes over *eligible* samples, or ``None`` when there are none.

        Samples skipped for temporal incoherence are excluded from both the
        numerator and the denominator. Counting them as failures would blame the
        configuration for an integration's polling schedule; counting them as
        passes would hide a genuinely dead source.
        """
        if self.eligible_samples <= 0:
            return None
        return self.passed_samples / self.eligible_samples

    def record_unavailable(self) -> None:
        """Record that not every source produced a reading."""
        self.unavailable_samples += 1

    def record(self, sample: BalanceSample) -> str:
        """Record one evaluated sample and return its outcome."""
        self.last_sample = sample
        outcome = sample.outcome

        if outcome == OUTCOME_SKIPPED_INCOHERENT:
            self.skipped_incoherent_samples += 1
            # A skipped sample is neither evidence of health nor of a fault, so
            # it leaves the debounce counter exactly as it was.
            return outcome

        self.eligible_samples += 1
        self.last_coherent_sample = sample

        if sample.within_tolerance:
            self.passed_samples += 1
            self.consecutive_failures = 0
            self._warned_for_current_run = False
        else:
            self.failed_samples += 1
            self.consecutive_failures += 1
            self.worst_consecutive_failures = max(
                self.worst_consecutive_failures, self.consecutive_failures
            )
        return outcome

    @property
    def sustained_failure(self) -> bool:
        """Return whether the imbalance has persisted long enough to report."""
        return self.consecutive_failures >= BALANCE_SUSTAINED_FAILURES

    def should_warn(self) -> bool:
        """Return whether a warning is due for the current failure run.

        True once per sustained run. The run must be broken by a passing
        coherent sample before another warning can be raised, which -- together
        with the caller's rate limiting -- keeps a persistent fault from
        producing a warning per sample.
        """
        if not self.sustained_failure or self._warned_for_current_run:
            return False
        self._warned_for_current_run = True
        return True

    def as_dict(self) -> dict[str, object]:
        """Return a compact mapping for the diagnostics payload."""
        return {
            "eligible_samples": self.eligible_samples,
            "passed_samples": self.passed_samples,
            "failed_samples": self.failed_samples,
            "skipped_incoherent_samples": self.skipped_incoherent_samples,
            "unavailable_samples": self.unavailable_samples,
            "pass_rate": (None if self.pass_rate is None else round(self.pass_rate, 4)),
            "pass_rate_basis": "passed / eligible (incoherent samples excluded)",
            "consecutive_failures": self.consecutive_failures,
            "worst_consecutive_failures": self.worst_consecutive_failures,
            "sustained_failure_threshold": BALANCE_SUSTAINED_FAILURES,
            "max_allowed_skew_seconds": BALANCE_MAX_SOURCE_SKEW_SECONDS,
            "max_allowed_age_seconds": BALANCE_MAX_SOURCE_AGE_SECONDS,
            "tolerance_model": (
                f"allowed_w = {BALANCE_BASE_ALLOWANCE_W:.0f}"
                f" + {BALANCE_CONVERSION_LOSS_FRACTION} * dc_power_w"
                f" + {BALANCE_METERING_TOLERANCE} * ac_power_w"
            ),
            "last_sample": None
            if self.last_sample is None
            else self.last_sample.as_dict(),
            "last_coherent_sample": (
                None
                if self.last_coherent_sample is None
                else self.last_coherent_sample.as_dict()
            ),
            "last_warning": self.last_warning,
        }
