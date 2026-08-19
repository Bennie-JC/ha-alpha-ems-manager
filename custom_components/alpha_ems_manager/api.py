"""The stable read-only interface later phases consume.

Phase 3 -- Battery Decision & Simulation -- needs two things from Phase 2: the
load forecast it is planning against, and an evidence-based sense of how wrong
that forecast usually is. It must get both from here.

The rule is one-directional and deliberate: **nothing outside this module may
reach into the forecast-history internals.** ``forecast_history``,
``history_store``, ``forecast_recorder`` and ``metrics`` are implementation. If
Phase 3 reads a partition dictionary directly, the storage layout can never be
changed again without breaking the battery logic, and the partitioning scheme is
exactly the kind of thing that will want to change. ``tests/test_api_boundary.py``
enforces this statically.

Everything returned here is frozen and copied. A caller cannot reach back
through a returned object and mutate coordinator state or stored evidence.

What this module deliberately does not do
-----------------------------------------

It makes no decision. There is no reserve calculation, no charge or discharge
recommendation, no price, no schedule and no simulation. It reports what was
predicted and how that prediction has historically performed; what to do about
that belongs to Phase 3 and later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import FORECAST_SLOT_BANDS
from .metrics import WindowMetrics, best_snapshot, compute_window
from .storage import local_slot_for_index

if TYPE_CHECKING:
    from .coordinator import AlphaEmsCoordinator

#: Version of this interface. Bumped when a field is removed or its meaning
#: changes, so a later phase can assert what it was written against. Adding a
#: field does not bump it.
API_VERSION = 1


@dataclass(frozen=True, slots=True)
class LoadForecast:
    """A baseline household-load forecast, as a consumer should see it.

    ``intervals`` is chronological and exactly ``interval_count`` long, so it is
    92 entries on a spring-forward day and 100 on a fall-back day. Index ``i``
    covers the quarter-hour beginning at ``utc_midnight(day) + i * 15 min``.
    Never index this by a wall-clock label.
    """

    day: date
    tz_key: str
    interval_count: int
    #: Predicted baseline kWh per chronological interval. Empty when withheld.
    intervals: tuple[float | None, ...]
    #: ``True`` where the value was extrapolated from a neighbouring interval
    #: rather than modelled from observations of its own behavioural slot. A
    #: consumer that wants to be careful should widen its margin on these.
    filled: tuple[bool, ...]
    #: Whether the model published anything at all. When false, every other
    #: field is empty and ``unavailable_reason`` says why. A withheld forecast
    #: is a correct answer and must not be replaced with zeros.
    available: bool
    unavailable_reason: str | None
    #: Number of learned days behind the model, and the published confidence.
    model_days: int
    confidence_percent: float | None
    #: When this prediction was issued, and how far ahead it was looking.
    #: ``None`` for a live forecast that has not yet been snapshotted.
    issued_at: datetime | None = None
    horizon_days: int | None = None

    @property
    def total_kwh(self) -> float | None:
        """Return the predicted day total, or ``None`` when withheld."""
        if not self.available:
            return None
        return round(sum(v for v in self.intervals if v is not None), 4)

    def remaining_kwh(self, from_index: int) -> float | None:
        """Return predicted energy from ``from_index`` on, or ``None``.

        ``from_index`` is a chronological index, which advances monotonically
        through a daylight-saving fold. A wall-clock index would move backwards
        during the repeated hour and re-count energy already consumed.
        """
        if not self.available:
            return None
        start = min(max(0, from_index), self.interval_count)
        return round(
            sum(
                value
                for value in self.intervals[start : self.interval_count]
                if value is not None
            ),
            4,
        )


@dataclass(frozen=True, slots=True)
class ForecastUncertainty:
    """How wrong the load forecast has actually been, from stored evidence.

    Every figure is measured, not assumed. A consumer sizing a reserve should
    prefer these over a guessed safety margin -- and should treat
    ``intervals_compared`` as the gate: below a meaningful sample the fields are
    ``None``, and ``None`` means "no evidence", never "no error".
    """

    window_days: int
    days_compared: int
    intervals_compared: int
    #: Mean absolute error in kWh per quarter-hour interval.
    mae_kwh: float | None
    #: Mean signed error. **Positive means the model over-predicts.**
    bias_kwh: float | None
    #: ``sum(|error|) / sum(actual)`` over the window, as a percentage.
    wape_percent: float | None
    #: MAE per behavioural slot band: night, morning, afternoon, evening.
    mae_by_band: dict[str, float | None]
    #: MAE split by whether the interval was modelled or neighbour-filled.
    mae_modelled_kwh: float | None
    mae_filled_kwh: float | None

    def band_of(self, day: date, index: int, tz_key: str | None = None) -> str | None:
        """Return which slot band a chronological interval falls in."""
        try:
            zone = ZoneInfo(tz_key) if tz_key else None
        except (ZoneInfoNotFoundError, ValueError):
            return None
        if zone is None:
            return None
        slot = local_slot_for_index(day, index, zone)
        for name, start, end in FORECAST_SLOT_BANDS:
            if start <= slot < end:
                return name
        return None

    def interval_margin_kwh(self, band: str | None) -> float | None:
        """Return the measured typical error for one slot band.

        The number a consumer should widen a per-interval plan by. Falls back to
        the whole-window MAE when the band has no evidence of its own, and
        returns ``None`` when there is no evidence at all -- at which point the
        caller must decide for itself, rather than being handed a zero that
        would read as a perfectly reliable forecast.
        """
        if band is not None:
            value = self.mae_by_band.get(band)
            if value is not None:
                return value
        return self.mae_kwh


def _as_forecast(
    forecast: Any,
    *,
    tz_key: str,
    confidence_percent: float | None,
) -> LoadForecast:
    """Convert an internal ``DayForecast`` into the public shape."""
    return LoadForecast(
        day=forecast.day,
        tz_key=tz_key,
        interval_count=forecast.interval_count,
        intervals=tuple(forecast.intervals) if forecast.available else (),
        filled=tuple(forecast.filled) if forecast.available else (),
        available=forecast.available,
        unavailable_reason=forecast.unavailable_reason,
        model_days=forecast.source_days,
        confidence_percent=confidence_percent,
    )


def current_forecast(
    coordinator: AlphaEmsCoordinator, day: date | None = None
) -> LoadForecast | None:
    """Return the live baseline forecast for today or tomorrow.

    This is the unadapted model forecast, which is the only quantity that can be
    planned against consistently. The Today *entity* additionally blends the
    energy already measured today into its total, making that figure a hybrid of
    prediction and measurement -- useful to a person reading a dashboard, and
    wrong to feed into a planner as if it were a forecast.

    Returns ``None`` for any day other than today or tomorrow: the model does
    not produce one, and inventing a fallback would hide that from the caller.
    """
    data = coordinator.data or {}
    baseline = data.get("today_baseline")
    tomorrow = data.get("tomorrow")
    if baseline is None or tomorrow is None:
        return None

    target = day if day is not None else baseline.day
    confidence = data.get("confidence")
    percent = None if confidence is None else round(confidence.percent, 1)
    tz_key = str(coordinator.hass.config.time_zone or "")

    for forecast in (baseline, tomorrow):
        if forecast.day == target:
            return _as_forecast(forecast, tz_key=tz_key, confidence_percent=percent)
    return None


async def async_issued_forecast(
    coordinator: AlphaEmsCoordinator, day: date, horizon_days: int | None = None
) -> LoadForecast | None:
    """Return a *historical* prediction for a target day, as it was issued.

    With no ``horizon_days`` the prediction actually scored for that day is
    returned -- the lowest horizon, which is the model's final word on it. Pass
    a horizon to ask what the model was saying that many days ahead.

    This is how a later phase reconstructs what was known at decision time,
    rather than what is known now.
    """
    history = coordinator.history
    if history.corrupt:
        return None
    await history.async_ensure_days([day])

    snapshots = history.snapshots(day)
    if not snapshots:
        return None
    if horizon_days is None:
        snapshot = best_snapshot(snapshots)
    else:
        matching = [s for s in snapshots if s.horizon_days == horizon_days]
        snapshot = matching[-1] if matching else None
    if snapshot is None:
        return None

    context = snapshot.context.get("load_model", {})
    return LoadForecast(
        day=snapshot.target_day,
        tz_key=snapshot.tz_key,
        interval_count=snapshot.interval_count,
        intervals=snapshot.predicted,
        filled=snapshot.filled,
        available=snapshot.available,
        unavailable_reason=snapshot.unavailable_reason,
        model_days=int(context.get("model_days") or 0),
        confidence_percent=context.get("confidence_percent"),
        issued_at=snapshot.issued_at,
        horizon_days=snapshot.horizon_days,
    )


async def async_uncertainty(
    coordinator: AlphaEmsCoordinator, window_days: int = 30
) -> ForecastUncertainty:
    """Return measured forecast-error statistics over the trailing window.

    The window ends at yesterday. The day in progress has not finished, and
    scoring a partial day against a whole-day prediction is the one comparison
    this design refuses to make.
    """
    empty_bands: dict[str, float | None] = {
        name: None for name, _, _ in FORECAST_SLOT_BANDS
    }
    history = coordinator.history
    today = coordinator.today_date()
    if history.corrupt:
        return ForecastUncertainty(
            window_days=window_days,
            days_compared=0,
            intervals_compared=0,
            mae_kwh=None,
            bias_kwh=None,
            wape_percent=None,
            mae_by_band=empty_bands,
            mae_modelled_kwh=None,
            mae_filled_kwh=None,
        )

    start = today - timedelta(days=window_days)
    await history.async_ensure_days(
        [day for day in history.days if start <= day < today]
    )
    metrics: WindowMetrics = compute_window(
        coordinator.recorder.scored_days(start, today)
    )
    return ForecastUncertainty(
        window_days=window_days,
        days_compared=metrics.days_compared,
        intervals_compared=metrics.intervals_compared,
        mae_kwh=metrics.mae_kwh,
        bias_kwh=metrics.bias_kwh,
        wape_percent=metrics.wape_percent,
        mae_by_band=dict(metrics.mae_by_band) or empty_bands,
        mae_modelled_kwh=metrics.mae_modelled_kwh,
        mae_filled_kwh=metrics.mae_filled_kwh,
    )
