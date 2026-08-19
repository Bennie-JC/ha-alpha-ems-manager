"""Forecast-error statistics, derived from stored evidence and never persisted.

Nothing here is written to disk. Predictions and actuals are the facts; every
number below is a reading of them, recomputed on demand. That is what makes it
safe to improve a metric definition later: a stored ``mae`` field would freeze
one definition into the history and, worse, would eventually disagree with the
data sitting beside it.

Which percentage, and why not the obvious one
---------------------------------------------

Quarter-hour household baseline load is routinely a few hundredths of a
kilowatt-hour at four in the morning. A percentage error per interval therefore
divides by something arbitrarily close to zero: predicting 0.05 kWh against an
actual 0.01 kWh is a 400 % error that means almost nothing in energy terms, and
one such interval swamps any average it enters. MAPE is not stabilised here with
a floor -- a floor is a threshold with no physical quantity behind it -- it is
simply not computed.

The percentage that *is* published is WAPE::

    WAPE = sum(|predicted - actual|) / sum(actual)

summed over a whole window before dividing. The denominator is a week of
household consumption, structurally far from zero, so the figure is bounded,
stable and directly readable: "the model is off by 8 % of the energy it was
predicting". It is deliberately not reported as an accuracy percentage. An
"accuracy" of ``100 - error`` is unbounded below, goes negative on a bad week,
and invites comparison against unrelated systems that computed it differently.

Sign convention
---------------

``error = predicted - actual``. **Positive means the model over-predicted.**
Fixed here once; every consumer inherits it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import FORECAST_SLOT_BANDS
from .forecast_history import DayOutcome, ForecastSnapshot
from .storage import local_slot_for_index

#: A single scored interval: (chronological index, predicted, actual, filled).
_Scored = tuple[int, float, float, bool]


@dataclass(frozen=True, slots=True)
class ScoredDay:
    """One finalised target day paired with the prediction being scored."""

    target_day: date
    snapshot: ForecastSnapshot
    outcome: DayOutcome
    #: Intervals where a prediction and a trustworthy actual both exist.
    scored: tuple[_Scored, ...]

    @property
    def intervals_compared(self) -> int:
        """Return how many intervals could actually be scored."""
        return len(self.scored)

    @property
    def predicted_kwh(self) -> float:
        """Return the predicted energy over the compared intervals only."""
        return sum(predicted for _, predicted, _, _ in self.scored)

    @property
    def actual_kwh(self) -> float:
        """Return the measured energy over the compared intervals only."""
        return sum(actual for _, _, actual, _ in self.scored)

    @property
    def signed_error_kwh(self) -> float:
        """Return predicted minus actual over the compared intervals."""
        return self.predicted_kwh - self.actual_kwh

    @property
    def absolute_error_sum_kwh(self) -> float:
        """Return the summed absolute per-interval error."""
        return sum(abs(predicted - actual) for _, predicted, actual, _ in self.scored)

    @property
    def error_percent(self) -> float | None:
        """Return the day-level percentage error, or ``None`` when meaningless.

        Safe at day level in a way it never is per interval: a day total is the
        sum of ninety-six intervals of household demand, so it is only zero when
        the house genuinely consumed nothing measurable -- and then the answer
        is ``None`` rather than a division.
        """
        actual = self.actual_kwh
        if actual <= 0:
            return None
        return 100.0 * self.signed_error_kwh / actual


def score_day(snapshot: ForecastSnapshot, outcome: DayOutcome) -> ScoredDay | None:
    """Pair one prediction with one outcome, interval by interval.

    Returns ``None`` when the two may not be compared at all. Two rules do that
    work, and both exist to stop a plausible-looking but meaningless number
    being produced:

    * a flagged outcome -- a changed timezone, a changed baseline definition, a
      day with no record -- is never scored, because the two sides are
      describing different things;
    * only intervals with **both** a prediction and a trustworthy actual are
      scored. Comparing a whole-day prediction against a partly observed day
      would report the unmeasured hours as a forecast that came in high, which
      is exactly how a systematic bias gets manufactured out of a sensor outage.
    """
    if not outcome.comparable or not snapshot.available:
        return None
    if snapshot.interval_count != outcome.interval_count:
        return None

    scored: list[_Scored] = []
    for index in outcome.valid_indices():
        predicted = snapshot.predicted_at(index)
        actual = outcome.actual[index]
        if predicted is None or actual is None:
            continue
        filled = index < len(snapshot.filled) and snapshot.filled[index]
        scored.append((index, predicted, actual, filled))

    if not scored:
        return None
    return ScoredDay(
        target_day=outcome.target_day,
        snapshot=snapshot,
        outcome=outcome,
        scored=tuple(scored),
    )


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    """Derived error statistics over a set of scored days."""

    days_compared: int = 0
    intervals_compared: int = 0
    #: Mean absolute error, kWh per quarter-hour interval.
    mae_kwh: float | None = None
    #: Mean signed error. Positive means the model over-predicts.
    bias_kwh: float | None = None
    #: Weighted absolute percentage error, 0..inf, expressed as a percentage.
    wape_percent: float | None = None
    #: Root mean squared error. Reported for diagnostics only: it weights the
    #: few large misses that matter for sizing far more heavily than MAE does.
    rmse_kwh: float | None = None
    predicted_kwh: float = 0.0
    actual_kwh: float = 0.0
    #: MAE restricted to intervals blended from their own behavioural slot.
    mae_modelled_kwh: float | None = None
    #: MAE restricted to intervals extrapolated from a neighbour.
    mae_filled_kwh: float | None = None
    intervals_modelled: int = 0
    intervals_filled: int = 0
    #: MAE per behavioural slot band; see ``FORECAST_SLOT_BANDS``.
    mae_by_band: dict[str, float | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Return a plain mapping for the diagnostics payload."""
        return {
            "days_compared": self.days_compared,
            "intervals_compared": self.intervals_compared,
            "mae_kwh_per_interval": _round(self.mae_kwh, 5),
            "bias_kwh_per_interval": _round(self.bias_kwh, 5),
            "wape_percent": _round(self.wape_percent, 2),
            "rmse_kwh_per_interval": _round(self.rmse_kwh, 5),
            "predicted_kwh": _round(self.predicted_kwh, 3),
            "actual_kwh": _round(self.actual_kwh, 3),
            "mae_modelled_kwh_per_interval": _round(self.mae_modelled_kwh, 5),
            "mae_filled_kwh_per_interval": _round(self.mae_filled_kwh, 5),
            "intervals_modelled": self.intervals_modelled,
            "intervals_filled": self.intervals_filled,
            "mae_by_slot_band": {
                name: _round(value, 5) for name, value in self.mae_by_band.items()
            },
        }


def _round(value: float | None, digits: int) -> float | None:
    """Round a metric, preserving ``None``."""
    return None if value is None else round(value, digits)


def _mean(values: Sequence[float]) -> float | None:
    """Return the arithmetic mean, or ``None`` for an empty sequence."""
    return sum(values) / len(values) if values else None


def compute_window(
    days: Iterable[ScoredDay], tz: ZoneInfo | str | None = None
) -> WindowMetrics:
    """Return the derived statistics for a set of scored days.

    ``tz`` is used only to resolve behavioural slot bands. When omitted, each
    day's own recorded zone is used, which is the correct choice for history
    that may span a timezone change.
    """
    scored_days = list(days)
    empty_bands: dict[str, float | None] = {
        name: None for name, _, _ in FORECAST_SLOT_BANDS
    }
    if not scored_days:
        return WindowMetrics(mae_by_band=empty_bands)

    errors: list[float] = []
    absolute: list[float] = []
    squared: list[float] = []
    modelled: list[float] = []
    filled: list[float] = []
    band_errors: dict[str, list[float]] = {
        name: [] for name, _, _ in FORECAST_SLOT_BANDS
    }
    predicted_total = 0.0
    actual_total = 0.0

    for day in scored_days:
        resolver = _band_resolver(day, tz if tz is not None else day.outcome.tz_key)
        for index, predicted, actual, is_filled in day.scored:
            error = predicted - actual
            errors.append(error)
            absolute.append(abs(error))
            squared.append(error * error)
            predicted_total += predicted
            actual_total += actual
            (filled if is_filled else modelled).append(abs(error))
            if resolver is not None:
                band = resolver(index)
                if band is not None:
                    band_errors[band].append(abs(error))

    mae = _mean(absolute)
    mean_squared = _mean(squared)
    return WindowMetrics(
        days_compared=len(scored_days),
        intervals_compared=len(errors),
        mae_kwh=mae,
        bias_kwh=_mean(errors),
        # The one guard that matters: an all-zero day, or a window with no
        # measured energy at all, must yield no percentage rather than an
        # infinity or a NaN travelling into a sensor state.
        wape_percent=(
            100.0 * sum(absolute) / actual_total if actual_total > 0 else None
        ),
        rmse_kwh=None if mean_squared is None else math.sqrt(mean_squared),
        predicted_kwh=predicted_total,
        actual_kwh=actual_total,
        mae_modelled_kwh=_mean(modelled),
        mae_filled_kwh=_mean(filled),
        intervals_modelled=len(modelled),
        intervals_filled=len(filled),
        mae_by_band={name: _mean(values) for name, values in band_errors.items()},
    )


def _band_resolver(
    day: ScoredDay, zone: ZoneInfo | str | None
) -> Callable[[int], str | None] | None:
    """Return a function mapping a chronological index to a slot-band name.

    The behavioural wall-clock slot is *derived* from the chronological index
    rather than being the index, which is what keeps the bands correct on a
    daylight-saving day: on a fall-back day two different indices legitimately
    land in the same band, and on a spring-forward day one band is an hour
    shorter than usual.

    An unresolvable zone yields ``None`` and the band breakdown is simply
    omitted, rather than raising out of a statistics call.
    """
    if isinstance(zone, str):
        try:
            resolved: ZoneInfo | None = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            return None
    else:
        resolved = zone
    if resolved is None:
        return None

    target = day.target_day

    def resolve(index: int) -> str | None:
        slot = local_slot_for_index(target, index, resolved)
        for name, start, end in FORECAST_SLOT_BANDS:
            if start <= slot < end:
                return name
        return None

    return resolve


# -- reduced facts, and the statistics that can be rebuilt from them ---------


def best_snapshot(snapshots: list[ForecastSnapshot]) -> ForecastSnapshot | None:
    """Return the prediction a day should be scored against.

    The lowest horizon wins, and the latest issuance breaks a tie: that is the
    model's final word on the day, made with the most history behind it. The
    earlier, longer-horizon predictions are not discarded -- they stay in the
    record and are scored separately when the horizon breakdown is built -- but
    a single headline figure has to be one of them, and "what did the model
    think going into the day" is the useful one.
    """
    available = [snapshot for snapshot in snapshots if snapshot.available]
    if not available:
        return None
    return min(available, key=lambda s: (s.horizon_days, -s.issued_at.timestamp()))


def summary_row(
    scored: ScoredDay | None,
    *,
    interval_count: int,
    flags: tuple[str, ...],
) -> dict[str, object]:
    """Return the reduced facts kept once the raw arrays are pruned.

    These are *sufficient statistics*, not metric definitions. Summed absolute
    error, summed actual and the compared count are enough to rebuild MAE, bias
    and WAPE for any window, so the way those are reported can still change
    later. What they cannot rebuild is a metric needing the individual errors --
    RMSE, or a percentile -- and that is the deliberate cost of keeping a
    hundred and fifty bytes a day for a decade instead of a kilobyte.
    """
    if scored is None:
        return {"n": interval_count, "c": 0, "fg": list(flags)}

    filled = [
        abs(predicted - actual)
        for _, predicted, actual, is_filled in scored.scored
        if is_filled
    ]
    context = scored.snapshot.context.get("load_model", {})
    return {
        "n": interval_count,
        "c": scored.intervals_compared,
        "ps": round(scored.predicted_kwh, 4),
        "as": round(scored.actual_kwh, 4),
        "ae": round(scored.absolute_error_sum_kwh, 4),
        "h": scored.snapshot.horizon_days,
        "md": context.get("model_days"),
        "dt": context.get("day_type"),
        "pl": context.get("day_type_pooled"),
        "cf": context.get("confidence_percent"),
        "fn": len(filled),
        "fe": round(sum(filled), 4),
        "fg": list(flags),
    }


@dataclass(frozen=True, slots=True)
class WindowSummary:
    """Rolling statistics rebuilt from summary rows alone.

    Cheap by construction: every figure comes from the always-loaded index, so
    the published sensors need no partition load and therefore no disk access on
    a refresh.
    """

    days_compared: int = 0
    intervals_compared: int = 0
    mae_kwh: float | None = None
    bias_kwh: float | None = None
    wape_percent: float | None = None
    predicted_kwh: float = 0.0
    actual_kwh: float = 0.0

    def as_dict(self) -> dict[str, object]:
        """Return a plain mapping for the diagnostics payload."""
        return {
            "days_compared": self.days_compared,
            "intervals_compared": self.intervals_compared,
            "mae_kwh_per_interval": _round(self.mae_kwh, 5),
            "bias_kwh_per_interval": _round(self.bias_kwh, 5),
            "wape_percent": _round(self.wape_percent, 2),
            "predicted_kwh": _round(self.predicted_kwh, 3),
            "actual_kwh": _round(self.actual_kwh, 3),
        }


def _number(value: object) -> float | None:
    """Return a stored figure as a float, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def window_from_summaries(rows: Iterable[dict[str, object]]) -> WindowSummary:
    """Rebuild rolling statistics from reduced summary rows.

    A row carrying flags, or no compared intervals, contributes nothing: the two
    sides of its comparison were not describing the same thing, and averaging it
    in would be exactly the manufactured bias the scoring rules exist to
    prevent.
    """
    days = 0
    compared = 0
    abs_error = 0.0
    predicted = 0.0
    actual = 0.0

    for row in rows:
        flags = row.get("fg")
        if isinstance(flags, list) and flags:
            continue
        count = _number(row.get("c"))
        row_actual = _number(row.get("as"))
        row_predicted = _number(row.get("ps"))
        row_error = _number(row.get("ae"))
        if not count or row_actual is None or row_predicted is None:
            continue
        if row_error is None:
            continue
        days += 1
        compared += int(count)
        abs_error += row_error
        predicted += row_predicted
        actual += row_actual

    if not compared:
        return WindowSummary()
    return WindowSummary(
        days_compared=days,
        intervals_compared=compared,
        mae_kwh=abs_error / compared,
        bias_kwh=(predicted - actual) / compared,
        # ``actual > 0`` rather than ``!= 0``: a window in which the house
        # measurably consumed nothing has no denominator, and a percentage
        # derived from one would be an infinity travelling into a sensor state.
        wape_percent=(100.0 * abs_error / actual) if actual > 0 else None,
        predicted_kwh=predicted,
        actual_kwh=actual,
    )


def day_error_from_summary(row: dict[str, object] | None) -> dict[str, object] | None:
    """Return the day-level error facts for one summary row, or ``None``.

    ``None`` means there is nothing honest to report -- the day was never
    finalised, carries a flag, or resolved with no comparable interval -- and
    the caller must publish nothing rather than a zero.
    """
    if not row:
        return None
    flags = row.get("fg")
    if isinstance(flags, list) and flags:
        return None
    count = _number(row.get("c"))
    predicted = _number(row.get("ps"))
    actual = _number(row.get("as"))
    abs_error = _number(row.get("ae"))
    if not count or predicted is None or actual is None or abs_error is None:
        return None

    signed = predicted - actual
    interval_count = _number(row.get("n"))
    return {
        "signed_error_kwh": round(signed, 3),
        "absolute_error_kwh": round(abs(signed), 3),
        "mae_kwh_per_interval": round(abs_error / count, 5),
        "predicted_kwh": round(predicted, 3),
        "actual_kwh": round(actual, 3),
        # Safe here in a way it never is per interval: the denominator is a
        # whole day of household demand.
        "error_percent": (None if actual <= 0 else round(100.0 * signed / actual, 2)),
        "intervals_compared": int(count),
        "intervals_in_day": None if interval_count is None else int(interval_count),
        "horizon_days": row.get("h"),
    }
