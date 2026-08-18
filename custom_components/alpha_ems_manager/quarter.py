"""Time-weighted accumulation of house-load power into quarter-hour buckets.

A single instantaneous reading taken at ``xx:15`` says nothing useful about the
energy consumed during the preceding fifteen minutes, so this module integrates
the power signal over time instead.

Integration is *left-handed*: the most recent reading is held constant until the
next one arrives. That is the correct interpretation of a change-driven sensor,
which only publishes when its value actually moves.

Three properties are deliberate and load-bearing:

* Energy is only ever accrued across a gap no longer than
  :data:`~.const.MAX_SAMPLE_GAP_SECONDS`. A longer silence is recorded as
  missing coverage, so a dead source cannot invent consumption.
* Coverage is measured against the full quarter, not against the observed
  portion of it. A quarter that began before integration started can therefore
  never reach the acceptance threshold.
* All arithmetic happens in absolute UTC; local wall-clock time is used only to
  *label* a finished bucket with its date and 0..95 slot index. Adding fifteen
  minutes to a local datetime is wall-clock arithmetic in Python and silently
  does the wrong thing across a DST transition, so it is never done here. A
  fall-back day simply produces the same slot index twice, and a spring-forward
  day skips four indices.

The module deliberately imports nothing from Home Assistant so the measurement
rules can be tested directly against synthetic timelines.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

from .const import (
    EV_NEGATIVE_NOISE_FLOOR_W,
    MAX_CATCHUP_SECONDS,
    MAX_PLAUSIBLE_EV_W,
    MAX_PLAUSIBLE_LOAD_W,
    MAX_SAMPLE_GAP_SECONDS,
    MIN_QUARTER_COVERAGE,
    QUARTER_MINUTES,
    QUARTER_SECONDS,
)

#: Small negative readings are sensor noise around zero and are clamped up.
#: Anything more negative than this is implausible for a house-load sensor.
_NEGATIVE_NOISE_FLOOR_W = -100.0

_QUARTER = timedelta(minutes=QUARTER_MINUTES)


def floor_to_quarter_utc(moment: datetime, tz: tzinfo) -> datetime:
    """Return the UTC instant at which ``moment``'s local quarter began."""
    local = moment.astimezone(tz)
    floored = local.replace(
        minute=(local.minute // QUARTER_MINUTES) * QUARTER_MINUTES,
        second=0,
        microsecond=0,
    )
    return floored.astimezone(UTC)


def slot_index_of(local_moment: datetime) -> int:
    """Return the 0..95 wall-clock slot index of a local datetime."""
    return local_moment.hour * 4 + local_moment.minute // QUARTER_MINUTES


def sanitize_load_w(value_w: float | None) -> float | None:
    """Return a plausible non-negative load, or ``None`` when unusable."""
    if value_w is None:
        return None
    if value_w < _NEGATIVE_NOISE_FLOOR_W or value_w > MAX_PLAUSIBLE_LOAD_W:
        return None
    return max(0.0, value_w)


def sanitize_ev_w(value_w: float | None) -> float | None:
    """Return a plausible EV charging power, or ``None`` when unusable.

    EV charging is consumption, so the canonical value is never negative. Only
    a narrow noise band around zero is clamped up; anything more negative is an
    *invalid sample*, not zero charging.

    The distinction matters because the value is subtracted from measured load.
    Reading a negative as zero would silently leave a charging session inside
    the baseline, which is precisely the contamination this exists to prevent --
    and reading it as a negative subtraction would inflate the baseline instead.
    Refusing the sample invalidates that interval's baseline and says so.
    """
    if value_w is None:
        return None
    if value_w > MAX_PLAUSIBLE_EV_W:
        return None
    if value_w < EV_NEGATIVE_NOISE_FLOOR_W:
        return None
    return max(0.0, value_w)


@dataclass(frozen=True, slots=True)
class QuarterResult:
    """A finalised quarter-hour bucket."""

    #: Absolute instant at which the quarter began.
    start_utc: datetime
    #: Local civil date the quarter belongs to.
    day: date
    #: Wall-clock slot index, 0..95.
    slot: int
    #: Energy integrated during the quarter.
    energy_kwh: float
    #: Fraction of the quarter covered by valid samples, 0.0..1.0.
    coverage: float

    @property
    def accepted(self) -> bool:
        """Return whether this quarter carries enough coverage to be learned."""
        return self.coverage >= MIN_QUARTER_COVERAGE


class QuarterAccumulator:
    """Integrates a power signal and emits :class:`QuarterResult` buckets."""

    def __init__(
        self,
        tz: tzinfo,
        sanitizer: Callable[[float | None], float | None] = sanitize_load_w,
    ) -> None:
        """Initialise an accumulator labelling buckets in timezone ``tz``.

        ``sanitizer`` decides which raw readings are usable. House load and EV
        charging share the integration machinery but differ in what counts as a
        plausible value, so the rule is injected rather than hard-coded.
        """
        self._tz = tz
        self._sanitize = sanitizer
        self._slot_start: datetime | None = None
        self._cursor: datetime | None = None
        self._held_value_w: float | None = None
        self._energy_wh: float = 0.0
        self._valid_seconds: float = 0.0

    # -- introspection ---------------------------------------------------

    @property
    def started(self) -> bool:
        """Return whether any sample has been observed yet."""
        return self._cursor is not None

    @property
    def open_coverage(self) -> float:
        """Return the coverage accrued so far in the open quarter."""
        return min(1.0, self._valid_seconds / QUARTER_SECONDS)

    @property
    def open_energy_kwh(self) -> float:
        """Return the energy accrued so far in the open quarter."""
        return self._energy_wh / 1000.0

    # -- input -----------------------------------------------------------

    def add_sample(
        self, moment: datetime, value_w: float | None
    ) -> list[QuarterResult]:
        """Feed one reading and return any quarters this closed.

        ``value_w`` of ``None`` marks the source as unusable from ``moment``
        onward; the preceding interval is still integrated normally.
        """
        moment_utc = moment.astimezone(UTC)
        results = self._advance_to(moment_utc)
        self._held_value_w = self._sanitize(value_w)
        return results

    # -- internals -------------------------------------------------------

    def _advance_to(self, moment: datetime) -> list[QuarterResult]:
        """Integrate from the cursor up to ``moment``, closing full quarters."""
        if self._cursor is None:
            self._begin(moment)
            return []

        if moment <= self._cursor:
            # Out-of-order or duplicate timestamp: nothing to integrate. The
            # caller still refreshes the held value, which is the useful part
            # of a late update.
            return []

        # Validity is decided once for the whole interval. A silence longer than
        # the tolerated gap contributes neither energy nor coverage, no matter
        # how many quarter boundaries it spans.
        gap_seconds = (moment - self._cursor).total_seconds()

        if gap_seconds > MAX_CATCHUP_SECONDS:
            # Nothing in a gap this long is recoverable: every quarter it spans
            # already fails the tolerated-gap test above, so walking it one
            # bucket at a time can only manufacture rejections. A clock stepped
            # from 1970 to the present would produce two million of them in a
            # single synchronous loop. Restart accumulation at the new instant
            # instead; the partially observed quarter it lands in cannot reach
            # the coverage threshold, which is the correct outcome anyway.
            self._begin(moment)
            return []
        contributes = (
            self._held_value_w is not None and gap_seconds <= MAX_SAMPLE_GAP_SECONDS
        )

        results: list[QuarterResult] = []
        while self._cursor < moment:
            assert self._slot_start is not None
            boundary = self._slot_start + _QUARTER
            step_end = min(moment, boundary)
            seconds = (step_end - self._cursor).total_seconds()

            if contributes and seconds > 0:
                assert self._held_value_w is not None
                self._energy_wh += self._held_value_w * seconds / 3600.0
                self._valid_seconds += seconds

            self._cursor = step_end
            if self._cursor >= boundary:
                results.append(self._close_slot(boundary))

        return results

    def _begin(self, moment: datetime) -> None:
        """Start accumulating at ``moment`` inside its containing quarter."""
        self._slot_start = floor_to_quarter_utc(moment, self._tz)
        self._cursor = moment
        self._energy_wh = 0.0
        self._valid_seconds = 0.0

    def _close_slot(self, next_start: datetime) -> QuarterResult:
        """Finalise the open quarter and open the one beginning at ``next_start``."""
        assert self._slot_start is not None
        local_start = self._slot_start.astimezone(self._tz)
        result = QuarterResult(
            start_utc=self._slot_start,
            day=local_start.date(),
            slot=slot_index_of(local_start),
            energy_kwh=self._energy_wh / 1000.0,
            coverage=min(1.0, self._valid_seconds / QUARTER_SECONDS),
        )
        self._slot_start = next_start
        self._energy_wh = 0.0
        self._valid_seconds = 0.0
        return result

    def reset(self) -> None:
        """Discard all accumulation state.

        A reload does not call this -- it builds a fresh accumulator instead --
        so this exists for a caller that wants to reuse an instance. Either way
        the in-flight quarter is deliberately dropped rather than restored: a
        partially observed quarter cannot reach the coverage threshold anyway,
        and guessing at the unobserved remainder would fabricate load across
        downtime.
        """
        self._slot_start = None
        self._cursor = None
        self._held_value_w = None
        self._energy_wh = 0.0
        self._valid_seconds = 0.0
