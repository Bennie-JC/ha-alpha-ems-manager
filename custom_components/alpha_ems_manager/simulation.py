"""Walking a battery forward through a day, one quarter-hour at a time.

Deliberately ignorant of *why* anything is requested. It asks a caller-supplied
provider what to do with each interval and applies the answer through
:func:`battery.apply_request`, which is the only place a limit is enforced. It
never reads a policy, imports nothing from :mod:`policy`, and contains no second
copy of any clamp -- a shadow limit here would be a second thing to keep in step,
and the first time the two disagreed it would be the shadow that got believed.

That separation is what makes the rest of the roadmap possible. Photovoltaic
production (Phase 5) is another demand series and needs no change here; prices
(Phase 6) are an input to the policy, not to this module; and comparing several
candidate strategies (Phase 8) is already what :func:`compare` does with two.

What a trajectory is *not*
--------------------------

It is not a grid forecast. There is no photovoltaic term, because predicting
production belongs to a later phase and inventing one here would be the
fabrication this project exists to avoid. For a household with solar the
simulated import will substantially exceed reality -- the real array is covering
load the model cannot see. Every figure derived from the grid residual is a
**battery-only counterfactual**, and has to be labelled as one wherever it
surfaces.

What it *is* good for, and what the reserve question actually needs, is the
conditional: given the predicted baseline load and no other generation, where
does the battery end up and when does the floor bite.

Why the arrays stay in memory
-----------------------------

A trajectory holds its per-interval detail, because a caller comparing two of
them needs it. Nothing publishes it: entity attributes are capped at eight flat
values and diagnostics at sixteen entries per list, both deliberately. So the
reductions in this module -- totals, the four behavioural bands, a bounded tally
of which limit bound where -- are the only shape this ever leaves in.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .battery import (
    INTERVAL_HOURS,
    BatteryLimits,
    BatteryRequest,
    BatteryReserve,
    BatteryState,
    GridEnergy,
    IntervalOutcome,
    advance,
    apply_request,
    split_grid_energy,
)
from .const import (
    BATTERY_CONSTRAINTS,
    BATTERY_KWH_PRECISION,
    BATTERY_SOC_PRECISION,
    FORECAST_SLOT_BANDS,
    MAX_BINDING_INTERVALS_REPORTED,
    MODE_CHARGE,
    MODE_DISCHARGE,
)
from .storage import local_slot_for_index

#: What the simulator asks for each interval. Given the state the battery is
#: actually in and the demand expected of it, return a request.
#:
#: A callable rather than a fixed list because any reserve-aware plan is
#: state-dependent: what to do at 18:00 depends on how much was spent by 17:45.
#: The simulator still decides nothing -- it asks, applies and records. A fixed
#: plan is expressed by a provider that ignores both arguments; see
#: :func:`constant_provider`.
RequestProvider = Callable[[BatteryState, "IntervalDemand"], BatteryRequest]


def _round_kwh(value: float | None) -> float | None:
    """Round an energy for reporting, preserving ``None``."""
    return None if value is None else round(value, BATTERY_KWH_PRECISION)


def _round_soc(value: float | None) -> float | None:
    """Round a state of charge for reporting, preserving ``None``."""
    return None if value is None else round(value, BATTERY_SOC_PRECISION)


@dataclass(frozen=True, slots=True)
class IntervalDemand:
    """What one chronological interval is expected to need, AC side.

    ``baseline_kwh`` is ``None`` when the forecast withheld that interval. It is
    emphatically not zero: an unpredicted interval is not a predicted idle house,
    and treating it as one is how a model gets credited for energy nobody
    forecast. Such an interval still advances the battery -- the request is
    applied -- but contributes nothing to any grid total.
    """

    index: int
    baseline_kwh: float | None
    #: ``True`` when the forecast extrapolated this interval from a neighbour
    #: rather than modelling its own behavioural slot. Carried through so a
    #: consumer can widen its margin, and so a later phase can ask whether the
    #: plan went wrong where the forecast was weakest.
    filled: bool = False

    @property
    def known(self) -> bool:
        """Return whether this interval carries a usable demand."""
        return self.baseline_kwh is not None

    @property
    def power_kw(self) -> float | None:
        """Return the demand as an average AC power over the interval."""
        if self.baseline_kwh is None:
            return None
        return self.baseline_kwh / INTERVAL_HOURS


def constant_provider(request: BatteryRequest) -> RequestProvider:
    """Return a provider that asks for the same thing in every interval.

    How a fixed plan is expressed, and how the hold reference trajectory is
    built.
    """

    def provide(_state: BatteryState, _demand: IntervalDemand) -> BatteryRequest:
        return request

    return provide


def sequence_provider(requests: Sequence[BatteryRequest]) -> RequestProvider:
    """Return a provider that replays a fixed sequence by interval index.

    Anything beyond the end of the sequence, or any index outside it, is idle
    rather than an error: an out-of-range index must not escape unchecked, and it
    must not silently reuse a neighbour's request either.
    """

    def provide(_state: BatteryState, demand: IntervalDemand) -> BatteryRequest:
        if 0 <= demand.index < len(requests):
            return requests[demand.index]
        return BatteryRequest.idle()

    return provide


@dataclass(frozen=True, slots=True)
class SimulatedTrajectory:
    """One walk through a sequence of intervals, and everything derived from it.

    Every figure below is a property rather than a stored field, following the
    same discipline as ``BalanceSample`` and ``lifecycle_state``: a stored verdict
    is a second source of truth, and the first time it disagreed with the data
    beside it, it is the stored one that would be believed.
    """

    start_energy_kwh: float
    limits: BatteryLimits
    reserve: BatteryReserve
    demands: tuple[IntervalDemand, ...]
    outcomes: tuple[IntervalOutcome, ...]
    #: Grid exchange per interval, or ``None`` where the demand was unknown.
    grid: tuple[GridEnergy | None, ...]

    # -- shape -----------------------------------------------------------

    @property
    def intervals(self) -> int:
        """Return how many intervals were walked."""
        return len(self.outcomes)

    @property
    def intervals_with_demand(self) -> int:
        """Return how many intervals carried a usable predicted demand."""
        return sum(1 for demand in self.demands if demand.known)

    @property
    def intervals_filled(self) -> int:
        """Return how many compared intervals were extrapolated by the forecast."""
        return sum(1 for demand in self.demands if demand.known and demand.filled)

    # -- state of charge -------------------------------------------------

    @property
    def start_soc_percent(self) -> float:
        """Return the state of charge the walk began at."""
        return self.limits.soc_for_energy(self.start_energy_kwh)

    @property
    def end_energy_kwh(self) -> float:
        """Return the stored DC energy at the end of the walk."""
        if not self.outcomes:
            return self.start_energy_kwh
        return self.outcomes[-1].end_energy_kwh

    @property
    def end_soc_percent(self) -> float:
        """Return the state of charge at the end of the walk."""
        return self.limits.soc_for_energy(self.end_energy_kwh)

    @property
    def minimum_energy_kwh(self) -> float:
        """Return the lowest stored energy reached, the start included."""
        return min(
            [self.start_energy_kwh]
            + [outcome.end_energy_kwh for outcome in self.outcomes]
        )

    @property
    def minimum_soc_percent(self) -> float:
        """Return the lowest state of charge reached."""
        return self.limits.soc_for_energy(self.minimum_energy_kwh)

    @property
    def minimum_soc_index(self) -> int | None:
        """Return the interval at which the minimum is first reached.

        ``None`` when the walk never goes below where it started, which is what a
        hold trajectory does and what a charging one does.
        """
        floor = self.minimum_energy_kwh
        for index, outcome in enumerate(self.outcomes):
            if outcome.end_energy_kwh <= floor:
                return index
        return None

    # -- energy ----------------------------------------------------------

    @property
    def charged_ac_kwh(self) -> float:
        """Return total AC energy charged into the battery."""
        return sum(outcome.charge_ac_kwh for outcome in self.outcomes)

    @property
    def discharged_ac_kwh(self) -> float:
        """Return total AC energy discharged from the battery."""
        return sum(outcome.discharge_ac_kwh for outcome in self.outcomes)

    @property
    def grid_import_kwh(self) -> float:
        """Return simulated grid import over the intervals that had a demand."""
        return sum(entry.import_kwh for entry in self.grid if entry is not None)

    @property
    def grid_export_kwh(self) -> float:
        """Return simulated grid export over the intervals that had a demand."""
        return sum(entry.export_kwh for entry in self.grid if entry is not None)

    @property
    def demand_kwh(self) -> float:
        """Return the predicted demand over the intervals that had one."""
        return sum(
            demand.baseline_kwh for demand in self.demands if demand.baseline_kwh
        )

    # -- constraints -----------------------------------------------------

    @property
    def constraint_counts(self) -> dict[str, int]:
        """Return how many intervals each limit bound.

        A bounded key space -- there are four limits -- so this cannot grow with
        runtime, exactly like the balance monitor's mode tallies.
        """
        counts = dict.fromkeys(BATTERY_CONSTRAINTS, 0)
        for outcome in self.outcomes:
            for name in outcome.constraints:
                if name in counts:
                    counts[name] += 1
        return counts

    @property
    def binding_intervals(self) -> tuple[int, ...]:
        """Return the indices at which some limit reduced the request."""
        return tuple(
            index for index, outcome in enumerate(self.outcomes) if outcome.clamped
        )

    # -- reporting -------------------------------------------------------

    def band_summary(self, day: date, tz: Any) -> dict[str, dict[str, float | None]]:
        """Return per-behavioural-band energy, keyed by band name.

        The band is derived from the interval's own wall clock rather than from
        its index, which is what keeps it right across a daylight-saving change:
        on a fall-back day two distinct indices legitimately land in the same
        band, and on a spring-forward day one band is an hour shorter.
        """
        bands: dict[str, dict[str, float]] = {
            name: {"discharged_kwh": 0.0, "charged_kwh": 0.0, "grid_import_kwh": 0.0}
            for name, _start, _end in FORECAST_SLOT_BANDS
        }
        for index, outcome in enumerate(self.outcomes):
            slot = local_slot_for_index(day, self.demands[index].index, tz)
            band = next(
                (
                    name
                    for name, start, end in FORECAST_SLOT_BANDS
                    if start <= slot < end
                ),
                None,
            )
            if band is None:
                continue
            bands[band]["discharged_kwh"] += outcome.discharge_ac_kwh
            bands[band]["charged_kwh"] += outcome.charge_ac_kwh
            entry = self.grid[index]
            if entry is not None:
                bands[band]["grid_import_kwh"] += entry.import_kwh
        return {
            name: {key: _round_kwh(value) for key, value in values.items()}
            for name, values in bands.items()
        }

    def as_dict(self, day: date | None = None, tz: Any = None) -> dict[str, Any]:
        """Return the reduced, bounded form diagnostics publishes.

        Never the per-interval arrays. Ninety-six values would breach the
        sixteen-entry ceiling every diagnostics list is held to, and would turn a
        support download into a history dump.
        """
        binding = self.binding_intervals
        payload: dict[str, Any] = {
            "intervals": self.intervals,
            "intervals_with_demand": self.intervals_with_demand,
            "intervals_neighbour_filled": self.intervals_filled,
            "start_soc_percent": _round_soc(self.start_soc_percent),
            "end_soc_percent": _round_soc(self.end_soc_percent),
            "minimum_soc_percent": _round_soc(self.minimum_soc_percent),
            "minimum_soc_interval": self.minimum_soc_index,
            "predicted_demand_kwh": _round_kwh(self.demand_kwh),
            "discharged_kwh": _round_kwh(self.discharged_ac_kwh),
            "charged_kwh": _round_kwh(self.charged_ac_kwh),
            "grid_import_kwh": _round_kwh(self.grid_import_kwh),
            "grid_export_kwh": _round_kwh(self.grid_export_kwh),
            "constraint_counts": self.constraint_counts,
            # Complete count beside a capped list, so a bound that bites is
            # visible without the list being able to grow with the history.
            "binding_intervals_total": len(binding),
            "binding_intervals": list(binding[:MAX_BINDING_INTERVALS_REPORTED]),
            "basis": (
                "battery-only counterfactual: predicted baseline load against "
                "the battery, with no photovoltaic production term. For a "
                "household with solar the simulated grid import is expected to "
                "exceed reality"
            ),
        }
        if day is not None and tz is not None:
            payload["bands"] = self.band_summary(day, tz)
        return payload


def simulate(
    state: BatteryState,
    demands: Sequence[IntervalDemand],
    provider: RequestProvider,
) -> SimulatedTrajectory:
    """Walk the battery through ``demands``, asking ``provider`` what to do.

    Pure, total and deterministic: the same state, demands and provider always
    produce an equal trajectory. Every limit comes from
    :func:`battery.apply_request`; nothing is enforced here.
    """
    current = state
    outcomes: list[IntervalOutcome] = []
    grid: list[GridEnergy | None] = []

    for demand in demands:
        request = provider(current, demand)
        outcome = apply_request(current, request)
        outcomes.append(outcome)
        if demand.baseline_kwh is None:
            # No predicted load, so no honest grid residual for this interval.
            # The battery still moved, and that is recorded; the grid total
            # simply does not count an interval nobody forecast.
            grid.append(None)
        else:
            grid.append(
                split_grid_energy(
                    load_ac_kwh=demand.baseline_kwh,
                    charge_ac_kwh=outcome.charge_ac_kwh,
                    discharge_ac_kwh=outcome.discharge_ac_kwh,
                )
            )
        current = advance(current, outcome)

    return SimulatedTrajectory(
        start_energy_kwh=state.energy_kwh,
        limits=state.limits,
        reserve=state.reserve,
        demands=tuple(demands),
        outcomes=tuple(outcomes),
        grid=tuple(grid),
    )


def compare(
    reference: SimulatedTrajectory, candidate: SimulatedTrajectory
) -> dict[str, Any]:
    """Return what a candidate trajectory changes against a reference.

    The reference is the hold trajectory: what happens if the battery does
    nothing. That comparison is the whole of Phase 3's what-if, and it is the
    same shape a later phase will price -- prices multiply the import and export
    differences, and comparing more than two candidates is comparing this
    pairwise.

    Deliberately not a verdict. The differences are reported and the reader
    judges, exactly as the daily-validation block reports a discrepancy rather
    than a pass or a fail.
    """
    return {
        "reference_grid_import_kwh": _round_kwh(reference.grid_import_kwh),
        "candidate_grid_import_kwh": _round_kwh(candidate.grid_import_kwh),
        "grid_import_avoided_kwh": _round_kwh(
            reference.grid_import_kwh - candidate.grid_import_kwh
        ),
        "reference_grid_export_kwh": _round_kwh(reference.grid_export_kwh),
        "candidate_grid_export_kwh": _round_kwh(candidate.grid_export_kwh),
        "reference_end_soc_percent": _round_soc(reference.end_soc_percent),
        "candidate_end_soc_percent": _round_soc(candidate.end_soc_percent),
        "candidate_discharged_kwh": _round_kwh(candidate.discharged_ac_kwh),
        "candidate_charged_kwh": _round_kwh(candidate.charged_ac_kwh),
        "comparable_intervals": min(
            reference.intervals_with_demand, candidate.intervals_with_demand
        ),
    }


def demands_from_forecast(
    intervals: Sequence[float | None],
    filled: Sequence[bool],
    *,
    start_index: int = 0,
    count: int | None = None,
) -> tuple[IntervalDemand, ...]:
    """Build demands from a forecast's chronological arrays.

    ``start_index`` is where the walk begins -- normally the next interval
    boundary, so every interval simulated is a whole one and the partial
    in-progress interval never needs a different duration.

    Indices are clamped into the array rather than trusted. An index outside the
    day would otherwise reach the fill mask, and this project has already been
    bitten once by an out-of-range chronological index being filed as though it
    were inside the day.
    """
    total = len(intervals)
    first = max(0, min(start_index, total))
    last = total if count is None else max(first, min(total, first + count))
    return tuple(
        IntervalDemand(
            index=index,
            baseline_kwh=intervals[index],
            filled=bool(index < len(filled) and filled[index]),
        )
        for index in range(first, last)
    )


def mode_counts(trajectory: SimulatedTrajectory) -> dict[str, int]:
    """Return how many intervals charged, discharged or did nothing."""
    counts = {MODE_CHARGE: 0, MODE_DISCHARGE: 0, "idle": 0}
    for outcome in trajectory.outcomes:
        counts[outcome.mode] = counts.get(outcome.mode, 0) + 1
    return counts
