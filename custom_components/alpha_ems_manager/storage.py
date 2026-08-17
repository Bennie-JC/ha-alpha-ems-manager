"""Persistent learning history for Alpha EMS Manager.

History lives in Home Assistant's :class:`~homeassistant.helpers.storage.Store`
(a JSON document under ``.storage/``), never in entity state or entity
attributes. A year of quarter-hour buckets is far too much data to put in front
of the recorder.

Interval identity
-----------------

A day is stored as a list of intervals in **chronological order**, one entry per
quarter-hour that the local civil day actually contains: 92 on a spring-forward
day, 96 normally, 100 on a fall-back day. Interval ``i`` begins at

    utc_midnight(day) + i * 15 minutes

which is an absolute instant, so every real quarter has a distinct, ordered
identity. The *behavioural* wall-clock slot (0..95) is derived from that instant
rather than used as the key.

This distinction matters exactly twice a year. On a fall-back day, chronological
intervals 8-11 and 12-15 both map to behavioural slots 8-11 -- the repeated
02:00-02:59 hour. Both are retained and both contribute to the statistical
sample for those slots. An earlier design keyed storage on the behavioural slot
alone, which silently overwrote the first occurrence and lost an hour of energy
from the profile.

Measured, flexible and baseline
-------------------------------

Three quantities are tracked per interval:

``measured``
    Total household consumption as reported by the house-load source. Ground
    truth; always stored when the interval had enough coverage.
``ev``
    Energy drawn by the configured flexible load (EV charging). ``None`` means
    no usable measurement for that interval.
``baseline``
    ``max(measured - ev, 0)`` -- the household demand Alpha EMS does not expect
    to be able to schedule. Derived, never stored, so the relationship stays
    auditable and reversible.

Baseline is only *valid* when the measured value exists and, if a flexible load
is configured, its value exists too. A missing EV reading therefore invalidates
the baseline for that interval without discarding the measured ground truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    BALANCE_SAMPLE_WINDOW,
    DAY_TYPE_WEEKDAY,
    DAY_TYPE_WEEKEND,
    MAX_HISTORY_DAYS,
    MIN_DAY_COMPLETENESS,
    QUARTER_MINUTES,
    SLOTS_PER_DAY,
    STORAGE_KEY_TEMPLATE,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    STORE_SAVE_DELAY,
)

_LOGGER = logging.getLogger(__name__)

#: Rounding applied to stored energies. Four decimals of a kWh is 0.1 Wh, well
#: below the noise floor of any household power sensor.
_KWH_PRECISION = 4

_QUARTER = timedelta(minutes=QUARTER_MINUTES)


def day_type_of(day: date) -> str:
    """Return the day type of ``day``.

    Monday-Friday and Saturday-Sunday are the two buckets. A full day-of-week
    model needs far more history than a household installation accumulates in
    its first season, and overfits badly before then.
    """
    return DAY_TYPE_WEEKEND if day.weekday() >= 5 else DAY_TYPE_WEEKDAY


def utc_midnight(day: date, tz: Any) -> datetime:
    """Return the absolute instant at which the local civil day ``day`` begins."""
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)


def expected_quarters_for(day: date, tz: Any) -> int:
    """Return how many quarter-hours the local civil day ``day`` contains.

    Normally 96. A spring-forward day has 92 and a fall-back day 100, which is
    why this is computed from real timezone arithmetic rather than assumed.

    Both midnights are converted to UTC before subtracting. CPython
    short-circuits arithmetic between two aware datetimes that share a tzinfo
    object into naive wall-clock arithmetic, never calling ``utcoffset()``,
    which would report a flat 96 on every day of the year.
    """
    start = utc_midnight(day, tz)
    end = utc_midnight(day + timedelta(days=1), tz)
    return max(1, round((end - start).total_seconds() / 900.0))


def interval_start_utc(day: date, index: int, tz: Any) -> datetime:
    """Return the absolute start of chronological interval ``index`` of ``day``."""
    return utc_midnight(day, tz) + index * _QUARTER


def local_slot_for_index(day: date, index: int, tz: Any) -> int:
    """Return the behavioural wall-clock slot (0..95) of a chronological index.

    Two different indices can share a slot on a fall-back day. That is the
    point: household behaviour repeats with the wall clock even though the
    intervals are distinct instants.
    """
    local = interval_start_utc(day, index, tz).astimezone(tz)
    return local.hour * 4 + local.minute // QUARTER_MINUTES


def index_for_start_utc(day: date, start: datetime, tz: Any) -> int:
    """Return the chronological index of an interval beginning at ``start``."""
    delta = start.astimezone(UTC) - utc_midnight(day, tz)
    return round(delta.total_seconds() / (QUARTER_MINUTES * 60))


@dataclass(slots=True)
class DayRecord:
    """One calendar day of learned household load, in chronological intervals."""

    day: date
    #: IANA key of the timezone the day was recorded in, stored explicitly so a
    #: later timezone change cannot silently reinterpret existing history.
    tz_key: str
    #: Number of quarter-hours this civil day actually contains (92/96/100).
    interval_count: int = SLOTS_PER_DAY
    #: Measured household energy per chronological interval; ``None`` where the
    #: interval was missing or too incomplete to trust.
    measured: list[float | None] = field(default_factory=list)
    #: Flexible-load (EV) energy per interval; ``None`` where no usable reading.
    ev: list[float | None] = field(default_factory=list)
    #: Whether a flexible-load source was configured for that interval.
    ev_expected: list[bool] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Size the parallel lists to the day's real interval count."""
        self._resize()

    def _resize(self) -> None:
        """Pad or trim every parallel list to ``interval_count``."""
        count = max(1, self.interval_count)
        self.interval_count = count
        for name, filler in (
            ("measured", None),
            ("ev", None),
            ("ev_expected", False),
        ):
            values = list(getattr(self, name))
            if len(values) < count:
                values.extend([filler] * (count - len(values)))
            setattr(self, name, values[:count])

    # -- derived values --------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        """Return the timezone this day was recorded in."""
        return ZoneInfo(self.tz_key)

    @property
    def day_type(self) -> str:
        """Return ``weekday`` or ``weekend``."""
        return day_type_of(self.day)

    def local_slot(self, index: int) -> int:
        """Return the behavioural wall-clock slot of a chronological interval."""
        return local_slot_for_index(self.day, index, self.tz)

    def baseline_at(self, index: int) -> float | None:
        """Return the baseline energy of one interval, or ``None`` if invalid.

        Invalid means either no measured reading, or a configured flexible load
        with no usable reading for that interval. Guessing zero for a missing EV
        sample would quietly fold a charging session into the baseline.
        """
        measured = self.measured[index]
        if measured is None:
            return None
        flexible = self.ev[index]
        if self.ev_expected[index] and flexible is None:
            return None
        return max(measured - (flexible or 0.0), 0.0)

    @property
    def measured_valid_count(self) -> int:
        """Return how many intervals carry a usable measured reading."""
        return sum(1 for value in self.measured if value is not None)

    @property
    def baseline_valid_count(self) -> int:
        """Return how many intervals carry a usable baseline value."""
        return sum(
            1
            for index in range(self.interval_count)
            if self.baseline_at(index) is not None
        )

    @property
    def measured_total_kwh(self) -> float:
        """Return the day's measured household energy."""
        return round(sum(value for value in self.measured if value is not None), 4)

    @property
    def ev_total_kwh(self) -> float:
        """Return the day's measured flexible-load energy."""
        return round(sum(value for value in self.ev if value is not None), 4)

    @property
    def baseline_total_kwh(self) -> float:
        """Return the day's baseline energy across valid intervals."""
        total = 0.0
        for index in range(self.interval_count):
            value = self.baseline_at(index)
            if value is not None:
                total += value
        return round(total, 4)

    @property
    def measured_completeness(self) -> float:
        """Return the fraction of real intervals with a measured reading."""
        return min(1.0, self.measured_valid_count / self.interval_count)

    @property
    def completeness(self) -> float:
        """Return the fraction of real intervals with a valid baseline."""
        return min(1.0, self.baseline_valid_count / self.interval_count)

    @property
    def is_learned(self) -> bool:
        """Return whether this day has enough valid baseline to be learned."""
        return self.completeness >= MIN_DAY_COMPLETENESS

    # -- mutation --------------------------------------------------------

    def record_interval(
        self,
        index: int,
        measured_kwh: float | None,
        ev_kwh: float | None,
        ev_expected: bool,
    ) -> None:
        """Store one finalised interval by chronological index."""
        if not 0 <= index < self.interval_count:
            return
        if measured_kwh is not None:
            self.measured[index] = round(measured_kwh, _KWH_PRECISION)
        if ev_kwh is not None:
            self.ev[index] = round(ev_kwh, _KWH_PRECISION)
        self.ev_expected[index] = ev_expected

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the compact serialisable form.

        The flexible-load arrays are omitted entirely when no interval expected
        one, which keeps the document small for installations without an EV.
        """
        payload: dict[str, Any] = {
            "tz": self.tz_key,
            "n": self.interval_count,
            "m": self.measured,
        }
        if any(self.ev_expected):
            payload["e"] = self.ev
            payload["x"] = [1 if flag else 0 for flag in self.ev_expected]
        return payload

    @classmethod
    def from_dict(
        cls, day: date, raw: dict[str, Any], fallback_tz_key: str
    ) -> DayRecord | None:
        """Rebuild a record, or return ``None`` when the entry is unusable."""
        measured_raw = raw.get("m")
        if not isinstance(measured_raw, list):
            return None

        # ``n`` is the day's real interval count and must be taken at face value.
        # Falling back to ``len(m)`` on a falsy or missing ``n`` redefined the
        # day's length to whatever survived in the array, so a truncated document
        # came back as a short but *fully covered* day: ``{"m": [0.1], "n": 0}``
        # loaded as a one-interval day at 100 % completeness, counted as learned,
        # and inflated both the learned-day count and the confidence score. A
        # damaged day must be discarded, not silently reinterpreted.
        raw_count = raw.get("n")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool):
            return None
        # Bounded to keep a corrupt or hostile ``n`` from allocating three huge
        # lists. Twice the nominal day length comfortably covers every real
        # value (92, 96 or 100) with room for a future sub-quarter resolution.
        if not 1 <= raw_count <= 2 * SLOTS_PER_DAY:
            return None
        count = raw_count

        # The stored zone must actually resolve. ``DayRecord.tz`` builds a
        # ``ZoneInfo`` from it on every forecast, so an unresolvable key -- a
        # renamed or hand-edited zone -- raised ``ZoneInfoNotFoundError`` out of
        # ``build_forecast``, failed every coordinator refresh and left all four
        # sensors permanently unavailable with no way back.
        tz_key = raw.get("tz")
        if not isinstance(tz_key, str) or not tz_key:
            tz_key = fallback_tz_key
        else:
            try:
                ZoneInfo(tz_key)
            except (ZoneInfoNotFoundError, ValueError):
                tz_key = fallback_tz_key

        record = cls(day=day, tz_key=tz_key, interval_count=count)
        record.measured = _numeric_list(measured_raw, count)
        record.ev = _numeric_list(raw.get("e"), count)
        flags_raw = raw.get("x")
        if isinstance(flags_raw, list):
            record.ev_expected = [bool(flag) for flag in flags_raw[:count]] + [
                False
            ] * max(0, count - len(flags_raw))
        else:
            record.ev_expected = [False] * count
        return record


def _numeric_list(raw: Any, count: int) -> list[float | None]:
    """Return ``count`` optional floats, dropping anything non-numeric.

    A corrupted entry becomes ``None`` -- missing data -- rather than zero.
    """
    values: list[float | None] = [None] * count
    if not isinstance(raw, list):
        return values
    for index, value in enumerate(raw[:count]):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values[index] = float(value)
    return values


@dataclass(slots=True)
class BalanceStats:
    """Rolling tally of the optional energy-balance sanity check."""

    ok_samples: int = 0
    total_samples: int = 0

    @property
    def score(self) -> float | None:
        """Return the pass rate, or ``None`` when nothing has been sampled."""
        if self.total_samples <= 0:
            return None
        return self.ok_samples / self.total_samples

    def record(self, within_tolerance: bool) -> None:
        """Record one balance observation.

        Once the window fills, both counters halve. That keeps the score a
        rolling view of recent data quality instead of an all-time average that
        a long-fixed wiring mistake would weigh down forever.
        """
        self.ok_samples += 1 if within_tolerance else 0
        self.total_samples += 1
        if self.total_samples > BALANCE_SAMPLE_WINDOW:
            self.ok_samples //= 2
            self.total_samples //= 2

    def to_dict(self) -> dict[str, int]:
        """Return the serialisable form."""
        return {"ok": self.ok_samples, "total": self.total_samples}

    @classmethod
    def from_dict(cls, raw: Any) -> BalanceStats:
        """Rebuild from stored data, tolerating anything unexpected."""
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls(
                ok_samples=max(0, int(raw.get("ok") or 0)),
                total_samples=max(0, int(raw.get("total") or 0)),
            )
        except (TypeError, ValueError):
            return cls()


class _LearningStoreBackend(Store[dict[str, Any]]):
    """Store subclass that refuses to misread an incompatible older schema."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Discard pre-v2 documents rather than reinterpreting them.

        Schema v1 stored a fixed 96-entry list keyed by wall-clock slot, which
        cannot express a 100-quarter fall-back day and carried no flexible-load
        information. There is no faithful mapping onto v2, and silently reading
        v1 arrays as chronological intervals would corrupt every DST day. Only
        this integration's own document is affected; nothing else in
        ``.storage`` is touched.
        """
        if old_major_version < 2:
            _LOGGER.warning(
                "Discarding Alpha EMS Manager learning history written under "
                "storage schema v%s: the v%s schema stores quarter-hour "
                "intervals chronologically so that daylight-saving days are "
                "represented exactly, and the older wall-clock-slot format "
                "cannot be converted to it. Learning restarts from zero; no "
                "other Home Assistant storage is affected",
                old_major_version,
                STORAGE_VERSION,
            )
            return {"days": {}, "balance": {}, "last_finalized": None}
        return old_data


class LearningStore:
    """Loads, prunes and persists the learning history for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise a per-entry store.

        The storage key embeds ``entry_id``, so two Alpha EMS instances in one
        Home Assistant never share learning state.
        """
        self._store: Store[dict[str, Any]] = _LearningStoreBackend(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_TEMPLATE.format(entry_id=entry_id),
            minor_version=STORAGE_MINOR_VERSION,
        )
        self.days: dict[date, DayRecord] = {}
        self.balance = BalanceStats()
        self.last_finalized: str | None = None
        self.corrupt = False
        self.reset_by_migration = False

    async def async_load(self, fallback_tz_key: str) -> None:
        """Load history from disk.

        Corrupt or unreadable storage degrades to an empty history rather than
        failing setup: losing learned days is recoverable, refusing to start is
        much less pleasant for the user.
        """
        try:
            raw = await self._store.async_load()
        except Exception:
            _LOGGER.warning(
                "Learning history could not be read and is being started over; "
                "previously learned days are lost"
            )
            self.corrupt = True
            return

        if not isinstance(raw, dict):
            return

        days_raw = raw.get("days")
        if isinstance(days_raw, dict):
            for key, value in days_raw.items():
                if not isinstance(value, dict):
                    continue
                try:
                    day = date.fromisoformat(key)
                except (TypeError, ValueError):
                    continue
                record = DayRecord.from_dict(day, value, fallback_tz_key)
                if record is not None:
                    self.days[day] = record

        self.balance = BalanceStats.from_dict(raw.get("balance"))
        last = raw.get("last_finalized")
        self.last_finalized = last if isinstance(last, str) else None

    def to_dict(self) -> dict[str, Any]:
        """Return the full serialisable document."""
        return {
            "days": {
                day.isoformat(): record.to_dict()
                for day, record in sorted(self.days.items())
            },
            "balance": self.balance.to_dict(),
            "last_finalized": self.last_finalized,
        }

    def schedule_save(self) -> None:
        """Queue a debounced write.

        Quarters finalise every fifteen minutes, so batching writes behind a
        short delay keeps disk churn negligible without risking real data loss.
        """
        self._store.async_delay_save(self.to_dict, STORE_SAVE_DELAY)

    async def async_save_now(self) -> None:
        """Write immediately. Used on unload and on Home Assistant stop."""
        await self._store.async_save(self.to_dict())

    async def async_remove(self) -> None:
        """Delete this entry's document from disk.

        Used when the config entry is removed. The in-memory state is cleared too
        so a stray later save cannot resurrect the file.
        """
        self.days.clear()
        self.balance = BalanceStats()
        self.last_finalized = None
        await self._store.async_remove()

    def get_or_create(self, day: date, tz: Any) -> DayRecord:
        """Return the record for ``day``, creating and pruning as needed."""
        record = self.days.get(day)
        if record is None:
            record = DayRecord(
                day=day,
                tz_key=str(tz),
                interval_count=expected_quarters_for(day, tz),
            )
            self.days[day] = record
            self.prune(reference=day)
        return record

    def prune(self, reference: date) -> int:
        """Drop days older than the retention window. Returns the count removed.

        ``reference`` is clamped to just past the newest day already stored. A
        single forward clock excursion -- a Pi that hands Home Assistant a date
        years ahead before NTP corrects it -- would otherwise create a
        future-dated record, prune the entire retention window against it and
        delete every learned day. Backwards jumps are harmless, since an older
        reference only prunes less.

        A genuine multi-day gap still prunes correctly: the clamp advances with
        whatever the newest stored day is, so history is trimmed relative to real
        recorded time rather than to whatever the clock momentarily claimed.
        """
        if self.days:
            newest = max(self.days)
            if reference > newest:
                reference = newest
        cutoff = reference - timedelta(days=MAX_HISTORY_DAYS - 1)
        stale = [day for day in self.days if day < cutoff]
        for day in stale:
            del self.days[day]
        return len(stale)

    # -- summaries -------------------------------------------------------

    def learned_days(self, before: date | None = None) -> list[DayRecord]:
        """Return complete-enough days, oldest first.

        ``before`` excludes the in-progress day: a day is only judged once it
        can no longer gain intervals.
        """
        return [
            record
            for day, record in sorted(self.days.items())
            if record.is_learned and (before is None or day < before)
        ]

    @property
    def span(self) -> tuple[date | None, date | None]:
        """Return the ``(oldest, newest)`` stored dates."""
        if not self.days:
            return None, None
        ordered = sorted(self.days)
        return ordered[0], ordered[-1]

    @property
    def retained_intervals(self) -> int:
        """Return how many real intervals are retained across all days."""
        return sum(record.interval_count for record in self.days.values())
