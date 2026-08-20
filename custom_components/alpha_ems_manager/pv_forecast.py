"""Phase 5: the PV forecast model, its mapping, and its provenance.

Pure. Nothing here imports Home Assistant, opens a socket or names a hostname, so
the whole mapping -- the part most likely to be quietly wrong -- is exercisable
against hand-written rows with no instance in sight. The impure half lives in
``solcast_source`` and does nothing but fetch values.

Energy is the primitive
-----------------------

The source publishes **average power in kilowatts** per period. This module
converts once, at the boundary, and everything downstream is kilowatt-hours per
chronological quarter-hour -- the same unit and the same index as
``LoadForecast.intervals``. Any consumer that already handles one handles the
other, with no alignment code, which is the entire compatibility contract Phases
6 to 10 depend on.

Reading that figure as though it were interval energy is the single most
plausible mistake available here, and it doubles every number on a thirty-minute
source. It has its own mutation test.

Piecewise-constant, never interpolated
--------------------------------------

A source period covering two quarters gives each quarter the *same average
power*, so the two quarters sum to exactly the period's energy. Drawing a smooth
curve between periods would invent intra-period shape that the source never
published -- the fabrication this project refuses everywhere else -- and it would
not conserve energy either.

The period length is **measured** from consecutive timestamps rather than
assumed. Every row this project has ever seen from the live source was thirty
minutes, which is precisely why assuming it would be untestable: a resolution
change would silently halve or double every stored series.

Missing is missing
------------------

``None`` means no forecast for that interval. It is never zero. Zero is a
forecast of no generation, which after dark is true and at noon is a fault, and
collapsing the two would make the whole evidence layer meaningless.

Multiple sites
--------------

The user declares which rooftop sites belong to this installation. When that is
all of them, the source's own aggregate series is used. When it is a subset, each
site is mapped separately and the arrays are summed per interval -- P10, P50 and
P90 each on their own, never derived from one another.

A selected site missing an interval never becomes zero for that interval: the sum
of the sites that did report is kept, tagged as partial, with the contributing
count recorded, and excluded from accuracy scoring. It is a known
*under*-estimate, which is the benign direction -- understated PV raises net
demand, and export protection comes from the meter rather than from PV.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

from .const import (
    PV_ACTUAL_BOUNDARY_MIXED,
    PV_AGGREGATE_SITE,
    PV_ELECTRICAL_CORRESPONDENCE_UNKNOWN,
    PV_FORECAST_BOUNDARY_UNSPECIFIED,
    PV_MAPPING_VERSION,
    PV_PERCENTILE_COMONOTONIC_SUM,
    PV_PERCENTILE_SOURCE_AGGREGATE,
    PV_QUERY_MODE_AGGREGATE,
    PV_QUERY_MODE_PER_SITE,
    PV_SELECTION_ORIGIN_AUTO,
    PV_SOURCE_PERIOD_STEP_MINUTES,
    PV_UNAVAILABLE_NO_ROWS,
    PV_UNAVAILABLE_PERIOD_REFUSED,
    PV_UNAVAILABLE_UNUSABLE_ROWS,
    QUARTER_MINUTES,
)

#: Hours in one planning interval. Defined in ``battery`` for the battery model
#: and deliberately not re-exported from here under the same name: a structural
#: test asserts exactly one module declares ``INTERVAL_HOURS``.
_QUARTER_HOURS = QUARTER_MINUTES / 60.0

#: Length of a fingerprint, in hex characters. Long enough that a collision is
#: not a practical concern and short enough to read in a diagnostics download.
_FINGERPRINT_CHARS = 16


def _fingerprint(parts: Iterable[Any]) -> str:
    """Return a short stable digest of an ordered sequence of facts.

    Sorted by the caller, not here, so the caller stays responsible for deciding
    what counts as the same set.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:_FINGERPRINT_CHARS]


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not one.

    Strings are refused rather than coerced. A source that started publishing
    ``"2.27"`` instead of ``2.27`` has changed its contract, and silently parsing
    it would hide that -- while a source publishing ``"unavailable"`` would parse
    to nothing at all.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


# --- site facts ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PvSite:
    """One rooftop site as the source describes it.

    ``resource_id`` is identity. ``name`` is decoration: it appears in the
    options form and in diagnostics, and it is deliberately absent from both
    fingerprints, because renaming a roof does not make it a different roof.
    """

    resource_id: str
    name: str = ""
    capacity_kw: float | None = None
    capacity_dc_kw: float | None = None
    azimuth: float | None = None
    tilt: float | None = None
    loss_factor: float | None = None

    @property
    def model_key(self) -> tuple[Any, ...]:
        """Return the facts that change what this site is forecast to produce.

        ``loss_factor`` is in here, and was missing from an earlier draft. It
        scales every figure the source returns, so a site whose loss factor moved
        from 0.9 to 0.85 would have produced a different series while looking
        like the same site.
        """
        return (
            self.resource_id,
            self.capacity_kw,
            self.capacity_dc_kw,
            self.azimuth,
            self.tilt,
            self.loss_factor,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics form."""
        return {
            "resource_id": self.resource_id,
            "name": self.name,
            "capacity_kw": self.capacity_kw,
            "capacity_dc_kw": self.capacity_dc_kw,
            "azimuth": self.azimuth,
            "tilt": self.tilt,
            "loss_factor": self.loss_factor,
        }


def sites_identity(site_ids: Iterable[str]) -> str:
    """Return the membership fingerprint of a set of site identifiers.

    Membership only. Changing it means the question "which roofs is this a
    forecast of" has a different answer, and evidence either side of that is not
    poolable.
    """
    return _fingerprint(sorted(set(site_ids)))


def sites_model(sites: Iterable[PvSite], excluded: Iterable[str] = ()) -> str:
    """Return the physical-model fingerprint of a set of sites.

    Excluded sites are folded in because excluding one changes what the source's
    aggregate contains without changing any individual site.
    """
    return _fingerprint(
        [site.model_key for site in sorted(sites, key=lambda s: s.resource_id)]
        + [("excluded", tuple(sorted(set(excluded))))]
    )


# --- provenance ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PvProvenance:
    """Every source fact needed to decide whether two series are comparable.

    Read from the source, never inferred. The fields that say "unknown" say so
    because they are unknown, and that is the point of them: a guessed value here
    would be indistinguishable from a measured one to the phase that finally reads
    it.
    """

    integration_version: str | None = None

    # -- membership, declared by the user -------------------------------------
    selected_site_ids: tuple[str, ...] = ()
    selected_site_display_names: tuple[str, ...] = ()
    selected_sites_identity: str = ""
    selected_sites_model: str = ""
    selected_site_count: int = 0
    available_site_count: int = 0
    available_sites_identity: str = ""
    selection_complete: bool = False
    selection_origin: str = PV_SELECTION_ORIGIN_AUTO
    membership_declared: bool = False
    selected_capacity_ac_total_kw: float | None = None
    selected_capacity_dc_total_kw: float | None = None
    excluded_sites: tuple[str, ...] = ()

    # -- how the series was produced -----------------------------------------
    query_mode: str = PV_QUERY_MODE_AGGREGATE
    percentile_aggregation: str = PV_PERCENTILE_SOURCE_AGGREGATE
    period_minutes: int | None = None
    mapping_version: int = PV_MAPPING_VERSION

    # -- what the source was doing to its own numbers ------------------------
    estimate_key: str | None = None
    dampened: bool | None = None
    auto_dampening_active: bool | None = None
    #: Actuals blending: a *second* correction channel, independent of dampening.
    #: An earlier draft of this design recorded only dampening and would have let
    #: Phase 9 learn on top of a correction it could not see.
    get_actuals: bool | None = None
    use_actuals: float | None = None
    #: Raw value and a separate judgement, rather than one boolean. The live
    #: install reports a configured hard limit of 100.0, which cannot bind a
    #: six-kilowatt array under any reading the data does not already disprove --
    #: so a bare "configured: true" would have implied the source modelled
    #: clipping when it demonstrably does not.
    hard_limit_raw: float | None = None
    hard_limit_binding: bool | None = None
    api_limit: int | None = None
    api_used: int | None = None
    forecast_health: str | None = None
    source_updated_at: datetime | None = None

    # -- boundaries, declared rather than solved ------------------------------
    actual_pv_entity: str | None = None
    actual_pv_boundary: str = PV_ACTUAL_BOUNDARY_MIXED
    forecast_boundary: str = PV_FORECAST_BOUNDARY_UNSPECIFIED
    electrical_correspondence: str = PV_ELECTRICAL_CORRESPONDENCE_UNKNOWN

    @property
    def correction_key(self) -> tuple[Any, ...]:
        """Return the facts that mean the source was correcting its own output.

        One key across all three channels, because the consequence of any of them
        moving is identical: evidence either side is not poolable.
        """
        return (
            self.dampened,
            self.auto_dampening_active,
            self.get_actuals,
            self.use_actuals,
            self.estimate_key,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics and storage form.

        No API key material can appear here: nothing in this dataclass ever holds
        one, and the reader that builds it drops any key-like field at the
        boundary. A test asserts both halves against a response containing one.
        """
        return {
            "integration_version": self.integration_version,
            "selected_site_ids": list(self.selected_site_ids),
            "selected_site_display_names": list(self.selected_site_display_names),
            "selected_sites_identity": self.selected_sites_identity,
            "selected_sites_model": self.selected_sites_model,
            "selected_site_count": self.selected_site_count,
            "available_site_count": self.available_site_count,
            "available_sites_identity": self.available_sites_identity,
            "selection_complete": self.selection_complete,
            "selection_origin": self.selection_origin,
            "membership_declared": self.membership_declared,
            "selected_capacity_ac_total_kw": self.selected_capacity_ac_total_kw,
            "selected_capacity_dc_total_kw": self.selected_capacity_dc_total_kw,
            "excluded_sites": list(self.excluded_sites),
            "query_mode": self.query_mode,
            "percentile_aggregation": self.percentile_aggregation,
            "period_minutes": self.period_minutes,
            "mapping_version": self.mapping_version,
            "estimate_key": self.estimate_key,
            "dampened": self.dampened,
            "auto_dampening_active": self.auto_dampening_active,
            "get_actuals": self.get_actuals,
            "use_actuals": self.use_actuals,
            "hard_limit_raw": self.hard_limit_raw,
            "hard_limit_binding": self.hard_limit_binding,
            "api_limit": self.api_limit,
            "api_used": self.api_used,
            "forecast_health": self.forecast_health,
            "source_updated_at": (
                None
                if self.source_updated_at is None
                else self.source_updated_at.isoformat()
            ),
            "actual_pv_entity": self.actual_pv_entity,
            "actual_pv_boundary": self.actual_pv_boundary,
            "forecast_boundary": self.forecast_boundary,
            "electrical_correspondence": self.electrical_correspondence,
            "boundaries_note": (
                "the measured figure sums DC string power and an AC meter, while "
                "the source does not state its own boundary; the difference is a "
                "conversion property of the installation and is recorded rather "
                "than corrected. which selected site corresponds to which "
                "AlphaESS subsystem is unknown and is never guessed"
            ),
            "percentile_note": (
                "per-site percentile sums assume every site has its bad day at "
                "once, so they bound the aggregate more conservatively than a "
                "true joint percentile and are not a calibrated band"
            ),
        }


# --- the mapping report -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PvMappingReport:
    """What the mapping did, so it can be audited from a diagnostics download."""

    rows_received: int = 0
    rows_mapped: int = 0
    rows_malformed: int = 0
    rows_duplicated: int = 0
    rows_out_of_range: int = 0
    rows_non_monotonic: int = 0
    periods_refused: int = 0
    period_minutes: int | None = None
    period_minutes_observed: tuple[int, ...] = ()

    def merged_with(self, other: PvMappingReport) -> PvMappingReport:
        """Return the sum of two reports, for the per-site path."""
        return PvMappingReport(
            rows_received=self.rows_received + other.rows_received,
            rows_mapped=self.rows_mapped + other.rows_mapped,
            rows_malformed=self.rows_malformed + other.rows_malformed,
            rows_duplicated=self.rows_duplicated + other.rows_duplicated,
            rows_out_of_range=self.rows_out_of_range + other.rows_out_of_range,
            rows_non_monotonic=self.rows_non_monotonic + other.rows_non_monotonic,
            periods_refused=self.periods_refused + other.periods_refused,
            period_minutes=other.period_minutes or self.period_minutes,
            period_minutes_observed=tuple(
                sorted(
                    set(self.period_minutes_observed)
                    | set(other.period_minutes_observed)
                )
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics form."""
        return {
            "rows_received": self.rows_received,
            "rows_mapped": self.rows_mapped,
            "rows_malformed": self.rows_malformed,
            "rows_duplicated": self.rows_duplicated,
            "rows_out_of_range": self.rows_out_of_range,
            "rows_non_monotonic": self.rows_non_monotonic,
            "periods_refused": self.periods_refused,
            "period_minutes": self.period_minutes,
            "period_minutes_observed": list(self.period_minutes_observed),
            "rule": (
                "source rows carry average power in kW over a period whose length "
                "is measured from consecutive timestamps; each period is split "
                "piecewise-constant across the quarter-hours it spans, so the "
                "quarters sum to the period energy exactly and no intra-period "
                "shape is invented"
            ),
        }


# --- the forecast ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PvForecast:
    """A day of expected generation on the chronological interval identity.

    Frozen, unlike the mutable day forecast beside it. There is no reason to
    repeat that: a forecast is a statement about a moment, and a caller that
    wants a different one should build it.
    """

    target_day: date
    tz_key: str
    interval_count: int
    #: Expected energy per chronological interval, kWh. ``None`` is "no forecast
    #: for this interval" and is never zero.
    intervals: tuple[float | None, ...] = ()
    #: The tenth and ninetieth percentile bands, on the same index. Retained at
    #: interval resolution rather than as daily scalars because a snapshot is a
    #: historical record: uncertainty for a day that has passed cannot be
    #: back-filled, and it costs nothing to fetch.
    p10: tuple[float | None, ...] = ()
    p90: tuple[float | None, ...] = ()
    #: Whether each interval falls inside the astronomical daylight window.
    #: Advisory: it never modifies a value. A non-zero forecast outside it is the
    #: signature of a timezone or offset bug, which is worth detecting in
    #: production rather than only in tests.
    daylight: tuple[bool, ...] = ()
    #: How many selected sites contributed to each interval.
    sites_contributing: tuple[int, ...] = ()

    available: bool = True
    unavailable_reason: str | None = None

    provenance: PvProvenance = field(default_factory=PvProvenance)
    mapping: PvMappingReport = field(default_factory=PvMappingReport)

    # -- construction ---------------------------------------------------------

    @classmethod
    def unavailable_for(
        cls,
        *,
        target_day: date,
        tz_key: str,
        interval_count: int,
        reason: str,
        provenance: PvProvenance | None = None,
        mapping: PvMappingReport | None = None,
        daylight: tuple[bool, ...] = (),
    ) -> PvForecast:
        """Return a forecast that says why there is no forecast.

        A full-length series of ``None`` rather than an empty one, so a consumer
        indexing by interval finds "not known" at every index instead of an
        ``IndexError`` -- and so the shape of an unavailable day is the shape of
        every other day.
        """
        blanks: tuple[float | None, ...] = (None,) * interval_count
        return cls(
            target_day=target_day,
            tz_key=tz_key,
            interval_count=interval_count,
            intervals=blanks,
            p10=blanks,
            p90=blanks,
            daylight=daylight or (False,) * interval_count,
            sites_contributing=(0,) * interval_count,
            available=False,
            unavailable_reason=reason,
            provenance=provenance or PvProvenance(),
            mapping=mapping or PvMappingReport(),
        )

    # -- derived --------------------------------------------------------------

    @property
    def forecast_intervals(self) -> int:
        """Return how many intervals carry a value."""
        return sum(1 for value in self.intervals if value is not None)

    @property
    def missing_intervals(self) -> int:
        """Return how many intervals carry no value."""
        return self.interval_count - self.forecast_intervals

    @property
    def coverage(self) -> float:
        """Return the fraction of the day that has a forecast."""
        if self.interval_count <= 0:
            return 0.0
        return round(self.forecast_intervals / self.interval_count, 4)

    @property
    def partial_site_intervals(self) -> int:
        """Return how many intervals were summed from fewer than all sites.

        Zero on the aggregate path by construction: there is one series, so it
        either covered an interval or it did not.
        """
        expected = self.provenance.selected_site_count
        if self.provenance.query_mode != PV_QUERY_MODE_PER_SITE or expected <= 1:
            return 0
        return sum(
            1
            for index, count in enumerate(self.sites_contributing)
            if 0 < count < expected and self.intervals[index] is not None
        )

    @property
    def total_kwh(self) -> float | None:
        """Return the expected energy across the intervals that have one."""
        if not self.available or self.forecast_intervals == 0:
            return None
        return round(sum(value for value in self.intervals if value is not None), 4)

    @property
    def total_p10_kwh(self) -> float | None:
        """Return the summed tenth-percentile energy, or ``None``."""
        return _optional_total(self.p10, self.available)

    @property
    def total_p90_kwh(self) -> float | None:
        """Return the summed ninetieth-percentile energy, or ``None``."""
        return _optional_total(self.p90, self.available)

    def energy_at(self, index: int) -> float | None:
        """Return one interval's expected energy, or ``None``."""
        if not 0 <= index < len(self.intervals):
            return None
        return self.intervals[index]

    def power_kw_at(self, index: int) -> float | None:
        """Return one interval's expected average power, derived from its energy.

        Derived rather than stored, so there is exactly one number and no way for
        the two to drift apart.
        """
        energy = self.energy_at(index)
        return None if energy is None else round(energy / _QUARTER_HOURS, 4)

    @property
    def non_daylight_generation_intervals(self) -> int:
        """Return how many intervals forecast generation outside daylight.

        The single best detector for a whole class of timezone and offset bugs,
        and it catches them in production rather than only in tests. Reported,
        never corrected: clamping the source because our astronomy disagrees
        would substitute our model for the source's.
        """
        return sum(
            1
            for index, value in enumerate(self.intervals)
            if value is not None
            and value > 0.0
            and index < len(self.daylight)
            and not self.daylight[index]
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form.

        Counts and totals, never the series: ninety-six numbers have no business
        in a payload capped at sixteen list entries.
        """
        return {
            "target_day": self.target_day.isoformat(),
            "tz_key": self.tz_key,
            "interval_count": self.interval_count,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "forecast_intervals": self.forecast_intervals,
            "missing_intervals": self.missing_intervals,
            "coverage": self.coverage,
            "partial_site_intervals": self.partial_site_intervals,
            "total_kwh": self.total_kwh,
            "total_p10_kwh": self.total_p10_kwh,
            "total_p90_kwh": self.total_p90_kwh,
            "daylight_intervals": sum(1 for flag in self.daylight if flag),
            "generation_outside_daylight": self.non_daylight_generation_intervals,
        }


def _optional_total(values: Sequence[float | None], available: bool) -> float | None:
    """Return the sum of the present values, or ``None`` when there are none."""
    if not available:
        return None
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present), 4)


# --- mapping -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    """One parsed source row."""

    start_utc: datetime
    kw50: float
    kw10: float | None
    kw90: float | None


def _parse_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[_Row], int, int, int]:
    """Return parsed rows plus malformed, duplicate and non-monotonic counts.

    Duplicates are resolved **first wins**, and deliberately: the result must not
    depend on iteration order, and determinism is asserted elsewhere in this
    project rather than hoped for.
    """
    parsed: dict[datetime, _Row] = {}
    malformed = 0
    duplicated = 0
    non_monotonic = 0
    previous: datetime | None = None

    for raw in rows:
        if not isinstance(raw, Mapping):
            malformed += 1
            continue
        start = raw.get("period_start")
        if not isinstance(start, datetime) or start.tzinfo is None:
            # A naive timestamp is refused rather than assumed to be UTC or
            # local. Guessing wrong shifts the whole day, and on this source the
            # value arrives with an explicit offset, so a naive one means the
            # contract changed.
            malformed += 1
            continue
        kw50 = _finite(raw.get("pv_estimate"))
        if kw50 is None or kw50 < 0.0:
            malformed += 1
            continue

        start_utc = start.astimezone(UTC)
        if previous is not None and start_utc < previous:
            non_monotonic += 1
        previous = start_utc

        if start_utc in parsed:
            duplicated += 1
            continue
        parsed[start_utc] = _Row(
            start_utc=start_utc,
            kw50=kw50,
            kw10=_clamp_optional(_finite(raw.get("pv_estimate10"))),
            kw90=_clamp_optional(_finite(raw.get("pv_estimate90"))),
        )

    ordered = sorted(parsed.values(), key=lambda row: row.start_utc)
    return ordered, malformed, duplicated, non_monotonic


def _clamp_optional(value: float | None) -> float | None:
    """Return a non-negative power, or ``None``.

    A negative percentile is not a small zero: generation cannot be negative, so
    the field cannot be interpreted and the band for that row is unknown. The
    median is refused outright by the caller for the same reason.
    """
    if value is None or value < 0.0:
        return None
    return value


def _measure_period_minutes(rows: Sequence[_Row]) -> tuple[int | None, tuple[int, ...]]:
    """Return the modal period length in minutes, and every length observed.

    Measured from the gaps between consecutive rows. A single row has no gap to
    measure, so it yields ``None`` -- which the caller turns into a refusal
    rather than a guess.
    """
    if len(rows) < 2:
        return None, ()
    gaps: list[int] = []
    for earlier, later in pairwise(rows):
        minutes = round((later.start_utc - earlier.start_utc).total_seconds() / 60)
        if minutes > 0:
            gaps.append(minutes)
    if not gaps:
        return None, ()
    observed = tuple(sorted(set(gaps)))
    modal = max(observed, key=gaps.count)
    return modal, observed


@dataclass(frozen=True, slots=True)
class _Series:
    """One site's mapped arrays, before summation."""

    values: list[float | None]
    p10: list[float | None]
    p90: list[float | None]
    present: list[bool]
    report: PvMappingReport


def _map_one_site(
    rows: Sequence[Mapping[str, Any]],
    *,
    interval_count: int,
    index_of: Callable[[datetime], int | None],
) -> _Series:
    """Map one site's rows onto the chronological interval identity.

    ``index_of`` is injected rather than imported. That keeps the storage
    coupling in the caller, where a structural guard confines it to exactly one
    module, and it makes every mapping case testable against a resolver written
    by hand. This module deliberately does not name that resolver at all, not
    even in prose: the guard is a substring check, so a mention would widen it.
    """
    parsed, malformed, duplicated, non_monotonic = _parse_rows(rows)
    period_minutes, observed = _measure_period_minutes(parsed)

    values: list[float | None] = [None] * interval_count
    p10: list[float | None] = [None] * interval_count
    p90: list[float | None] = [None] * interval_count
    present = [False] * interval_count

    mapped = 0
    out_of_range = 0
    refused = 0

    if period_minutes is None or period_minutes % PV_SOURCE_PERIOD_STEP_MINUTES:
        # Not a whole number of planning intervals, so there is no honest way to
        # place it. Refused and reported rather than rounded to the nearest one.
        refused = len(parsed)
        return _Series(
            values,
            p10,
            p90,
            present,
            PvMappingReport(
                rows_received=len(rows),
                rows_malformed=malformed,
                rows_duplicated=duplicated,
                rows_non_monotonic=non_monotonic,
                periods_refused=refused,
                period_minutes=period_minutes,
                period_minutes_observed=observed,
            ),
        )

    quarters_per_period = period_minutes // PV_SOURCE_PERIOD_STEP_MINUTES

    for position, row in enumerate(parsed):
        # A row covers its own period, and never more than the distance to the
        # next row -- rows cannot overlap. The distinction matters when the series
        # has a hole in it: the modal period stays correct, the row before the hole
        # still covers only its own period, and the hole stays a hole. Spreading a
        # row across the whole gap instead would fabricate generation that the
        # source never published, for exactly the intervals it declined to
        # describe.
        span = quarters_per_period
        if position + 1 < len(parsed):
            gap_minutes = round(
                (parsed[position + 1].start_utc - row.start_utc).total_seconds() / 60
            )
            span = min(span, max(1, gap_minutes // PV_SOURCE_PERIOD_STEP_MINUTES))

        placed = False
        for step in range(span):
            start = row.start_utc + timedelta(minutes=QUARTER_MINUTES * step)
            index = index_of(start)
            if index is None or not 0 <= index < interval_count:
                out_of_range += 1
                continue
            # Piecewise-constant: every quarter of the period carries the same
            # average power, so the quarters sum to exactly the period's energy.
            values[index] = row.kw50 * _QUARTER_HOURS
            p10[index] = None if row.kw10 is None else row.kw10 * _QUARTER_HOURS
            p90[index] = None if row.kw90 is None else row.kw90 * _QUARTER_HOURS
            present[index] = True
            placed = True
        if placed:
            mapped += 1

    return _Series(
        values,
        p10,
        p90,
        present,
        PvMappingReport(
            rows_received=len(rows),
            rows_mapped=mapped,
            rows_malformed=malformed,
            rows_duplicated=duplicated,
            rows_out_of_range=out_of_range,
            rows_non_monotonic=non_monotonic,
            period_minutes=period_minutes,
            period_minutes_observed=observed,
        ),
    )


def build_forecast(
    site_rows: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
    *,
    target_day: date,
    tz_key: str,
    interval_count: int,
    index_of: Callable[[datetime], int | None],
    daylight: Sequence[bool] = (),
    provenance: PvProvenance | None = None,
) -> PvForecast:
    """Return the forecast for one day from one or more sites' rows.

    ``site_rows`` is a sequence of ``(site_id, rows)`` pairs. The aggregate path
    passes exactly one pair, whose identifier is
    :data:`~.const.PV_AGGREGATE_SITE`; the subset path passes one pair per
    selected site and the arrays are summed here.

    P10, P50 and P90 are each summed independently. None is derived from another,
    which is why three deliberately different per-site shapes are enough to catch
    a crossed wire.
    """
    base = provenance or PvProvenance()
    per_site = len(site_rows) > 1 or (
        len(site_rows) == 1 and site_rows[0][0] != PV_AGGREGATE_SITE
    )
    base = replace(
        base,
        query_mode=PV_QUERY_MODE_PER_SITE if per_site else PV_QUERY_MODE_AGGREGATE,
        percentile_aggregation=(
            PV_PERCENTILE_COMONOTONIC_SUM
            if per_site and len(site_rows) > 1
            else PV_PERCENTILE_SOURCE_AGGREGATE
        ),
    )
    window = tuple(bool(flag) for flag in daylight[:interval_count]) or (
        (False,) * interval_count
    )
    if len(window) < interval_count:
        window = window + (False,) * (interval_count - len(window))

    if not site_rows or all(not rows for _, rows in site_rows):
        return PvForecast.unavailable_for(
            target_day=target_day,
            tz_key=tz_key,
            interval_count=interval_count,
            reason=PV_UNAVAILABLE_NO_ROWS,
            provenance=base,
            daylight=window,
        )

    series = [
        _map_one_site(rows, interval_count=interval_count, index_of=index_of)
        for _, rows in site_rows
    ]

    report = PvMappingReport()
    for one in series:
        report = report.merged_with(one.report)

    if report.rows_mapped == 0:
        reason = (
            PV_UNAVAILABLE_PERIOD_REFUSED
            if report.periods_refused
            else PV_UNAVAILABLE_UNUSABLE_ROWS
        )
        return PvForecast.unavailable_for(
            target_day=target_day,
            tz_key=tz_key,
            interval_count=interval_count,
            reason=reason,
            provenance=replace(base, period_minutes=report.period_minutes),
            mapping=report,
            daylight=window,
        )

    values = _sum_arrays([one.values for one in series])
    p10 = _sum_arrays([one.p10 for one in series])
    p90 = _sum_arrays([one.p90 for one in series])
    contributing = tuple(
        sum(1 for one in series if one.present[index])
        for index in range(interval_count)
    )

    return PvForecast(
        target_day=target_day,
        tz_key=tz_key,
        interval_count=interval_count,
        intervals=values,
        p10=p10,
        p90=p90,
        daylight=window,
        sites_contributing=contributing,
        available=True,
        unavailable_reason=None,
        provenance=replace(base, period_minutes=report.period_minutes),
        mapping=report,
    )


def _sum_arrays(arrays: Sequence[Sequence[float | None]]) -> tuple[float | None, ...]:
    """Return the element-wise sum, treating absent contributions as absent.

    An interval nobody reported stays ``None``. An interval some reported is the
    sum of those -- a known under-estimate when a selected site is missing, never
    a zero, and flagged as partial by the contributing count rather than by
    quietly filling the gap.
    """
    if not arrays:
        return ()
    length = len(arrays[0])
    result: list[float | None] = []
    for index in range(length):
        present = [
            array[index]
            for array in arrays
            if index < len(array) and array[index] is not None
        ]
        result.append(round(sum(present), 6) if present else None)
    return tuple(result)
