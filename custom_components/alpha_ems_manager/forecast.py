"""Deterministic baseline household-load forecasting.

This is a transparent statistical model, not machine learning, and nothing here
calls a cloud service. Every number a sensor shows can be traced back to an
arithmetic mean of intervals the integration actually measured.

What is forecast
----------------

The **baseline** household load: measured consumption minus separately measured
flexible loads. A future optimiser must not reserve battery energy to cover an
EV charging session that the optimiser itself may end up scheduling, so the
learned demand curve deliberately excludes it. With no flexible-load source
configured, baseline equals measured and the model is unchanged.

Windows
-------

Five overlapping look-back windows (7/30/90/180/365 days) are blended per
behavioural slot. The overlap is the point: a day inside the 7-day window is
also inside all four longer windows, so recent behaviour carries more total
weight than its nominal 0.35 suggests, while the long windows keep seasonal
shape in view. Windows without enough observations drop out and the remaining
weights renormalise, so a three-day-old installation still forecasts.

Day length
----------

A forecast is generated per **chronological interval** of the target civil day,
so it is 92 entries long on a spring-forward day, 96 normally, and 100 on a
fall-back day. Each interval looks up the statistics of its own behavioural
wall-clock slot, which means the repeated fall-back hour is forecast twice and
the skipped spring-forward hour is never forecast at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .const import (
    FORECAST_WINDOW_WEIGHTS,
    FORECAST_WINDOWS,
    MIN_DAY_COMPLETENESS,
    MIN_DAYS_FOR_DAY_TYPE,
    MIN_OBSERVATIONS_PER_WINDOW,
    TODAY_ADAPT_DAMPING,
    TODAY_ADAPT_MIN_BASELINE_KWH,
    TODAY_ADAPT_MIN_ELAPSED_SLOTS,
    TODAY_ADAPT_RATIO_MAX,
    TODAY_ADAPT_RATIO_MIN,
)
from .storage import (
    DayRecord,
    day_type_of,
    expected_quarters_for,
    local_slot_for_index,
)

#: One observation of a behavioural slot: (age in days, day type, kWh).
_Observation = tuple[int, str, float]

# Why a forecast is not published. Reported through diagnostics so a live
# installation can be diagnosed without reading the source: the withholding is
# usually correct, but "unknown" on its own does not say which safeguard fired.

#: No completed day has been recorded yet.
REASON_NO_HISTORY = "no_history"
#: History exists, but no behavioural slot reached MIN_OBSERVATIONS_PER_WINDOW --
#: the usual cause is a single prior day, since a window needs at least two.
REASON_INSUFFICIENT_MODEL_DAYS = "insufficient_model_days"
#: Some slots blended, but too little of the target day could be modelled to
#: publish a whole-day figure without extrapolating.
REASON_INSUFFICIENT_COVERAGE = "insufficient_baseline_coverage"
#: The forecast object was never populated, e.g. read before the first refresh.
REASON_NOT_BUILT = "forecast_not_built"


@dataclass(slots=True)
class DayForecast:
    """A baseline household-load forecast for one calendar day."""

    day: date
    day_type: str
    #: Number of chronological intervals in the target civil day (92/96/100).
    interval_count: int
    #: Per-interval forecast in kWh, chronological. ``None`` only when the model
    #: has no data at all.
    intervals: list[float | None] = field(default_factory=list)
    #: Look-back windows that contributed at least one interval.
    windows_used: tuple[int, ...] = ()
    #: Number of learned days the forecast was derived from.
    source_days: int = 0
    #: True when the day-type split was too thin and all days were pooled.
    day_type_pooled: bool = False
    #: Past days that contributed at least one observation, learned or not.
    usable_days: int = 0
    #: Intervals that a look-back window could actually be blended for.
    modelled_intervals: int = 0
    #: Why nothing is published, or ``None`` when the forecast is available.
    #: Diagnostics only -- this bug was found on a live system where the
    #: withholding was correct but impossible to explain from the outside.
    unavailable_reason: str | None = REASON_NOT_BUILT

    @property
    def available(self) -> bool:
        """Return whether the forecast carries any information."""
        return self.source_days > 0 and any(v is not None for v in self.intervals)

    @property
    def total_kwh(self) -> float | None:
        """Return the forecast day total, or ``None`` when unavailable."""
        if not self.available:
            return None
        return sum(value for value in self.intervals if value is not None)

    def remaining_kwh(self, from_index: int) -> float | None:
        """Return forecast energy from ``from_index`` on, or ``None`` if withheld.

        Gated on :attr:`available` for the same reason :attr:`total_kwh` is.
        Without that gate this summed whatever intervals happened to blend, even
        when the forecast had been deliberately withheld -- so a baseline the
        entity refused to publish still produced a confident-looking remainder
        for anything reading it directly, and diagnostics reported a day total
        for a sensor showing ``unknown``.

        ``from_index`` is a chronological interval index, which advances
        monotonically through a DST fold. A wall-clock slot index would move
        backwards during the repeated hour and re-count energy already consumed.
        """
        if not self.available:
            return None
        start = min(max(0, from_index), self.interval_count)
        return sum(
            value
            for value in self.intervals[start : self.interval_count]
            if value is not None
        )


@dataclass(slots=True)
class TodayForecast:
    """Today's forecast after adaptation to what has actually been measured."""

    #: Baseline energy already measured today.
    actual_so_far_kwh: float
    #: Forecast baseline energy for the remainder of the day, after adaptation.
    #: ``None`` when the underlying baseline is not publishable.
    forecast_remaining_kwh: float | None
    #: Whole-day total: measured so far plus adapted remainder. ``None`` when the
    #: underlying baseline is not publishable -- measured energy alone is not a
    #: forecast, and reporting it as one is what made diagnostics disagree with
    #: the entity.
    forecast_total_kwh: float | None
    #: The damped ratio that was applied. 1.0 means no adaptation.
    adaptation_ratio: float
    #: Whether adaptation was actually applied, as opposed to suppressed.
    adapted: bool
    #: Whether the baseline behind this was publishable at all.
    available: bool = False


def _collect_observations(
    records: list[DayRecord], reference: date
) -> dict[int, list[_Observation]]:
    """Bucket every valid baseline interval by its behavioural wall-clock slot.

    Built once per forecast so the per-window lookups below stay cheap. On a
    fall-back day two chronological intervals land in the same slot bucket, and
    both are kept -- the repeated hour contributes two observations rather than
    overwriting one.
    """
    buckets: dict[int, list[_Observation]] = {}
    for record in records:
        age = (reference - record.day).days
        if age < 1:
            continue
        day_type = record.day_type
        tz = record.tz
        for index in range(record.interval_count):
            value = record.baseline_at(index)
            if value is None:
                continue
            slot = local_slot_for_index(record.day, index, tz)
            buckets.setdefault(slot, []).append((age, day_type, value))
    return buckets


def _mean(
    observations: list[_Observation],
    window: int,
    day_type: str | None,
) -> float | None:
    """Return the mean of the observations inside ``window``, if there are enough."""
    values = [
        value
        for age, obs_type, value in observations
        if age <= window and (day_type is None or obs_type == day_type)
    ]
    if len(values) < MIN_OBSERVATIONS_PER_WINDOW:
        return None
    return sum(values) / len(values)


def build_forecast(
    records: list[DayRecord],
    reference: date,
    target: date,
    tz: Any,
) -> DayForecast:
    """Build a baseline forecast for ``target`` from learned history.

    ``reference`` is today; look-back windows are measured backwards from it,
    and the in-progress day is never used as an input. ``tz`` determines the
    target day's real length.
    """
    day_type = day_type_of(target)
    interval_count = expected_quarters_for(target, tz)
    forecast = DayForecast(
        day=target,
        day_type=day_type,
        interval_count=interval_count,
        intervals=[None] * interval_count,
    )

    usable = [record for record in records if record.day < reference]
    if not usable:
        forecast.unavailable_reason = REASON_NO_HISTORY
        return forecast

    same_type = [record for record in usable if record.day_type == day_type]
    # With only a day or two of a given type in hand, the weekday/weekend split
    # describes one particular Saturday rather than weekends in general.
    pooled = len(same_type) < MIN_DAYS_FOR_DAY_TYPE
    forecast.day_type_pooled = pooled
    # Counted over *learned* days only, so the published ``model_days`` attribute
    # agrees with the Learning Days sensor. A partial day still contributes the
    # intervals it did measure -- a valid interval is real data -- but claiming it
    # as a day the model was built from overstates the history behind a forecast.
    counted = usable if pooled else same_type
    forecast.source_days = sum(1 for record in counted if record.is_learned)

    buckets = _collect_observations(usable, reference)
    forecast.usable_days = len(
        {record.day for record in usable if record.baseline_valid_count > 0}
    )
    if not buckets:
        forecast.source_days = 0
        forecast.unavailable_reason = REASON_NO_HISTORY
        return forecast

    windows_used: set[int] = set()
    slot_cache: dict[int, float | None] = {}

    for index in range(interval_count):
        slot = local_slot_for_index(target, index, tz)
        if slot not in slot_cache:
            slot_cache[slot] = _blend_slot(
                buckets.get(slot, []), day_type, pooled, windows_used
            )
        forecast.intervals[index] = slot_cache[slot]

    forecast.windows_used = tuple(sorted(windows_used))

    # A forecast is only published when most of the day has actually been
    # observed. Below that, any whole-day figure would be extrapolation dressed
    # as a prediction: on a fresh install where only the evening has been seen
    # twice, filling the rest painted the evening rate across all 96 intervals
    # and reported 38 kWh against a real 14 kWh. An `unknown` forecast is the
    # correct answer here, and the same threshold that decides whether a day
    # counts as learned decides whether a day can be forecast.
    known = sum(1 for value in forecast.intervals if value is not None)
    forecast.modelled_intervals = known
    if known < interval_count * MIN_DAY_COMPLETENESS:
        forecast.source_days = 0
        # Nothing blended at all means no slot reached the observation minimum,
        # which is what a single prior day always produces. Some blending means
        # the history is there but too thin to cover the day. The two look
        # identical from outside and call for completely different user action:
        # wait one more day, versus look at why coverage is patchy.
        forecast.unavailable_reason = (
            REASON_INSUFFICIENT_MODEL_DAYS
            if known == 0
            else REASON_INSUFFICIENT_COVERAGE
        )
        return forecast

    _fill_unknown_intervals(forecast)
    forecast.modelled_intervals = interval_count
    forecast.unavailable_reason = None
    return forecast


def _blend_slot(
    observations: list[_Observation],
    day_type: str,
    pooled: bool,
    windows_used: set[int],
) -> float | None:
    """Blend the look-back windows for one behavioural slot."""
    weighted_total = 0.0
    weight_total = 0.0
    for window in FORECAST_WINDOWS:
        mean: float | None = None
        if not pooled:
            mean = _mean(observations, window, day_type)
        if mean is None:
            # Either the split is pooled, or this particular window is too thin
            # for the day type. Falling back to all days keeps a long window
            # contributing shape instead of dropping out entirely.
            mean = _mean(observations, window, None)
        if mean is None:
            continue
        weight = FORECAST_WINDOW_WEIGHTS[window]
        weighted_total += weight * mean
        weight_total += weight
        windows_used.add(window)

    if weight_total <= 0:
        return None
    return weighted_total / weight_total


def _fill_unknown_intervals(forecast: DayForecast) -> None:
    """Fill the few unobserved intervals from their nearest observed neighbour.

    Only reached once most of the day is known, so this really is closing small
    holes rather than inventing a day.

    The filler is the nearest known interval by index, **not** the whole-day
    mean. Household demand is strongly autocorrelated in time, so the interval
    next door is a far better estimate than the daily average -- and the average
    is actively misleading for the case this exists to handle. A flexible-load
    sensor that goes unavailable overnight invalidates the same early-morning
    slots on every day, so they never accumulate enough observations to blend;
    filling those with a mean that includes the evening peak overstated the night
    by more than half, while the day still counted as learned and confidence
    stayed high.

    Distance is measured linearly rather than wrapping at midnight. A leading gap
    therefore inherits the first known interval and a trailing gap the last,
    which is predictable; wrapping would let a 23:45 evening peak fill 00:00.
    """
    known_indices = [
        index for index, value in enumerate(forecast.intervals) if value is not None
    ]
    if not known_indices or len(known_indices) == forecast.interval_count:
        return

    for index, value in enumerate(forecast.intervals):
        if value is not None:
            continue
        nearest = min(known_indices, key=lambda known: abs(known - index))
        forecast.intervals[index] = forecast.intervals[nearest]


def adapt_today(
    baseline: DayForecast,
    measured_baseline: list[float | None],
    measured_total_kwh: float,
    elapsed_intervals: int,
) -> TodayForecast:
    """Blend today's measurements into the remainder of today's forecast.

    If the house has run consistently above the model all morning, the rest of
    the day is nudged up -- but only halfway, and only within a clamp. One long
    oven cycle should tilt the remaining forecast, not rewrite it.

    ``elapsed_intervals`` is a chronological index, so it never moves backwards
    through a daylight-saving fold.
    """
    # A baseline that was withheld has no remainder to adapt. Measured energy is
    # still real and keeps being reported, but it is not a forecast: presenting
    # "what the house has used so far" as a whole-day total is precisely how
    # diagnostics came to show 4.5 kWh for a sensor reading `unknown`.
    if not baseline.available:
        return TodayForecast(
            actual_so_far_kwh=measured_total_kwh,
            forecast_remaining_kwh=None,
            forecast_total_kwh=None,
            adaptation_ratio=1.0,
            adapted=False,
            available=False,
        )

    elapsed = max(0, min(baseline.interval_count, elapsed_intervals))
    baseline_remaining = baseline.remaining_kwh(elapsed) or 0.0

    comparable = [
        index
        for index in range(min(elapsed, len(measured_baseline)))
        if measured_baseline[index] is not None
        and index < baseline.interval_count
        and baseline.intervals[index] is not None
    ]
    actual_sum = sum(measured_baseline[index] or 0.0 for index in comparable)
    baseline_sum = sum(baseline.intervals[index] or 0.0 for index in comparable)

    adapted = (
        elapsed >= TODAY_ADAPT_MIN_ELAPSED_SLOTS
        and baseline_sum >= TODAY_ADAPT_MIN_BASELINE_KWH
        and bool(comparable)
    )

    if adapted:
        raw_ratio = actual_sum / baseline_sum
        damped = 1.0 + TODAY_ADAPT_DAMPING * (raw_ratio - 1.0)
        ratio = min(TODAY_ADAPT_RATIO_MAX, max(TODAY_ADAPT_RATIO_MIN, damped))
    else:
        ratio = 1.0

    remaining = baseline_remaining * ratio
    return TodayForecast(
        actual_so_far_kwh=measured_total_kwh,
        forecast_remaining_kwh=remaining,
        forecast_total_kwh=measured_total_kwh + remaining,
        adaptation_ratio=ratio,
        adapted=adapted,
        available=True,
    )
