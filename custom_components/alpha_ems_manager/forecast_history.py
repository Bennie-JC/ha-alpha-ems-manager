"""Immutable forecast evidence: what was predicted, and what actually happened.

This module is the Phase-2 record model. It defines one immutable *issuance
snapshot* per distinct forecast, one *day outcome* per finalised target day, and
the rules that decide when a new snapshot is worth keeping and how a prediction
is matched against reality.

It imports nothing from Home Assistant, so every rule here can be tested against
synthetic forecasts and synthetic history.

Why snapshots are change-triggered
----------------------------------

``build_forecast`` is a pure function of the stored day records, the reference
date and the target date. The in-progress day is excluded from its own forecast
(``collect_forecast_inputs`` keeps only ``0 < age <= horizon``), and the only
writer that touches a *past* day mid-run is the midnight close of the previous
day's last quarter. So between one midnight and the next, every refresh rebuilds
both forecasts from an unchanged input set and produces an identical array.

Persisting a snapshot per coordinator refresh would therefore write ninety-six
identical records a day. Persisting on a fixed schedule instead would be wrong
in the other direction: it would miss the one legitimate mid-day change --
retention pruning a day out of the look-back window when the first quarter of a
new day is created -- and it would have to be rewritten the moment a later phase
introduces an input that really does vary through the day.

A content fingerprint solves both. A snapshot is written when, and only when,
the forecast's *content and provenance* differ from the last snapshot kept for
that target. Under the Phase-1 model that yields exactly the two genuinely
distinct predictions per target day:

* **H-1**, issued while the target was "tomorrow";
* **H-0**, issued on the target day itself, after the model gained a learned day.

Volatile context -- the issuance timestamp, the confidence percentage, the
energy-balance score -- is recorded *on* the snapshot but deliberately excluded
from the fingerprint. The balance score is resampled every sixty seconds, so
including it would defeat the whole policy.

What a snapshot is not
----------------------

It is never the *adapted* Today figure. Same-day adaptation blends measured
energy into the remainder of the day, so the adapted total is a hybrid of
prediction and reality and is not a like-for-like prediction of anything. The
snapshot stores the unadapted baseline model forecast, which is the thing that
can honestly be scored against what the house went on to do.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .const import (
    FLAG_DEFINITION_CHANGED,
    FLAG_NO_RECORD,
    FLAG_SHAPE_MISMATCH,
    FLAG_TIMEZONE_CHANGED,
    FORECAST_KWH_PRECISION,
    FORECAST_MODEL_VERSION,
    FORECAST_WINDOW_WEIGHTS,
    FORECAST_WINDOWS,
    MIN_DAY_COMPLETENESS,
    MIN_DAYS_FOR_DAY_TYPE,
    MIN_OBSERVATIONS_PER_WINDOW,
    QUARTER_MINUTES,
    STATUS_FLEXIBLE_MISSING,
    STATUS_MEASURED_MISSING,
    STATUS_NOT_ELAPSED,
    STATUS_VALID,
)
from .forecast import DayForecast
from .storage import DayRecord, elapsed_quarters_for

#: Length of the stored fingerprint. Sixteen hex characters is sixty-four bits:
#: at the handful of snapshots a day this design produces, a collision is not a
#: reachable event, and the short form keeps the document readable.
_FINGERPRINT_CHARS = 16


def model_params_hash() -> str:
    """Return a stable hash of the constants that shape a forecast.

    Recorded on every snapshot. Without it, a future change to a window weight
    or an observation minimum would silently split the historical error series
    into two incomparable halves, and nothing in the record would say so -- a
    later phase would read the discontinuity as the household changing its
    behaviour.

    Deliberately *not* the storage schema version: the document format and the
    model can each change without the other.
    """
    payload = {
        "windows": list(FORECAST_WINDOWS),
        "weights": {str(k): v for k, v in sorted(FORECAST_WINDOW_WEIGHTS.items())},
        "min_observations": MIN_OBSERVATIONS_PER_WINDOW,
        "min_days_for_day_type": MIN_DAYS_FOR_DAY_TYPE,
        "min_day_completeness": MIN_DAY_COMPLETENESS,
        "quarter_minutes": QUARTER_MINUTES,
    }
    return _digest(payload)


def baseline_definition(ev_power_entity: str | None) -> str:
    """Return a fingerprint of what "baseline load" currently means.

    ``baseline = max(measured - flexible, 0)``, so configuring or removing a
    flexible-load source changes the *definition* of both the prediction and the
    actual -- mid-day, and with nothing in the Phase-1 history to mark it. A
    prediction made under one definition cannot honestly be scored against an
    actual measured under another, so the definition travels with the snapshot.
    """
    return f"ev:{ev_power_entity}" if ev_power_entity else "none"


def _digest(payload: Any) -> str:
    """Return a short, stable hash of a JSON-serialisable payload.

    ``sort_keys`` and the compact separators make the encoding canonical, and
    SHA-256 is used rather than :func:`hash` because the built-in is salted per
    process: a fingerprint computed before a restart would not match the same
    forecast computed after one, and every restart would write a duplicate
    snapshot.
    """
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:_FINGERPRINT_CHARS]


def _round_kwh(value: float | None) -> float | None:
    """Round a stored energy, preserving ``None``."""
    return None if value is None else round(value, FORECAST_KWH_PRECISION)


def _finite(value: Any) -> float | None:
    """Return a loaded number as a float, or ``None`` when it is not usable.

    Booleans are excluded because ``True`` is an ``int``, and ``NaN`` and the
    infinities are excluded because a damaged or hand-edited document can carry
    them and Python's ``json`` accepts the literals. A non-finite prediction or
    actual would travel through every metric into a sensor state while comparing
    false against every guard that might have caught it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mask_to_text(mask: list[bool]) -> str:
    """Encode a per-interval boolean mask as a compact bitstring."""
    return "".join("1" if flag else "0" for flag in mask)


def _mask_from_text(text: Any, count: int) -> list[bool]:
    """Decode a bitstring, padding or trimming to ``count`` entries."""
    if not isinstance(text, str):
        return [False] * count
    flags = [char == "1" for char in text[:count]]
    return flags + [False] * max(0, count - len(flags))


# -- context -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextProvider:
    """One namespaced, versioned block of forecast-time context.

    Context exists so a later phase can ask *why* a forecast was wrong rather
    than only how wrong it was. The obvious way to allow that -- an open
    dictionary -- becomes an unauditable dumping ground within two releases, so
    every block declares its key, its version and its exact field set, and
    anything undeclared is refused.

    Phase 2 registers exactly one provider. A later phase adds its own key
    without touching this one, and records written before it existed simply do
    not carry it.
    """

    key: str
    version: int
    fields: frozenset[str]

    def build(self, values: dict[str, Any]) -> dict[str, Any]:
        """Return the serialisable block, refusing anything undeclared."""
        unknown = set(values) - self.fields
        if unknown:
            raise ValueError(
                f"context provider {self.key!r} emitted undeclared fields: "
                f"{sorted(unknown)}"
            )
        return {"v": self.version, **values}


#: The load model behind every Phase-1 forecast. Every field is either already
#: published by ``DayForecast`` or by ``ConfidenceBreakdown``, so capturing them
#: costs nothing and invents nothing.
LOAD_MODEL_CONTEXT = ContextProvider(
    key="load_model",
    version=1,
    fields=frozenset(
        {
            "model_days",
            "usable_days",
            "learned_days",
            "day_type",
            "day_type_pooled",
            "windows_used",
            "modelled_intervals",
            "filled_intervals",
            "confidence_percent",
            "confidence",
        }
    ),
)

CONTEXT_PROVIDERS: tuple[ContextProvider, ...] = (LOAD_MODEL_CONTEXT,)


# -- issuance snapshot -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForecastSnapshot:
    """One immutable prediction, exactly as it stood when it was issued.

    Never mutated after creation, and never overwritten by a later issuance for
    the same target: a prediction that turned out to be wrong is evidence, not a
    mistake to be tidied away.
    """

    #: Absolute instant the forecast was issued.
    issued_at: datetime
    #: The civil day being predicted.
    target_day: date
    #: IANA zone in force at issuance. Stored, never re-inferred, so a later
    #: timezone change cannot silently reinterpret the interval identity.
    tz_key: str
    #: Real number of quarter-hours in the target civil day: 92, 96 or 100.
    interval_count: int
    #: Whole civil days between issuance and target. 0 = same day, 1 = day-ahead.
    horizon_days: int
    #: Whether the model published anything at all.
    available: bool
    #: Why it did not, when it did not. A withheld forecast is still recorded:
    #: otherwise a model that never spoke during its first month would look, to
    #: a later phase, like a model that was never wrong.
    unavailable_reason: str | None
    #: Predicted baseline kWh per chronological interval. Empty when withheld.
    predicted: tuple[float | None, ...]
    #: Per-interval fill provenance. ``True`` means the value was extrapolated
    #: from a neighbouring interval rather than blended from its own slot.
    filled: tuple[bool, ...]
    #: Content fingerprint. Two snapshots with equal fingerprints describe the
    #: same forecast and only one of them is kept.
    fingerprint: str
    model_version: int
    model_params: str
    #: What "baseline" meant at issuance. See :func:`baseline_definition`.
    baseline_definition: str
    #: Namespaced provenance blocks, keyed by provider.
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def ev_configured(self) -> bool:
        """Return whether a flexible load was configured at issuance."""
        return self.baseline_definition != "none"

    def predicted_at(self, index: int) -> float | None:
        """Return the prediction for one chronological interval, or ``None``."""
        if not self.available or not 0 <= index < len(self.predicted):
            return None
        return self.predicted[index]

    def total_kwh(self) -> float | None:
        """Return the predicted whole-day total, or ``None`` when withheld."""
        if not self.available:
            return None
        return round(
            sum(value for value in self.predicted if value is not None),
            FORECAST_KWH_PRECISION,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the compact serialisable form."""
        payload: dict[str, Any] = {
            "iat": self.issued_at.isoformat(),
            "tz": self.tz_key,
            "n": self.interval_count,
            "h": self.horizon_days,
            "av": self.available,
            "ur": self.unavailable_reason,
            "fp": self.fingerprint,
            "mv": self.model_version,
            "mp": self.model_params,
            "bd": self.baseline_definition,
            "ctx": self.context,
        }
        if self.available:
            # Omitted entirely when withheld. There is no array to store, and
            # writing zeros or nulls would be the one thing this project refuses
            # to do: make "we did not know" indistinguishable from a number.
            payload["p"] = [_round_kwh(value) for value in self.predicted]
            payload["f"] = _mask_to_text(list(self.filled))
        return payload

    @classmethod
    def from_dict(cls, target_day: date, raw: Any) -> ForecastSnapshot | None:
        """Rebuild a snapshot, or return ``None`` when the entry is unusable."""
        if not isinstance(raw, dict):
            return None
        try:
            issued_at = datetime.fromisoformat(str(raw["iat"]))
        except (KeyError, TypeError, ValueError):
            return None
        if issued_at.tzinfo is None:
            # A naive instant cannot be ordered against the rest of the history.
            return None

        count = raw.get("n")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            return None

        available = bool(raw.get("av"))
        predicted: tuple[float | None, ...] = ()
        filled: tuple[bool, ...] = ()
        if available:
            values = raw.get("p")
            if not isinstance(values, list):
                return None
            predicted = tuple(_finite(value) for value in values[:count])
            predicted = predicted + (None,) * max(0, count - len(predicted))
            filled = tuple(_mask_from_text(raw.get("f"), count))

        horizon = raw.get("h")
        context = raw.get("ctx")
        return cls(
            issued_at=issued_at,
            target_day=target_day,
            tz_key=str(raw.get("tz") or ""),
            interval_count=count,
            horizon_days=horizon if isinstance(horizon, int) else 0,
            available=available,
            unavailable_reason=(
                raw.get("ur") if isinstance(raw.get("ur"), str) else None
            ),
            predicted=predicted,
            filled=filled,
            fingerprint=str(raw.get("fp") or ""),
            model_version=(
                raw["mv"] if isinstance(raw.get("mv"), int) else FORECAST_MODEL_VERSION
            ),
            model_params=str(raw.get("mp") or ""),
            baseline_definition=str(raw.get("bd") or "none"),
            # Preserved verbatim rather than validated against the provider
            # registry. A document written by a newer release may carry context
            # this one has never heard of, and dropping it on load would make a
            # downgrade destructive.
            context=context if isinstance(context, dict) else {},
        )


def fingerprint_forecast(
    forecast: DayForecast,
    *,
    tz_key: str,
    horizon_days: int,
    model_version: int,
    model_params: str,
    baseline_def: str,
) -> str:
    """Return the content fingerprint of a forecast.

    Everything that makes two forecasts *materially different* is included;
    everything that merely moves with the clock is excluded. Getting that split
    wrong in either direction breaks the issuance policy: include the balance
    score and a snapshot is written every minute, omit the predicted values and
    a genuinely changed forecast is never recorded at all.

    The horizon is included, and that is a deliberate exception to "content
    only". A prediction made a day ahead and one made on the day are different
    observations even when they carry identical numbers, because the question
    they answer is different -- "how good is this model at 24 hours' notice"
    versus "at zero". Excluding it collapsed the two into one record whenever
    the model happened not to move, which is common on a settled household, and
    left the day-of bucket of any horizon comparison systematically empty of
    exactly the days the model found easy.

    It costs nothing in churn: the horizon of a fixed target is constant for a
    whole civil day, so this still yields the intended two snapshots per target
    and not one per refresh.
    """
    payload = {
        "day": forecast.day.isoformat(),
        "tz": tz_key,
        "h": horizon_days,
        "n": forecast.interval_count,
        "available": forecast.available,
        "reason": forecast.unavailable_reason,
        # Rounded exactly as the values are stored, so a forecast cannot
        # fingerprint differently from the one that was persisted from it.
        "p": (
            [_round_kwh(value) for value in forecast.intervals]
            if forecast.available
            else None
        ),
        "f": _mask_to_text(forecast.filled) if forecast.available else None,
        "model_days": forecast.source_days,
        "usable_days": forecast.usable_days,
        "day_type": forecast.day_type,
        "pooled": forecast.day_type_pooled,
        "windows": list(forecast.windows_used),
        "modelled": forecast.modelled_intervals,
        "filled": forecast.filled_intervals,
        "mv": model_version,
        "mp": model_params,
        "bd": baseline_def,
    }
    return _digest(payload)


def build_snapshot(
    forecast: DayForecast,
    *,
    issued_at: datetime,
    issuance_day: date,
    tz_key: str,
    learned_days: int,
    confidence_percent: float | None,
    confidence: dict[str, Any] | None,
    ev_power_entity: str | None,
) -> ForecastSnapshot:
    """Capture one forecast as an immutable snapshot.

    The arrays are copied, not referenced. ``DayForecast`` is a mutable
    dataclass that the coordinator rebuilds on every refresh; holding a
    reference would let a later refresh rewrite evidence that is supposed to be
    frozen at the moment of issuance.
    """
    baseline_def = baseline_definition(ev_power_entity)
    params = model_params_hash()
    context = {
        LOAD_MODEL_CONTEXT.key: LOAD_MODEL_CONTEXT.build(
            {
                "model_days": forecast.source_days,
                "usable_days": forecast.usable_days,
                "learned_days": learned_days,
                "day_type": forecast.day_type,
                "day_type_pooled": forecast.day_type_pooled,
                "windows_used": list(forecast.windows_used),
                "modelled_intervals": forecast.modelled_intervals,
                "filled_intervals": forecast.filled_intervals,
                "confidence_percent": (
                    None if confidence_percent is None else round(confidence_percent, 1)
                ),
                "confidence": dict(confidence) if confidence else None,
            }
        )
    }
    horizon_days = (forecast.day - issuance_day).days
    return ForecastSnapshot(
        issued_at=issued_at,
        target_day=forecast.day,
        tz_key=tz_key,
        interval_count=forecast.interval_count,
        horizon_days=horizon_days,
        available=forecast.available,
        unavailable_reason=forecast.unavailable_reason,
        predicted=(
            tuple(_round_kwh(value) for value in forecast.intervals)
            if forecast.available
            else ()
        ),
        filled=tuple(forecast.filled) if forecast.available else (),
        fingerprint=fingerprint_forecast(
            forecast,
            tz_key=tz_key,
            horizon_days=horizon_days,
            model_version=FORECAST_MODEL_VERSION,
            model_params=params,
            baseline_def=baseline_def,
        ),
        model_version=FORECAST_MODEL_VERSION,
        model_params=params,
        baseline_definition=baseline_def,
        context=context,
    )


# -- day outcome -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayOutcome:
    """What actually happened on a target day, matched to its predictions.

    Written once, when the day can no longer gain intervals, and then immutable.
    The actual values are *copied* rather than referenced back into the learning
    history on purpose: that history is pruned at its own retention horizon and
    is discarded outright by a schema migration, and forecast evidence that
    silently loses its other half is not evidence.
    """

    target_day: date
    #: Absolute instant the day was finalised.
    finalized_at: datetime
    #: The zone the day's *record* was written in, which is what its interval
    #: identity is expressed in.
    tz_key: str
    interval_count: int
    #: Measured baseline kWh per chronological interval; ``None`` where there is
    #: no trustworthy observation. Never zero-filled.
    actual: tuple[float | None, ...]
    #: One status character per interval; see the ``STATUS_*`` constants.
    status: str
    #: The day's flexible-load total, as context for why a day was unusual.
    flexible_total_kwh: float | None
    #: Reasons this day may not enter a derived metric. Empty is the normal case.
    flags: tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        """Return whether this day may contribute to error statistics."""
        return not self.flags

    def valid_indices(self) -> list[int]:
        """Return the chronological indices carrying a trustworthy actual."""
        return [
            index
            for index, code in enumerate(self.status[: self.interval_count])
            if code == STATUS_VALID
        ]

    def to_dict(self) -> dict[str, Any]:
        """Return the compact serialisable form."""
        return {
            "fin": self.finalized_at.isoformat(),
            "tz": self.tz_key,
            "n": self.interval_count,
            "a": [_round_kwh(value) for value in self.actual],
            "s": self.status,
            "ev": _round_kwh(self.flexible_total_kwh),
            "fl": list(self.flags),
        }

    @classmethod
    def from_dict(cls, target_day: date, raw: Any) -> DayOutcome | None:
        """Rebuild an outcome, or return ``None`` when the entry is unusable."""
        if not isinstance(raw, dict):
            return None
        try:
            finalized_at = datetime.fromisoformat(str(raw["fin"]))
        except (KeyError, TypeError, ValueError):
            return None
        if finalized_at.tzinfo is None:
            return None

        count = raw.get("n")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            return None

        values = raw.get("a")
        actual: tuple[float | None, ...] = ()
        if isinstance(values, list):
            actual = tuple(_finite(value) for value in values[:count])
        actual = actual + (None,) * max(0, count - len(actual))

        status = raw.get("s")
        if not isinstance(status, str):
            status = STATUS_MEASURED_MISSING * count
        status = status[:count].ljust(count, STATUS_MEASURED_MISSING)

        # A stored status that claims an interval is valid while the value
        # beside it is missing would let a fabricated zero in through the back
        # door. The two are reconciled on load, in the safe direction.
        status = "".join(
            code
            if not (code == STATUS_VALID and actual[index] is None)
            else STATUS_MEASURED_MISSING
            for index, code in enumerate(status)
        )

        flags = raw.get("fl")
        ev = raw.get("ev")
        return cls(
            target_day=target_day,
            finalized_at=finalized_at,
            tz_key=str(raw.get("tz") or ""),
            interval_count=count,
            actual=actual,
            status=status,
            flexible_total_kwh=_finite(ev),
            flags=tuple(str(flag) for flag in flags) if isinstance(flags, list) else (),
        )


def build_outcome(
    target_day: date,
    record: DayRecord | None,
    snapshots: list[ForecastSnapshot],
    *,
    finalized_at: datetime,
    fallback_tz_key: str,
    fallback_interval_count: int,
) -> DayOutcome:
    """Match a finished target day against the predictions made for it.

    Pure and total: it fabricates nothing, and it always returns an outcome --
    an absent day is itself evidence, and refusing to record one would leave the
    snapshot dangling forever.

    ``record`` is the Phase-1 day record. The actual is
    :meth:`DayRecord.baseline_at`, which is ``max(measured - flexible, 0)`` and
    ``None`` when either half is untrustworthy. That is deliberately the same
    quantity the model predicts: comparing a baseline forecast against raw
    measured load would score the model on energy it was never asked to predict.
    """
    if record is None:
        count = fallback_interval_count
        return DayOutcome(
            target_day=target_day,
            finalized_at=finalized_at,
            tz_key=fallback_tz_key,
            interval_count=count,
            actual=(None,) * count,
            status=STATUS_MEASURED_MISSING * count,
            flexible_total_kwh=None,
            flags=(FLAG_NO_RECORD,),
        )

    count = record.interval_count
    elapsed = elapsed_quarters_for(target_day, record.tz, finalized_at)

    actual: list[float | None] = []
    status: list[str] = []
    for index in range(count):
        value = record.baseline_at(index)
        if value is not None:
            actual.append(value)
            status.append(STATUS_VALID)
            continue
        actual.append(None)
        if index >= elapsed:
            # Only reachable if the clock moved backwards across a
            # finalisation. Named rather than reported as a measurement gap.
            status.append(STATUS_NOT_ELAPSED)
        elif record.measured[index] is not None:
            status.append(STATUS_FLEXIBLE_MISSING)
        else:
            status.append(STATUS_MEASURED_MISSING)

    flags: list[str] = []
    for snapshot in snapshots:
        if snapshot.interval_count != count and FLAG_SHAPE_MISMATCH not in flags:
            # Two different day shapes cannot be matched by index: doing so
            # would line an 18:00 prediction up against a 17:00 measurement and
            # look entirely plausible while doing it.
            flags.append(FLAG_SHAPE_MISMATCH)
        if (
            snapshot.tz_key
            and snapshot.tz_key != record.tz_key
            and FLAG_TIMEZONE_CHANGED not in flags
        ):
            flags.append(FLAG_TIMEZONE_CHANGED)

    if _definition_changed(record, snapshots):
        flags.append(FLAG_DEFINITION_CHANGED)

    return DayOutcome(
        target_day=target_day,
        finalized_at=finalized_at,
        tz_key=record.tz_key,
        interval_count=count,
        actual=tuple(actual),
        status="".join(status),
        flexible_total_kwh=record.ev_total_kwh if any(record.ev_expected) else None,
        flags=tuple(flags),
    )


def _definition_changed(record: DayRecord, snapshots: list[ForecastSnapshot]) -> bool:
    """Return whether "baseline" meant the same thing all day and at issuance.

    Judged from the evidence rather than from the current configuration: the
    record's own ``ev_expected`` flags say what was actually expected of each
    interval, so a flexible load switched on at noon shows up here even though
    the configuration looks perfectly consistent by the time the day is
    finalised.

    **Only intervals the integration actually observed have an opinion.**
    ``ev_expected`` is written by ``DayRecord.record_interval``, and the
    coordinator calls that exactly once per *accepted* quarter -- so an interval
    that never reached coverage, or that fell in a Home Assistant restart, keeps
    the ``False`` its list was padded with. Reading those padded entries as
    "no flexible load was configured then" is what made a single missing quarter
    on a day *with* a charger look like the definition changing at that quarter:
    ``any(expected) and not all(expected)`` was true, the whole day was excluded
    from every statistic, and the exclusion was permanent.

    That is a data *gap*, which the per-interval status codes already describe
    exactly, and it says nothing at all about what "baseline" meant. So the
    judgement is made over the observed intervals only, and a day with no
    observations at all makes no claim: it has no comparable interval either
    way, and inventing a definition change on top of that would be an invention.
    """
    observed = [
        record.ev_expected[index]
        for index in range(record.interval_count)
        if record.measured[index] is not None
    ]
    if not observed:
        return False
    if any(observed) and not all(observed):
        return True
    day_configured = any(observed)
    return any(snapshot.ev_configured != day_configured for snapshot in snapshots)


# -- lifecycle ---------------------------------------------------------------

#: A target day that has snapshots but has not yet been finalised, and cannot be
#: because it is today or later.
LIFECYCLE_PENDING = "pending"
#: Finalised, comparable, and carrying at least one interval that can be scored.
LIFECYCLE_VALIDATED = "validated"
#: Finalised, but nothing survives to compare: every actual was missing, or the
#: day is flagged as incomparable.
LIFECYCLE_UNMATCHED = "unmatched"
#: In the past, with snapshots, and still not finalised. Normally transient --
#: it is what finalisation being suspended looks like from outside.
LIFECYCLE_UNRESOLVED = "unresolved"


def lifecycle_state(
    target_day: date,
    today: date,
    *,
    finalized: bool,
    comparable: bool,
    has_valid_interval: bool,
) -> str:
    """Return the derived lifecycle state from the four facts that decide it.

    Derived, never stored. The only state committed to disk is ``finalized_at``
    on the outcome; everything else is a reading of the facts beside it. A
    stored state field would be a second source of truth, and the first time the
    two disagreed it would be the stored one that got believed -- a record
    labelled ``validated`` whose actual is null.

    The same reasoning applies to this function. Both callers -- the one holding
    a full outcome and the one holding only an index row -- come through here,
    so a diagnostics count and a scored day can never tell different stories
    about the same target.
    """
    if not finalized:
        return LIFECYCLE_PENDING if target_day >= today else LIFECYCLE_UNRESOLVED
    if comparable and has_valid_interval:
        return LIFECYCLE_VALIDATED
    return LIFECYCLE_UNMATCHED


def lifecycle_from_summary(
    target_day: date,
    today: date,
    *,
    finalized: bool,
    summary: dict[str, Any] | None,
) -> str:
    """Return the lifecycle state of a target day from its index summary row.

    Lets the counts be produced without loading a single month partition, while
    still going through :func:`lifecycle_state` so they cannot drift from the
    outcome-based reading.
    """
    facts = summary or {}
    flags = facts.get("fg")
    compared = facts.get("c")
    return lifecycle_state(
        target_day,
        today,
        finalized=finalized,
        comparable=not (isinstance(flags, list) and flags),
        has_valid_interval=bool(compared),
    )
