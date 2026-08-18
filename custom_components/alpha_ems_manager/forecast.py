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
from datetime import date, timedelta
from typing import Any

from .const import (
    FORECAST_WINDOW_WEIGHTS,
    FORECAST_WINDOWS,
    MIN_DAY_COMPLETENESS,
    MIN_DAYS_FOR_DAY_TYPE,
    MIN_OBSERVATIONS_PER_WINDOW,
    QUARTER_MINUTES,
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
    utc_midnight,
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
    #: Intervals with no observations of their own slot, filled from the nearest
    #: neighbour instead. Published so a day that is mostly extrapolated cannot
    #: look identical to one that was fully observed.
    filled_intervals: int = 0
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


@dataclass(frozen=True, slots=True)
class ForecastInputs:
    """The history a forecast is built from, prepared once per refresh.

    Both of these depend only on ``(records, reference)``, never on the target
    day, so today's and tomorrow's forecasts can share them. They were being
    recomputed per target: at a full year of history the bucketing alone cost
    around 90 ms, and doing it twice put roughly a fifth of a second of
    synchronous work on the event loop every quarter of an hour -- several times
    that on the Raspberry Pi class of host this is expected to run on.
    """

    #: Past days that contribute at least one valid baseline interval.
    usable: list[DayRecord]
    #: Every valid baseline interval, bucketed by behavioural wall-clock slot.
    buckets: dict[int, list[_Observation]]


def collect_forecast_inputs(
    records: list[DayRecord], reference: date
) -> ForecastInputs:
    """Prepare the shared history behind every forecast issued at ``reference``."""
    horizon = max(FORECAST_WINDOWS)
    usable = [
        record
        for record in records
        # ``0 < age`` excludes the in-progress day and any future-dated record a
        # clock excursion may have left behind. The upper bound keeps days that
        # no look-back window can reach from being counted as model inputs:
        # ``_mean`` already ignores them, so reporting them in ``model_days``
        # claimed history the forecast had not used.
        if 0 < (reference - record.day).days <= horizon
        and record.baseline_valid_count > 0
    ]
    return ForecastInputs(
        usable=usable, buckets=_collect_observations(usable, reference)
    )


def _collect_observations(
    records: list[DayRecord], reference: date
) -> dict[int, list[_Observation]]:
    """Bucket every valid baseline interval by its behavioural wall-clock slot.

    Built once per refresh so the per-window lookups below stay cheap. On a
    fall-back day two chronological intervals land in the same slot bucket, and
    both are kept -- the repeated hour contributes two observations rather than
    overwriting one.

    The day's UTC midnight is resolved once per record rather than once per
    interval. ``local_slot_for_index`` recomputes it on every call, which made
    the timezone arithmetic dominate the whole forecast at a year of history.
    The arithmetic is otherwise identical, including through a fold: each slot
    is still read off the interval's own local time.
    """
    quarter = timedelta(minutes=QUARTER_MINUTES)
    buckets: dict[int, list[_Observation]] = {}
    for record in records:
        age = (reference - record.day).days
        if age < 1:
            continue
        day_type = record.day_type
        tz = record.tz
        midnight = utc_midnight(record.day, tz)
        for index in range(record.interval_count):
            value = record.baseline_at(index)
            if value is None:
                continue
            local = (midnight + index * quarter).astimezone(tz)
            slot = local.hour * 4 + local.minute // QUARTER_MINUTES
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
    inputs: ForecastInputs | None = None,
) -> DayForecast:
    """Build a baseline forecast for ``target`` from learned history.

    ``reference`` is today; look-back windows are measured backwards from it,
    and the in-progress day is never used as an input. ``tz`` determines the
    target day's real length.

    ``inputs`` lets a caller issuing several forecasts for the same reference
    day prepare the shared history once; when omitted it is derived from
    ``records``, so a single call needs to know nothing about it.
    """
    day_type = day_type_of(target)
    interval_count = expected_quarters_for(target, tz)
    forecast = DayForecast(
        day=target,
        day_type=day_type,
        interval_count=interval_count,
        intervals=[None] * interval_count,
    )

    # A retained day with no valid baseline interval anywhere -- a house-load
    # outage, a flexible-load sensor that was down all day, the stub left by a
    # restart -- supplies no observation to any slot. It must therefore not be
    # allowed to influence any decision *about* the observations either, and the
    # day-type split is where that leaked: counting such a day toward
    # MIN_DAYS_FOR_DAY_TYPE engaged the weekday/weekend split on the strength of
    # a day contributing nothing, which then narrowed the pool to that one type
    # and could drop ``model_days`` below what the same history pooled would
    # support. ``collect_forecast_inputs`` drops such days, so an unusable day
    # takes part in nothing at all.
    if inputs is None:
        inputs = collect_forecast_inputs(records, reference)
    usable = inputs.usable
    if not usable:
        forecast.unavailable_reason = REASON_NO_HISTORY
        return forecast

    same_type = [record for record in usable if record.day_type == day_type]
    # With only a day or two of a given type in hand, the weekday/weekend split
    # describes one particular Saturday rather than weekends in general.
    #
    # Counted over *learned* days of the type, which is what
    # MIN_DAYS_FOR_DAY_TYPE has always been documented to mean ("minimum number
    # of valid days of a given day type"). Counting merely present days let the
    # split engage on two partial weekends that were not learned; ``counted``
    # then narrowed to those two, ``source_days`` came out zero, and the day was
    # withheld -- with no reason attached, because the coverage gate below never
    # ran. Worse, it was non-monotonic in data: deleting one of the two partial
    # days restored a working forecast, so acquiring history removed one.
    same_type_learned = sum(1 for record in same_type if record.is_learned)
    pooled = same_type_learned < MIN_DAYS_FOR_DAY_TYPE
    forecast.day_type_pooled = pooled
    # Counted over *learned* days only, so the published ``model_days`` attribute
    # agrees with the Learning Days sensor. A partial day still contributes the
    # intervals it did measure -- a valid interval is real data -- but claiming it
    # as a day the model was built from overstates the history behind a forecast.
    counted = usable if pooled else same_type
    forecast.source_days = sum(1 for record in counted if record.is_learned)

    buckets = inputs.buckets
    forecast.usable_days = len({record.day for record in usable})
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
    forecast.filled_intervals = interval_count - known
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
    # ``modelled_intervals`` deliberately keeps the *blended* count. Overwriting
    # it with the day length here made the field a constant on every published
    # forecast and hid the one thing it was added to show: how much of the day
    # came from a neighbouring interval rather than from observations of its own
    # slot. A chronically invalid slot range -- the overnight hours a flexible
    # load sensor drops out for -- was reported as fully modelled.
    forecast.unavailable_reason = None
    if forecast.source_days <= 0:
        # Belt and braces. ``available`` is gated on ``source_days``, so any
        # future path that zeroes it must not be able to withhold a forecast
        # while reporting no reason for doing so.
        forecast.unavailable_reason = REASON_INSUFFICIENT_MODEL_DAYS
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
