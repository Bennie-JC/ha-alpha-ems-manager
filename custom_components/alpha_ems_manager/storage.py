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
import math
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
    MAX_CAMPAIGN_LIFECYCLE_REMEMBERED,
    MAX_DECISION_RECORDS_RETAINED,
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
#: Decimal places for a stored state of charge. The source reports whole
#: percent, so one decimal is already generous; four would advertise a precision
#: the sensor does not have.
_SOC_PRECISION = 1

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


def elapsed_quarters_for(day: date, tz: Any, now: datetime) -> int:
    """Return how many of ``day``'s quarter-hours have fully elapsed at ``now``.

    This is the denominator for any *so-far* coverage figure. A day still in
    progress has not had the chance to record its evening, so measuring it
    against the full civil day reports a fault where there is only a future:
    at 06:00 a perfectly healthy installation would show 25 % coverage.

    The three cases fall out of the arithmetic rather than needing a branch:

    * a **past** day returns its full civil-day length, so a finalised day is
      still judged against every interval it really had;
    * a **future** day returns ``0``;
    * the **current** day returns the intervals that have actually closed.

    Daylight saving is handled by construction. The elapsed count is absolute
    time since the day's UTC midnight, and the clamp is
    :func:`expected_quarters_for`, so a spring-forward day saturates at 92 and a
    fall-back day is allowed all 100 -- neither is a hard-coded 96, and the
    repeated hour advances the count instead of rewinding it.
    """
    total = expected_quarters_for(day, tz)
    elapsed = (now.astimezone(UTC) - utc_midnight(day, tz)).total_seconds()
    return max(0, min(total, int(elapsed // (QUARTER_MINUTES * 60))))


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
    #: Battery state of charge in percent at the **end** of each interval, or
    #: ``None`` where there was no usable reading.
    #:
    #: Additive evidence, and evidence only. Nothing in the learning or forecast
    #: path reads it: it does not affect ``baseline_at``, ``completeness``,
    #: ``is_learned``, the forecast, the confidence score or any Phase-2 figure,
    #: and ``test_soc_persistence.py`` pins every one of those. A day with no
    #: state-of-charge data is exactly as learnable as it was before this field
    #: existed.
    #:
    #: It is recorded because it is the one physical observation a battery plan
    #: depends on that cannot be reconstructed from anything else. A prediction
    #: can be recomputed from the stored forecast and the stored configuration;
    #: where the battery actually was at 03:15 last Tuesday cannot. Every day
    #: without it is a day the physical model can never be checked against.
    #:
    #: Sampled rather than integrated, because a state of charge is a level and
    #: not a flow: it does not pass through ``QuarterAccumulator``. When several
    #: quarters close together after a restart, only the one that just ended
    #: takes the sample -- the others genuinely are not known.
    soc: list[float | None] = field(default_factory=list)
    #: Measured photovoltaic generation per chronological interval, in kWh, or
    #: ``None`` where there was no usable reading.
    #:
    #: Additive evidence on exactly the same terms as ``soc``: nothing in the
    #: learning or forecast path reads it. It does not affect ``baseline_at``,
    #: ``completeness``, ``is_learned``, the forecast, the confidence score or any
    #: Phase-2 figure, and ``test_pv_independence.py`` -- which predates this
    #: field and passes unmodified beside it -- pins the reason why: if a sunny
    #: day taught the model that the house consumes less simply because the panels
    #: supplied the energy, every later decision would be built on that lie.
    #:
    #: It is recorded because a PV forecast is worth nothing without something to
    #: check it against, and generation actually observed at 13:15 last Tuesday
    #: cannot be reconstructed from anything else. Integrated rather than sampled,
    #: because generation is a flow: it goes through ``QuarterAccumulator`` like
    #: house load and the flexible load, and is subject to the same coverage
    #: threshold, so a partially observed interval is missing rather than short.
    pv: list[float | None] = field(default_factory=list)
    #: Measured grid import and export per chronological interval, in kWh, or
    #: ``None`` where there was no usable reading.
    #:
    #: Additive evidence on exactly the same terms as ``soc`` and ``pv``: nothing
    #: in the learning, forecast, reserve or economic path reads either array.
    #: ``test_economic_evidence.py`` pins that, and it matters more here than for
    #: the two before it -- an optimizer that learned from its own recorded
    #: outcomes would be Phase 9 wearing Phase 8's clothes.
    #:
    #: They are recorded because **what a plan actually cost is irrecoverable
    #: afterwards.** Phase 8 can compute what a plan *should* cost from prices it
    #: has, but the realised flows at 18:15 last Tuesday exist nowhere else, and
    #: every day without them is a day whose economics can never be reconstructed.
    #: Integrated rather than sampled, because both are flows: they go through
    #: ``QuarterAccumulator`` like house load, the flexible load and generation,
    #: and are subject to the same coverage threshold.
    grid_import: list[float | None] = field(default_factory=list)
    grid_export: list[float | None] = field(default_factory=list)
    #: What the energy this civil day opened with was worth, **on the value curve
    #: that existed then**. Written once per day and never revised. beta.39.
    #:
    #: Not a parallel array: a revaluation only ever needs the day's boundary, and
    #: one scalar per day is what makes it survive a gap. Decorating the bounded
    #: decision records instead would have stopped answering after roughly
    #: forty-eight hours, which is when a reader most wants it.
    #:
    #: **It is the one datum nothing retained, and that was a finding rather than
    #: an oversight.** ``EconomicSnapshot`` keeps the marginal value in EUR/kWh and
    #: the stored energy in kWh, and multiplying them looks like the position
    #: value. It is not: the marginal figure is the slope at the head bucket while
    #: the position is the integral of that slope from the floor upward, over a
    #: curve the model itself reports as kinked at every switching-fee boundary. On
    #: the 2026-09-02 capture the product gives 2.30 EUR against an actual
    #: 3.0942 EUR -- a third out -- so the shortcut is not an approximation of this
    #: field, it is a different number.
    #:
    #: Additive and optional. Absent on every document written before beta.39, and
    #: on any day whose first refresh landed before the day had a usable
    #: state-of-charge reading; the revaluation then publishes ``None`` with a
    #: reason, never zero. Keyed by the civil date the record already is, so the
    #: 92/96/100-interval machinery is untouched by it.
    open_value: dict[str, Any] | None = None
    #: The day's authoritative realised battery benefit, sealed once. beta.42.
    #:
    #: **Persisted because the evidence does not outlive the figure**, which is a
    #: narrower claim than the one this field was first written to make and is the
    #: one the arithmetic actually supports. The comparator is four measured cash
    #: legs -- import and export, actual and counterfactual -- so it reads no state
    #: of charge, no capacity and no efficiency, and a re-derivation of a day whose
    #: record and prices are both still on disk returns the same number. That is a
    #: property worth having and ``test_beta42_day_finalisation`` pins it rather
    #: than assuming it.
    #:
    #: What re-derivation cannot survive is the day record being **evicted** at 365
    #: days, and a lifetime total has to. So the value is computed while the
    #: evidence exists and folded forward when the evidence goes -- which also means
    #: eviction needs no prices and no ``await``, and can therefore happen where it
    #: actually happens, inside a synchronous callback.
    #:
    #: Write-once, on the same guard as :attr:`open_value`: the field's own absence.
    #: So a restart, a reload or an extra refresh cannot re-seal a day, and a lost
    #: write simply replays.
    #:
    #: ``bv`` records **which comparator produced the figure**, because beta.42 is
    #: itself the release that corrects the comparator. A day sealed under an older
    #: basis has to be identifiable rather than quietly summed alongside days sealed
    #: under the corrected one.
    #:
    #: Additive and optional. Absent on every document written before beta.42, and
    #: on any day that never became finalisable -- which is a defined state that
    #: withholds the lifetime total's completeness claim, never a zero.
    final_benefit: dict[str, Any] | None = None

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
            ("soc", None),
            ("pv", None),
            ("grid_import", None),
            ("grid_export", None),
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

    def total_load_at(self, index: int) -> float | None:
        """Return the whole household's energy for one interval, or ``None``.

        **The same reading as :meth:`baseline_at` without the flexible load taken
        out**, and it exists because the two are not interchangeable and beta.42
        found them being differenced against each other.

        The baseline is what the *forecast* wants: it excludes an electric vehicle
        so the optimiser never reserves battery energy to cover a charging session
        it may itself schedule. The realised counterfactual wants the opposite. It
        asks what the meter would have read with no battery, and it compares that
        against ``grid_import_at`` -- which is the whole house, vehicle included. Fed
        the baseline, ``max(0, baseline - pv) - import`` went to zero on every
        interval the vehicle drew, so realised avoidance silently read as no battery
        benefit at all, while those intervals still counted as priced.

        Validity is the baseline's rule and not a looser one. A missing measurement
        is ``None``; and an interval where a flexible load was expected but not
        recorded is ``None`` too, because the total is then unknown by exactly the
        amount nobody measured. Returning the bare measured figure there would be
        the same class of error in the other direction.
        """
        measured = self.measured[index]
        if measured is None:
            return None
        if self.ev_expected[index] and self.ev[index] is None:
            return None
        return max(measured, 0.0)

    def soc_at(self, index: int) -> float | None:
        """Return the recorded state of charge at the end of one interval."""
        if not 0 <= index < self.interval_count:
            return None
        return self.soc[index]

    @property
    def soc_sample_count(self) -> int:
        """Return how many intervals carry a state-of-charge sample."""
        return sum(1 for value in self.soc if value is not None)

    def grid_import_at(self, index: int) -> float | None:
        """Return the measured grid import of one interval, or ``None``."""
        if not 0 <= index < self.interval_count:
            return None
        return self.grid_import[index]

    def grid_export_at(self, index: int) -> float | None:
        """Return the measured grid export of one interval, or ``None``."""
        if not 0 <= index < self.interval_count:
            return None
        return self.grid_export[index]

    def pv_at(self, index: int) -> float | None:
        """Return the measured PV energy of one interval, or ``None``."""
        if not 0 <= index < self.interval_count:
            return None
        return self.pv[index]

    @property
    def pv_sample_count(self) -> int:
        """Return how many intervals carry a measured PV reading."""
        return sum(1 for value in self.pv if value is not None)

    @property
    def pv_total_kwh(self) -> float:
        """Return the day's measured PV energy across the intervals that have it.

        A partial total, and honestly so: it is the sum of what was observed, not
        an estimate of the day. ``pv_sample_count`` is what says how much of the
        day that covers.
        """
        return round(sum(value for value in self.pv if value is not None), 4)

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
        soc_percent: float | None = None,
        pv_kwh: float | None = None,
        grid_import_kwh: float | None = None,
        grid_export_kwh: float | None = None,
    ) -> bool:
        """Store one finalised interval by chronological index.

        Returns whether the interval was actually stored. An out-of-range index
        is dropped, and the caller must be told: under a single stable timezone
        the index is provably inside the day, so a rejection here means
        something upstream is already wrong -- a record whose stored length
        disagrees with the current zone, or a day boundary computed in a zone
        the record was not written in. Returning nothing let that land as a
        finalised-looking quarter that had in fact stored nothing at all.
        """
        if not 0 <= index < self.interval_count:
            return False
        if measured_kwh is not None:
            self.measured[index] = round(measured_kwh, _KWH_PRECISION)
        if ev_kwh is not None:
            self.ev[index] = round(ev_kwh, _KWH_PRECISION)
        self.ev_expected[index] = ev_expected
        if soc_percent is not None:
            self.soc[index] = round(soc_percent, _SOC_PRECISION)
        if pv_kwh is not None:
            self.pv[index] = round(pv_kwh, _KWH_PRECISION)
        if grid_import_kwh is not None:
            self.grid_import[index] = round(grid_import_kwh, _KWH_PRECISION)
        if grid_export_kwh is not None:
            self.grid_export[index] = round(grid_export_kwh, _KWH_PRECISION)
        return True

    def note_opening_valuation(
        self,
        *,
        valued_at: str,
        stored_energy_kwh: float,
        position_value_eur: float,
        floor_kwh: float,
        bucket_kwh: float,
    ) -> bool:
        """Record what this day opened holding, once. Returns whether it wrote.

        **Idempotent, and that is the whole of its correctness.** A Home Assistant
        reload, a coordinator restart or an extra refresh must not be able to
        re-open a civil day: the second write would move the reference the whole
        day's revaluation is measured from, and the figure would silently reset
        mid-day. The guard is the field's own absence, so it holds across a
        restart without anything else being remembered.
        """
        if self.open_value is not None:
            return False
        self.open_value = {
            "at": valued_at,
            "e": round(float(stored_energy_kwh), _KWH_PRECISION),
            "v": round(float(position_value_eur), 6),
            "f": round(float(floor_kwh), _KWH_PRECISION),
            "b": round(float(bucket_kwh), 6),
        }
        return True

    def note_final_benefit(
        self,
        *,
        finalized_at: str,
        benefit_eur: float,
        basis_version: str,
    ) -> bool:
        """Seal this day's realised battery benefit, once. Returns whether it wrote.

        **The idempotence is the correctness**, exactly as it is for
        :meth:`note_opening_valuation`, and for a sharper reason: this figure is
        summed into a lifetime total. A second write would not merely move a
        reference, it would double-count, and no later read could tell that it had.

        The caller decides *when* a day qualifies -- that judgement needs prices,
        an interval count and a rollover, none of which belong in a storage record.
        What belongs here is that the answer is written at most once.
        """
        if self.final_benefit is not None:
            return False
        self.final_benefit = {
            "at": finalized_at,
            "v": round(float(benefit_eur), 6),
            "bv": str(basis_version),
        }
        return True

    @property
    def benefit_eur_final(self) -> float | None:
        """Return the sealed benefit, or ``None`` while the day is not sealed."""
        if not self.final_benefit:
            return None
        value = self.final_benefit.get("v")
        return float(value) if isinstance(value, int | float) else None

    @property
    def benefit_finalized_at(self) -> str | None:
        """Return when the day was sealed, or ``None``."""
        if not self.final_benefit:
            return None
        value = self.final_benefit.get("at")
        return value if isinstance(value, str) else None

    @property
    def benefit_basis_version(self) -> str | None:
        """Return which comparator sealed the day, or ``None``."""
        if not self.final_benefit:
            return None
        value = self.final_benefit.get("bv")
        return value if isinstance(value, str) else None

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
        if any(value is not None for value in self.soc):
            # Omitted entirely on an installation that has never produced a
            # usable reading, exactly as the flexible-load arrays are, so the
            # document does not grow for a user this evidence cannot help.
            payload["s"] = self.soc
        if any(value is not None for value in self.pv):
            # Omitted entirely on an installation with no PV, or with no usable
            # reading yet, exactly as the flexible-load and state-of-charge
            # arrays are. The document does not grow for a user this evidence
            # cannot help.
            payload["p"] = self.pv
        if any(value is not None for value in self.grid_import):
            # Omitted entirely until a usable reading exists, exactly as the
            # three arrays above are. A document written before minor 2.4 has
            # neither key, and reads as no samples rather than as zeros.
            payload["gi"] = self.grid_import
        if any(value is not None for value in self.grid_export):
            payload["gx"] = self.grid_export
        if self.open_value is not None:
            # Omitted while absent, exactly as the five arrays above are, so a
            # document from a day that never had a usable opening reading is
            # byte-identical to a beta.38 one.
            payload["ov"] = dict(self.open_value)
        if self.final_benefit is not None:
            # Omitted while absent, exactly as ``ov`` is. A day that never became
            # finalisable writes the document a beta.41 release would have written.
            payload["bf"] = dict(self.final_benefit)
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
        # Absent on every document written before this field existed, and on any
        # installation with no usable reading. ``_numeric_list`` already refuses
        # a non-finite or non-numeric entry, so a damaged array degrades to
        # missing samples rather than to plausible-looking numbers.
        record.soc = _numeric_list(raw.get("s"), count)
        # Absent on every document written before beta.9, and on any installation
        # without PV. Read as missing samples rather than as zeros, which is the
        # difference between "the panels produced nothing" and "nobody looked".
        record.pv = _numeric_list(raw.get("p"), count)
        # Absent on every document written before beta.14. Missing samples, never
        # zeros: "the house exported nothing" and "nobody measured" are different
        # facts, and the second must not be able to look like the first in a
        # dataset a later phase will price.
        record.grid_import = _numeric_list(raw.get("gi"), count)
        record.grid_export = _numeric_list(raw.get("gx"), count)
        # Absent on every document written before beta.39. **Validated rather than
        # trusted**: it is read back as a divisor and a subtrahend inside a
        # published accounting identity, so a partial or non-numeric entry has to
        # degrade to "no opening valuation" -- which is a defined state with its
        # own published reason -- rather than to a plausible-looking zero.
        record.open_value = _opening_valuation(raw.get("ov"))
        # Absent on every document written before beta.42. **Validated rather than
        # trusted, for the same reason ``ov`` is and one more**: this value is
        # summed into a lifetime total, so a non-numeric entry that degraded to a
        # plausible zero would not merely be wrong, it would be invisible -- a day
        # that reads as sealed-at-nothing is indistinguishable from a day the
        # battery genuinely saved nothing. A malformed entry reads as *unsealed*,
        # which is a defined state that says so.
        record.final_benefit = _final_benefit(raw.get("bf"))
        flags_raw = raw.get("x")
        if isinstance(flags_raw, list):
            record.ev_expected = [bool(flag) for flag in flags_raw[:count]] + [
                False
            ] * max(0, count - len(flags_raw))
        else:
            record.ev_expected = [False] * count
        return record


def _opening_valuation(raw: Any) -> dict[str, Any] | None:
    """Return a usable opening valuation, or ``None``.

    All five numbers or none of them. A revaluation is a difference between two
    valuations of the same energy against the same lattice, so a record missing
    the energy it valued, or the floor and bucket it was measured against, cannot
    be compared with anything -- and half a valuation is not a smaller one.
    """
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    if not isinstance(at, str) or not at:
        return None
    values: dict[str, Any] = {"at": at}
    for key in ("e", "v", "f", "b"):
        number = raw.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return None
        number = float(number)
        if number != number or abs(number) == float("inf"):
            return None
        values[key] = number
    return values


def _final_benefit(raw: Any) -> dict[str, Any] | None:
    """Return a usable sealed benefit, or ``None``.

    All three fields or none. The euro figure alone would be summable but not
    auditable: without ``at`` nothing can say when the day stopped being
    re-derivable, and without ``bv`` a figure produced by the comparator beta.42
    replaced is indistinguishable from one produced by the corrected one. A
    lifetime total assembled from unattributable addends is not evidence.

    A benefit of exactly zero is legitimate and is kept -- a day the battery did
    not help is a real measurement. What is refused is a *malformed* entry, which
    reads as unsealed rather than as zero, because those two say opposite things.
    """
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    basis = raw.get("bv")
    if not isinstance(at, str) or not at:
        return None
    if not isinstance(basis, str) or not basis:
        return None
    value = raw.get("v")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value != value or abs(value) == float("inf"):
        return None
    return {"at": at, "v": value, "bv": basis}


def _numeric_list(raw: Any, count: int) -> list[float | None]:
    """Return ``count`` optional floats, dropping anything non-numeric.

    A corrupted entry becomes ``None`` -- missing data -- rather than zero.

    ``NaN`` and the infinities are dropped too. Nothing this integration writes
    can produce one, but a hand-edited or externally damaged document can, and
    Python's ``json`` accepts the literals. One would propagate straight through
    every mean, total and forecast into a sensor state, poisoning arithmetic that
    has no other way to notice: unlike a wrong number, ``NaN`` compares false
    against every threshold, so even the completeness guards would wave it past.
    """
    values: list[float | None] = [None] * count
    if not isinstance(raw, list):
        return values
    for index, value in enumerate(raw[:count]):
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise the backend with its migration flag cleared."""
        super().__init__(*args, **kwargs)
        #: Set when this store threw away a pre-v2 document, so diagnostics can
        #: say that history was discarded by a schema migration rather than
        #: leaving it indistinguishable from a fresh install. A user whose
        #: learning vanished on upgrade otherwise has only a log line, which has
        #: usually rotated away by the time they ask.
        #:
        #: Per instance rather than per class: two config entries load their own
        #: documents, and a shared flag would report one entry's migration
        #: against the other's history.
        self.discarded_legacy_document = False

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
            self.discarded_legacy_document = True
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

    def record_decision(self, entry: dict[str, Any]) -> None:
        """Append one Stage-A decision record, evicting the oldest.

        Append-only and bounded at :data:`MAX_DECISION_RECORDS_RETAINED`, which is
        two days of quarter-hour refreshes -- long enough to replay a weekend
        against a changed architecture, short enough that this never becomes a
        database. The caller decides what a record contains; this only bounds it.
        """
        self.decisions.append(entry)
        if len(self.decisions) > MAX_DECISION_RECORDS_RETAINED:
            del self.decisions[:-MAX_DECISION_RECORDS_RETAINED]

    def note_lifecycle_closed(self, instance_id: str) -> bool:
        """Latch one instance's public terminal. Returns whether it was new.

        Idempotent by membership, which is what makes a restart replay nothing: the
        recovery pass asks this before publishing, so an instance closed before the
        restart is closed once in total rather than once per boot.
        """
        if instance_id in self.closed_lifecycle:
            return False
        self.closed_lifecycle.append(instance_id)
        del self.closed_lifecycle[:-MAX_CAMPAIGN_LIFECYCLE_REMEMBERED]
        return True

    def lifecycle_closed(self, instance_id: str | None) -> bool:
        """Return whether this instance's public terminal was already published."""
        return instance_id is not None and instance_id in self.closed_lifecycle

    def seal_day(self, day: date, benefit_eur: float) -> bool:
        """Fold one finalised day into the lifetime total. Returns whether it moved.

        **Monotonic by refusal, not by convention.** A day at or before the cursor
        is already counted and is rejected, which is what makes a replay after a
        lost write harmless and what stops a backwards clock from double-counting.
        The cursor advances to the sealed day and never past it, so a gap left by a
        day that never qualified stays visible instead of being stepped over.
        """
        if self.sealed_through is not None and day <= self.sealed_through:
            return False
        self.sealed_benefit_eur = round(self.sealed_benefit_eur + float(benefit_eur), 6)
        self.sealed_day_count += 1
        self.sealed_through = day
        return True

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
        #: What Stage B needs to survive a restart, and nothing else.
        #:
        #: Two things, both of which are worthless if forgotten. The published
        #: revision of each execution target, because a revision that reset to one
        #: on every reboot would tell Stage B that every target it has been
        #: tracking for hours is brand new. And the causal record of a dispatch
        #: Alpha EMS armed, because without it an owned dispatch is
        #: indistinguishable from a stranger's after a restart -- and that is the
        #: one situation where the only safe action is to touch nothing.
        #:
        #: Deliberately not the plan, not the progress and not the economics. A
        #: restart should reconstruct those from evidence, not trust a snapshot.
        self.execution_revisions: dict[str, dict[str, Any]] = {}
        self.execution_record: dict[str, Any] | None = None
        #: One compact record per Stage-A refresh, oldest first, bounded.
        #:
        #: **Without this nothing this optimiser decides is auditable.** The
        #: attempt to reconstruct two real days of purchases from beta.30 evidence
        #: failed on three missing inputs: a twelve-entry quarter ring that had
        #: already dropped every night campaign, no historical price series in the
        #: payload, and -- decisively -- no record of the pack energy at the moment
        #: each decision was made, which is the initial condition of any replay.
        #:
        #: Scalars and fingerprints only, deliberately. The forecast and price
        #: evidence layers already retain their own series for a year, so storing
        #: them again per refresh would cost megabytes to duplicate what is already
        #: on disk. What could not be recovered from anywhere is here.
        self.decisions: list[dict[str, Any]] = []
        #: Set when the document could not be read at all. While true the store
        #: refuses to write, because an empty in-memory history must never be
        #: allowed to overwrite a file whose only problem may have been a
        #: momentary I/O error.
        self.corrupt = False
        #: The open campaign's public lifecycle: which transitions have been
        #: published for it, and the evidence needed to close it truthfully after a
        #: restart. beta.42.
        #:
        #: **Without this a ``created`` event can be emitted and never closed**, and
        #: that breaks the one guarantee the surface is built on. The open campaign's
        #: identity was in-memory only -- ``_campaign_id``,
        #: ``_campaign_instance_id`` and ``_campaign_started_at`` are plain
        #: attributes -- so a restart mid-campaign minted a fresh instance with a new
        #: ``opened_at`` and the pre-restart one never received ``removed``. One
        #: physical objective appeared twice in the log under two ids.
        #:
        #: **The evidence, not merely the four booleans**, and that distinction is
        #: the difference between reporting a restart and papering over it. A
        #: campaign whose terminal verdict was already computed before the restart --
        #: objective measurable, target frozen, reason recorded -- has a *truthful*
        #: result, and it is preserved. Without the evidence that case would be
        #: indistinguishable from one interrupted mid-flight, and the log would
        #: report a successful campaign as failed.
        self.campaign_lifecycle: dict[str, Any] | None = None
        #: The one future campaign currently announced to the trading log. beta.45.
        #:
        #: **Persisted for the same reason the lifecycle mark is: so a restart does
        #: not re-announce a campaign the log already shows.** A single dict rather
        #: than a list -- one announcement is live at a time, exactly as one campaign
        #: is, and the overlap test decides which publications continue it.
        #:
        #: It carries no realised figure and no instance id, because an announcement
        #: has neither. Nothing in the accounting reads it.
        self.campaign_announcement: dict[str, Any] | None = None
        #: Instances whose public terminal has already been published. beta.42.
        #:
        #: **A telemetry latch, and deliberately not one of the two that exist.**
        #: ``_final_campaigns`` means a campaign may never run again, and
        #: ``_closed_instances`` is session-local. A never-started campaign must
        #: receive its ``removed`` event *and* stay free to be attempted properly
        #: later, so filing its terminal through either of those would block a
        #: legitimate later attempt. This one answers exactly one question -- has
        #: this instance's terminal already been published? -- and a later attempt
        #: opens a different instance, so it can never be the answer for that one.
        self.closed_lifecycle: list[str] = []
        #: The realised battery benefit of every finalised day the store no longer
        #: retains, and the date through which that total is complete. beta.42.
        #:
        #: **A running total plus a cursor, rather than a flag on each record**, and
        #: the cursor is what makes it survive the three ways a per-record flag
        #: fails. It replays idempotently after a lost write, because re-sealing a
        #: day already behind the cursor is a no-op. It still covers a day that
        #: ``DayRecord.from_dict`` rejected outright -- which ``prune`` never sees,
        #: because the record was never built. And it survives a change to
        #: ``MAX_HISTORY_DAYS``, where a flag living on the deleted record could not.
        #:
        #: The earlier design sealed a day as it was evicted. That could not work:
        #: the same operation deletes the record the guard lives on, so the guard
        #: could only ever be read ``False``, and a day 365 days old cannot be priced
        #: at the moment it is dropped -- ``prune`` is a synchronous callback and the
        #: prices live behind an awaitable partition load.
        self.sealed_benefit_eur: float = 0.0
        #: The last civil day folded into :attr:`sealed_benefit_eur`. Monotonic on
        #: write, enforced rather than assumed: a clock that steps backwards, or a
        #: ``prune`` cutoff that trails wall-clock after an outage, must not be able
        #: to rewind the cursor and re-add days already counted.
        self.sealed_through: date | None = None
        #: How many days :attr:`sealed_benefit_eur` is the sum of.
        #:
        #: **A total without its count is not a mean.** Once a day is evicted the
        #: record it was derived from is gone, so "how many days does this figure
        #: cover?" is unanswerable from the retained days alone -- and an average
        #: computed over only what is still on disk would climb every time a day
        #: aged out, on a figure whose whole purpose is to stay put.
        self.sealed_day_count: int = 0
        #: Set when a pre-v2 document was discarded by the migration guard.
        self.reset_by_migration = False

    async def async_load(self, fallback_tz_key: str) -> None:
        """Load history from disk.

        Corrupt or unreadable storage degrades to an empty history rather than
        failing setup: losing learned days is recoverable, refusing to start is
        much less pleasant for the user.
        """
        self._store.discarded_legacy_document = False
        try:
            raw = await self._store.async_load()
        except Exception:
            _LOGGER.warning(
                "Learning history could not be read. Learning continues from an "
                "empty history for this session, but nothing will be written to "
                "disk until the problem is resolved and Home Assistant is "
                "restarted: the existing document is left untouched in case it "
                "is still intact and the read failure was transient"
            )
            self.corrupt = True
            return
        self.reset_by_migration = self._store.discarded_legacy_document

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

        # Absent on every document written before beta.19, and absence means
        # nothing was running -- never an assertion that something was.
        execution = raw.get("execution")
        if isinstance(execution, dict):
            revisions = execution.get("revisions")
            if isinstance(revisions, dict):
                for plan_id, value in revisions.items():
                    if isinstance(plan_id, str) and isinstance(value, dict):
                        self.execution_revisions[plan_id] = dict(value)
            record = execution.get("record")
            self.execution_record = dict(record) if isinstance(record, dict) else None
            # Absent on every document written before beta.42, and absence means no
            # campaign was open -- never an assertion that one was. A malformed entry
            # reads the same way: the recovery then finds nothing to close, which
            # loses one log line, where trusting it could publish a terminal for a
            # campaign that never existed.
            lifecycle = execution.get("lifecycle")
            if isinstance(lifecycle, dict) and isinstance(
                lifecycle.get("instance_id"), str
            ):
                self.campaign_lifecycle = dict(lifecycle)
            # Absent on every document written before beta.45, and absence means
            # nothing was announced. A malformed entry reads the same way: the log
            # loses one plan line rather than announcing a campaign twice.
            announcement = execution.get("announcement")
            if isinstance(announcement, dict) and isinstance(
                announcement.get("purpose"), str
            ):
                self.campaign_announcement = dict(announcement)
            closed = execution.get("closed_lifecycle")
            if isinstance(closed, list):
                self.closed_lifecycle = [
                    value for value in closed if isinstance(value, str)
                ][-MAX_CAMPAIGN_LIFECYCLE_REMEMBERED:]

        # Absent on every document written before beta.42, and absence means no day
        # has been sealed -- never that the lifetime benefit was measured at zero.
        # Read defensively and **together**: a cursor without its total, or a total
        # without its cursor, would let the next seal add to a figure whose coverage
        # nothing can state. Half a lifetime total is not a smaller one.
        sealed = raw.get("sealed")
        if isinstance(sealed, dict):
            through = sealed.get("through")
            amount = sealed.get("eur")
            if (
                isinstance(through, str)
                and not isinstance(amount, bool)
                and isinstance(amount, int | float)
                and float(amount) == float(amount)
                and abs(float(amount)) != float("inf")
            ):
                try:
                    self.sealed_through = date.fromisoformat(through)
                except (TypeError, ValueError):
                    self.sealed_through = None
                else:
                    self.sealed_benefit_eur = float(amount)
                    count = sealed.get("days")
                    # Absent on a document written between the cursor landing and
                    # the count joining it. Read as zero, which makes the published
                    # average conservative rather than inflated: it is divided by
                    # too few days, never by too many.
                    self.sealed_day_count = (
                        int(count)
                        if isinstance(count, int) and not isinstance(count, bool)
                        else 0
                    )

        # Additive since beta.31, so a document written by any earlier release
        # simply has no decisions and starts collecting them. Read defensively:
        # a malformed list costs the replay history and must not cost the load.
        decisions = raw.get("decisions")
        if isinstance(decisions, list):
            self.decisions = [
                dict(entry) for entry in decisions if isinstance(entry, dict)
            ][-MAX_DECISION_RECORDS_RETAINED:]

    def to_dict(self) -> dict[str, Any]:
        """Return the full serialisable document."""
        payload: dict[str, Any] = {
            "days": {
                day.isoformat(): record.to_dict()
                for day, record in sorted(self.days.items())
            },
            "balance": self.balance.to_dict(),
            "last_finalized": self.last_finalized,
        }
        execution: dict[str, Any] = {}
        if self.execution_revisions:
            execution["revisions"] = self.execution_revisions
        if self.execution_record is not None:
            execution["record"] = self.execution_record
        if self.campaign_lifecycle is not None:
            execution["lifecycle"] = dict(self.campaign_lifecycle)
        if self.campaign_announcement is not None:
            execution["announcement"] = dict(self.campaign_announcement)
        if self.closed_lifecycle:
            execution["closed_lifecycle"] = self.closed_lifecycle[
                -MAX_CAMPAIGN_LIFECYCLE_REMEMBERED:
            ]
        if self.decisions:
            # Omitted while empty, exactly as ``execution`` is: an installation
            # that has never planned anything writes the same document it always
            # did, and ``test_the_stored_document_has_the_documented_schema`` keeps
            # that promise honest.
            payload["decisions"] = self.decisions[-MAX_DECISION_RECORDS_RETAINED:]
        if execution:
            # Omitted entirely while there is nothing to remember, so a document
            # from an installation that has never armed anything is byte-identical
            # to a beta.18 one.
            payload["execution"] = execution
        if self.sealed_through is not None:
            # Omitted until the first day is sealed, exactly as ``execution`` is.
            payload["sealed"] = {
                "through": self.sealed_through.isoformat(),
                "eur": self.sealed_benefit_eur,
                "days": self.sealed_day_count,
            }
        return payload

    def schedule_save(self) -> None:
        """Queue a debounced write.

        Quarters finalise every fifteen minutes, so batching writes behind a
        short delay keeps disk churn negligible without risking real data loss.
        """
        if self.corrupt:
            return
        self._store.async_delay_save(self.to_dict, STORE_SAVE_DELAY)

    async def async_save_now(self) -> None:
        """Write immediately. Used on unload and on Home Assistant stop.

        Refused after a failed load. ``async_load`` degrades an unreadable
        document to an empty history so setup can continue, which is the right
        call for availability -- but that empty history was then written back
        over the file on the very next unload or shutdown, turning one transient
        read error into permanent loss of a year of learning. The document is
        left exactly as it was found instead.
        """
        if self.corrupt:
            return
        await self._store.async_save(self.to_dict())

    async def async_remove(self) -> None:
        """Delete this entry's document from disk.

        Used when the config entry is removed. The in-memory state is cleared too
        so a stray later save cannot resurrect the file.
        """
        self.days.clear()
        self.balance = BalanceStats()
        self.last_finalized = None
        # The lifetime total goes with the rest of it. Leaving the cursor behind
        # would let a stray save write a total whose days no longer exist -- and a
        # re-added entry would then start counting from a figure it never earned.
        self.sealed_benefit_eur = 0.0
        self.sealed_day_count = 0
        self.sealed_through = None
        self.campaign_lifecycle = None
        self.campaign_announcement = None
        self.closed_lifecycle.clear()
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
            # Pruned *before* the new day joins the history, not after. Inserting
            # first put the new day inside the set ``prune`` clamps against, so
            # ``reference > newest`` could never be true and the clock-excursion
            # guard below was inert on the only path that reaches it: a single
            # future-dated record deleted every retained day, and the debounced
            # save then wrote the empty document to disk within the minute.
            self.prune(reference=day)
            self.days[day] = record
        return record

    def prune(self, reference: date) -> int:
        """Drop days older than the retention window. Returns the count removed.

        ``reference`` is clamped to at most one day past the newest day already
        stored. Time advances a day at a time, so a reference immediately after
        the known history is ordinary progression, while one further ahead is
        either a gap in which nothing was learned or a clock that is simply
        wrong -- and neither is a reason to discard more history.

        That distinction matters because the failure is silent and total. A Pi
        without a real-time clock hands Home Assistant a date years ahead until
        NTP corrects it; the first quarter to close in that window creates a
        future-dated record, and an unclamped reference would prune the entire
        retention window against it. Backwards jumps need no guard: an older
        reference only ever prunes less.

        A genuine multi-day gap still prunes correctly. The clamp advances with
        the newest stored day, so once real days start being recorded again the
        window trims relative to actual recorded time rather than to whatever
        the clock momentarily claimed.
        """
        if self.days:
            horizon = max(self.days) + timedelta(days=1)
            if reference > horizon:
                reference = horizon
        cutoff = reference - timedelta(days=MAX_HISTORY_DAYS - 1)
        stale = sorted(day for day in self.days if day < cutoff)
        for day in stale:
            # **Folded before it is deleted, in date order.** The value was computed
            # and persisted when the day became finalisable, so nothing here needs
            # prices, a partition load or an ``await`` -- which is what makes this
            # possible inside a synchronous callback at all. The earlier design tried
            # to *price* a day at eviction and could not: this method never sees the
            # forecast history, and a 365-day-old day has no live forecast to read.
            #
            # A day that was never sealed contributes nothing and the cursor does not
            # advance past it, so an unfinalisable day leaves a visible gap in the
            # lifetime coverage rather than being silently skipped over. Both this
            # and the deletion ride the same document write, so they land together
            # or not at all -- and a replay is harmless because ``seal_day`` refuses
            # a day at or before the cursor.
            record = self.days[day]
            benefit = record.benefit_eur_final
            if benefit is not None:
                self.seal_day(day, benefit)
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
