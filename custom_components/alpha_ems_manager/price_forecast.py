"""Phase 6: the price model, its mapping, and its provenance.

Pure. Nothing here imports Home Assistant, opens a socket or names a hostname, so
the whole mapping is exercisable against hand-written blocks. The impure half
lives in ``frank_source`` and does nothing but read published entity state.

Three prices, and only one of them is a measurement of the same kind
-------------------------------------------------------------------

The source publishes a wholesale price and an all-in purchase price per interval.
It publishes **no** export price at all: the upstream endpoint has no such field,
so the export figure is *reconstructed* from the wholesale price plus a
user-configured adjustment. That asymmetry is load-bearing and is why
:attr:`PriceInterval.export_basis` exists -- a configuration-derived estimate must
never be mistaken for a published price.

The asymmetry is not academic. The purchase side carries a fixed floor of energy
tax plus sourcing markup -- 0.129 EUR/kWh on the installation this was built
against -- while the export side carries none of it. So on a negative wholesale
interval, **importing still costs money while exporting earns a negative amount**.
Import and export are not two signs of one number, and no single price field can
answer both questions.

Unknown is never zero
---------------------

An interval with no price is absent from the series. An interval priced ``0.0`` is
a known zero. Beyond the horizon there are no intervals at all -- not
placeholders, not zeroes. A later phase that confused the two would plan free
electricity across a gap in the data.

This module computes no decision, no ranking, no objective and no correction. It
normalises and records.
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
    FRANK_FIELD_FEED_IN_PRICE,
    FRANK_OPTION_APPLY_FEED_IN_VAT,
    FRANK_OPTION_FEED_IN_ADJUSTMENT,
    FRANK_PRICE_PRECISION,
    FRANK_VAT_RATE,
    PRICE_CROSS_CHECK_AGREES,
    PRICE_CROSS_CHECK_DISAGREES,
    PRICE_CROSS_CHECK_NOT_COMPARABLE,
    PRICE_CROSS_CHECK_TOLERANCE_EUR_KWH,
    PRICE_EXPORT_BASIS_ADJUSTMENT,
    PRICE_EXPORT_BASIS_ADJUSTMENT_VAT,
    PRICE_EXPORT_BASIS_API_FIELD,
    PRICE_EXPORT_BASIS_UNKNOWN,
    PRICE_FLAG_COMPONENTS_VARIED,
    PRICE_FLAG_RESOLUTION_DISAGREES,
    PRICE_FLAG_VAT_RATIO_UNEXPECTED,
    PRICE_MAPPING_VERSION,
    PRICE_SOURCE_PERIOD_STEP_MINUTES,
    PRICE_UNAVAILABLE_EMPTY,
    PRICE_UNAVAILABLE_UNUSABLE_ROWS,
    PRICE_VAT_RATIO_TOLERANCE_EUR_KWH,
    QUARTER_MINUTES,
)

#: Length of a fingerprint, in hex characters.
_FINGERPRINT_CHARS = 16

#: Precision the normalised prices are held to. One more than the source's five
#: decimal places, so the reconstruction's own rounding is the only rounding that
#: happens and a stored value is never a re-rounded re-rounding.
_PRICE_DECIMALS = FRANK_PRICE_PRECISION


def _fingerprint(parts: Iterable[Any]) -> str:
    """Return a short stable digest of an ordered sequence of facts."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:_FINGERPRINT_CHARS]


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None``.

    Booleans and strings are refused rather than coerced. A source that started
    publishing ``"0.185"`` instead of ``0.185`` has changed its contract, and
    quietly parsing it would hide that.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


def as_number(value: Any) -> float | None:
    """Return a number the way the price source's own reader does.

    Deliberately identical to the source's ``_as_number``: booleans rejected
    because ``True`` is an ``int`` in Python but never a price, and strings
    rejected outright. The point is not to be lenient or strict in the abstract
    but to **agree with the sensor the user can see** -- a source option of
    ``"0.05"`` makes that sensor use its default, so reading ``0.05`` here would
    put Alpha EMS at odds with the figure on the user's dashboard.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def feed_in_adjustment_of(options: Mapping[str, Any] | None, default: float) -> float:
    """Return the configured export adjustment, replicating the source exactly.

    Falls back to ``default`` when the option is absent *or unusable*, which is
    what the source does -- so an installation that never set it, and one that set
    it to something unparseable, both behave the way their own sensor behaves.
    """
    if not isinstance(options, Mapping):
        return default
    value = as_number(options.get(FRANK_OPTION_FEED_IN_ADJUSTMENT))
    return default if value is None else value


def apply_vat_of(options: Mapping[str, Any] | None, default: bool) -> bool:
    """Return whether VAT is applied, replicating the source's ``bool()`` exactly.

    **Not** a stricter or more sensible boolean parse, and that is the whole
    point. The source calls plain ``bool()`` on the stored option, so the string
    ``"false"`` is *truthy* there and VAT is applied. A better parser here would
    disagree with the running integration, which is the one thing this must never
    do.
    """
    if not isinstance(options, Mapping):
        return default
    return bool(options.get(FRANK_OPTION_APPLY_FEED_IN_VAT, default))


def cross_check(
    ours: float | None,
    theirs: float | None,
    tolerance: float = PRICE_CROSS_CHECK_TOLERANCE_EUR_KWH,
) -> str:
    """Compare one of our figures against the source's own published figure.

    The only check in this phase that can fail when the **source** changes rather
    than when our reading of a fixture changes. A captured artefact proves we read
    the shape we observed; only this proves we still agree with the running
    integration, and it is the direct answer to a defect that shipped because a
    fixture agreed with the parser that wrote it.

    ``not_comparable`` is a third outcome on purpose: no current interval, or an
    unreadable sensor, is an absence of evidence rather than a disagreement.
    """
    if ours is None or theirs is None:
        return PRICE_CROSS_CHECK_NOT_COMPARABLE
    if abs(ours - theirs) <= tolerance:
        return PRICE_CROSS_CHECK_AGREES
    return PRICE_CROSS_CHECK_DISAGREES


# --- one interval -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceInterval:
    """One quarter-hour of price, on the chronological interval identity.

    Four price floats are carried and stored. ``market_price_tax`` is a **source
    field held as received**: it is very nearly ``VAT rate x market price``, and an
    earlier design discarded it for that reason -- but that relation is tax
    legislation rather than arithmetic, it is asserted nowhere the source can
    enforce, and a stored series that dropped the field could not be repaired
    afterwards. It is checked and flagged instead (see :func:`vat_ratio_holds`).
    """

    index: int
    start_utc: datetime
    end_utc: datetime
    #: Measured from the block, never taken from a reported summary.
    source_resolution_minutes: int
    #: The civil day the source filed this block under, retained through the
    #: merge so provenance survives it.
    source_day: date

    # -- source facts ---------------------------------------------------------
    market_price_eur_kwh: float | None = None
    market_price_tax_eur_kwh: float | None = None
    import_price_eur_kwh: float | None = None

    # -- reconstructed, and labelled as such -----------------------------------
    export_price_eur_kwh: float | None = None
    export_basis: str = PRICE_EXPORT_BASIS_UNKNOWN

    @property
    def known(self) -> bool:
        """Return whether this interval carries a usable import price."""
        return self.import_price_eur_kwh is not None

    @property
    def duration_hours(self) -> float:
        """Return the interval's length in hours."""
        return (self.end_utc - self.start_utc).total_seconds() / 3600.0

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics form for a single interval."""
        return {
            "index": self.index,
            "start": self.start_utc.isoformat(),
            "end": self.end_utc.isoformat(),
            "source_resolution_minutes": self.source_resolution_minutes,
            "market_price_eur_kwh": self.market_price_eur_kwh,
            "import_price_eur_kwh": self.import_price_eur_kwh,
            "export_price_eur_kwh": self.export_price_eur_kwh,
            "export_basis": self.export_basis,
        }


def vat_ratio_holds(
    market_price: float | None,
    market_price_tax: float | None,
    rate: float = FRANK_VAT_RATE,
    tolerance: float = PRICE_VAT_RATIO_TOLERANCE_EUR_KWH,
) -> bool | None:
    """Return whether the tax equals the expected share of the wholesale price.

    An **observation**, never a derivation. It held on every block of the live
    capture, and it is exactly the kind of relation that holds until a government
    changes it -- so it is compared and reported rather than relied upon. The
    stored tax is always whatever the source sent.

    ``None`` when either side is missing, which is not the same as a mismatch.
    """
    if market_price is None or market_price_tax is None:
        return None
    return abs(market_price_tax - market_price * rate) <= tolerance


def reconstruct_export_price(
    block: Mapping[str, Any],
    adjustment: float | None,
    apply_vat: bool,
) -> tuple[float | None, str]:
    """Return the export price for one block, and how it was arrived at.

    Mirrors the source's own calculation, branch for branch, because the result
    has to match the return-price sensor the user can see:

    * an explicit feed-in field in the block wins outright -- kept even though the
      live source publishes no such field, because dropping the branch would
      silently prefer a reconstruction over a real figure if one ever appears;
    * otherwise the wholesale price plus the configured adjustment;
    * with VAT applied to that whole sum, never to the wholesale price alone, and
      only when the user turned it on.

    Never clamped. A negative result is a real outcome: exporting during a
    negative wholesale interval costs money.
    """
    explicit = _finite(block.get(FRANK_FIELD_FEED_IN_PRICE))
    if explicit is not None:
        return round(explicit, _PRICE_DECIMALS), PRICE_EXPORT_BASIS_API_FIELD

    market = _finite(block.get("market_price"))
    if market is None or adjustment is None:
        # No wholesale figure, or a configuration that could not be read. Either
        # way there is no honest reconstruction -- and a guessed adjustment would
        # be worse than no figure, because it would look like one.
        return None, PRICE_EXPORT_BASIS_UNKNOWN

    total = market + adjustment
    if apply_vat:
        total *= 1.0 + FRANK_VAT_RATE
        return round(total, _PRICE_DECIMALS), PRICE_EXPORT_BASIS_ADJUSTMENT_VAT
    return round(total, _PRICE_DECIMALS), PRICE_EXPORT_BASIS_ADJUSTMENT


# --- provenance ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceProvenance:
    """Every source fact needed to decide whether two series are comparable.

    Read, never inferred. The market timezone is recorded here and used nowhere
    else: the source defines its own market day, and Alpha EMS deliberately does
    not reimplement its publication schedule -- availability comes from the
    source's own signal, never from a clock comparison.
    """

    source_entry_id: str | None = None
    source_country: str | None = None
    market_timezone: str | None = None
    #: The entity ids actually resolved, so a rename is visible.
    today_entity_id: str | None = None
    tomorrow_entity_id: str | None = None
    availability_entity_id: str | None = None

    # -- what the source was configured to do ---------------------------------
    feed_in_adjustment: float | None = None
    apply_feed_in_vat: bool | None = None
    options_readable: bool = True
    #: Recorded because the live installation has these two numerically equal,
    #: which would let a wrong-field bug reconstruct the right answer. Synthetic
    #: fixtures deliberately use distinct values; this records reality.
    sourcing_markup_eur_kwh: float | None = None
    energy_tax_eur_kwh: float | None = None

    # -- how the series was produced ------------------------------------------
    reported_resolution_minutes: int | None = None
    measured_resolution_minutes: int | None = None
    mapping_version: int = PRICE_MAPPING_VERSION
    source_updated_at: datetime | None = None
    observed_freshness: bool = True

    # -- the cross-checks ------------------------------------------------------
    import_cross_check: str | None = None
    export_cross_check: str | None = None

    @property
    def source_key(self) -> tuple[Any, ...]:
        """Return the facts that make two series comparable at all."""
        return (
            self.source_entry_id,
            self.source_country,
            self.feed_in_adjustment,
            self.apply_feed_in_vat,
            self.mapping_version,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics and storage form.

        Carries no credential by construction: the source is unauthenticated and
        only the named fields above are ever read out of it.
        """
        return {
            "source_entry_id": self.source_entry_id,
            "source_country": self.source_country,
            "market_timezone": self.market_timezone,
            "today_entity_id": self.today_entity_id,
            "tomorrow_entity_id": self.tomorrow_entity_id,
            "availability_entity_id": self.availability_entity_id,
            "feed_in_adjustment": self.feed_in_adjustment,
            "apply_feed_in_vat": self.apply_feed_in_vat,
            "options_readable": self.options_readable,
            "sourcing_markup_eur_kwh": self.sourcing_markup_eur_kwh,
            "energy_tax_eur_kwh": self.energy_tax_eur_kwh,
            "reported_resolution_minutes": self.reported_resolution_minutes,
            "measured_resolution_minutes": self.measured_resolution_minutes,
            "mapping_version": self.mapping_version,
            "source_updated_at": (
                None
                if self.source_updated_at is None
                else self.source_updated_at.isoformat()
            ),
            "freshness_is_observed": self.observed_freshness,
            "import_cross_check": self.import_cross_check,
            "export_cross_check": self.export_cross_check,
            "export_note": (
                "the export price is reconstructed from the wholesale price and "
                "the source's own configured adjustment, because the upstream "
                "publishes no feed-in field; export_basis records which rule was "
                "used and it is never presented as a published price"
            ),
            "market_timezone_note": (
                "recorded for context only. availability comes from the source's "
                "own signal, never from comparing a clock against an expected "
                "publication time -- publication can be early or late"
            ),
        }


@dataclass(frozen=True, slots=True)
class PriceMappingReport:
    """What the mapping did, so it can be audited from a diagnostics download."""

    blocks_received: int = 0
    blocks_mapped: int = 0
    blocks_malformed: int = 0
    blocks_duplicated: int = 0
    blocks_out_of_range: int = 0
    blocks_non_monotonic: int = 0
    periods_refused: int = 0
    period_minutes_observed: tuple[int, ...] = ()

    def merged_with(self, other: PriceMappingReport) -> PriceMappingReport:
        """Return the sum of two reports, for the two-day merge."""
        return PriceMappingReport(
            blocks_received=self.blocks_received + other.blocks_received,
            blocks_mapped=self.blocks_mapped + other.blocks_mapped,
            blocks_malformed=self.blocks_malformed + other.blocks_malformed,
            blocks_duplicated=self.blocks_duplicated + other.blocks_duplicated,
            blocks_out_of_range=(self.blocks_out_of_range + other.blocks_out_of_range),
            blocks_non_monotonic=(
                self.blocks_non_monotonic + other.blocks_non_monotonic
            ),
            periods_refused=self.periods_refused + other.periods_refused,
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
            "blocks_received": self.blocks_received,
            "blocks_mapped": self.blocks_mapped,
            "blocks_malformed": self.blocks_malformed,
            "blocks_duplicated": self.blocks_duplicated,
            "blocks_out_of_range": self.blocks_out_of_range,
            "blocks_non_monotonic": self.blocks_non_monotonic,
            "periods_refused": self.periods_refused,
            "period_minutes_observed": list(self.period_minutes_observed),
            "rule": (
                "blocks are placed by absolute instant, never by array position "
                "and never by assuming a day length; intervals are half-open "
                "[start, end), and a source period spanning several quarters is "
                "split piecewise-constant because a price is a rate"
            ),
        }


# --- the series ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceForecast:
    """The known price series, and the honest edge of what is known.

    Holds no policy. Nothing here ranks an interval, picks a cheapest window or
    expresses an objective -- and no consumer in the decision layer reads it at
    all, which is what makes "prices change no decision" a structural property
    rather than a promise.
    """

    tz_key: str
    intervals: tuple[PriceInterval, ...] = ()

    #: The Alpha EMS civil day this series was resolved against, and how many
    #: planning intervals that day holds -- 92, 96 or 100. Both are needed to say
    #: anything about coverage, and coverage is the honest way to report the
    #: market day and the local day not being the same span.
    target_day: date | None = None
    expected_intervals: int = 0

    today_available: bool = False
    tomorrow_available: bool = False
    today_reason: str | None = None
    tomorrow_reason: str | None = None

    provenance: PriceProvenance = field(default_factory=PriceProvenance)
    mapping: PriceMappingReport = field(default_factory=PriceMappingReport)
    flags: tuple[str, ...] = ()

    # -- availability ---------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return whether any usable price is known.

        Today alone is a complete, correct result: between market midnight and
        the next day's publication that is the healthy state, and it must not read
        as a degraded source.
        """
        return self.today_available and bool(self.intervals)

    @property
    def coverage(self) -> float:
        """Return the fraction of the target day carrying a known price.

        **Below 1.0 is normal, not a fault.** The source publishes a *market* day
        -- midnight to midnight in the market's own zone -- while this series is
        built on the Home Assistant civil day. For anyone running Home Assistant
        outside the market zone those are different spans, so part of the local
        day is legitimately priced by a market day that has not been published
        yet. Reported, never repaired by extrapolation.
        """
        if self.expected_intervals <= 0:
            return 0.0
        return min(1.0, self.intervals_known / self.expected_intervals)

    @property
    def missing_intervals(self) -> int:
        """Return how many intervals of the target day carry no price."""
        if self.expected_intervals <= 0:
            return 0
        return max(0, self.expected_intervals - self.intervals_known)

    # -- the horizon ----------------------------------------------------------

    @property
    def known_window_start(self) -> datetime | None:
        """Return the start of the first interval of the contiguous known run."""
        run = self._contiguous_run()
        return run[0].start_utc if run else None

    @property
    def economic_price_horizon_end(self) -> datetime | None:
        """Return the end instant of the last interval known *contiguously*.

        Informational. It causes nothing in this phase -- no charge, no discharge,
        no reserve, no plan change -- and exists so a later phase inherits one
        definition of "prices are known this far" rather than inventing its own.

        Contiguity is deliberate: the run stops at the first gap. Knowing prices
        on both sides of a hole is not knowing them continuously, and anything
        planning across the hole would be planning over invented data. Isolated
        later intervals stay visible through :attr:`intervals_beyond_horizon`.
        """
        run = self._contiguous_run()
        return run[-1].end_utc if run else None

    @property
    def intervals_known(self) -> int:
        """Return how many intervals carry a usable price."""
        return sum(1 for interval in self.intervals if interval.known)

    @property
    def intervals_beyond_horizon(self) -> int:
        """Return how many known intervals lie after a gap.

        Not lost, just not contiguous. Reported so a hole in the middle of the
        series does not make everything past it invisible.
        """
        return max(0, self.intervals_known - len(self._contiguous_run()))

    def _contiguous_run(self) -> tuple[PriceInterval, ...]:
        """Return the unbroken run of known intervals from the earliest one."""
        known = [interval for interval in self.intervals if interval.known]
        if not known:
            return ()
        run = [known[0]]
        for earlier, later in pairwise(known):
            if later.start_utc != earlier.end_utc:
                break
            run.append(later)
        return tuple(run)

    # -- lookup ---------------------------------------------------------------

    def interval_at(self, moment: datetime) -> PriceInterval | None:
        """Return the interval containing ``moment``, or ``None``.

        Half-open ``[start, end)``, compared on absolute instants. There is
        deliberately no fallback to a neighbouring interval: an instant with no
        price has no price.
        """
        target = moment.astimezone(UTC)
        for interval in self.intervals:
            if interval.start_utc <= target < interval.end_utc:
                return interval
        return None

    # -- totals ---------------------------------------------------------------

    @property
    def import_price_available(self) -> bool:
        """Return whether any interval carries an import price."""
        return any(
            interval.import_price_eur_kwh is not None for interval in self.intervals
        )

    @property
    def export_price_available(self) -> bool:
        """Return whether any interval carries an export price."""
        return any(
            interval.export_price_eur_kwh is not None for interval in self.intervals
        )

    @property
    def market_price_available(self) -> bool:
        """Return whether any interval carries a wholesale price."""
        return any(
            interval.market_price_eur_kwh is not None for interval in self.intervals
        )

    def fingerprint(self) -> str:
        """Return the content fingerprint, for change-triggered issuance."""
        return _fingerprint(
            [
                self.tz_key,
                self.today_available,
                self.tomorrow_available,
                self.today_reason,
                self.tomorrow_reason,
                tuple(
                    (
                        interval.start_utc.isoformat(),
                        interval.market_price_eur_kwh,
                        interval.market_price_tax_eur_kwh,
                        interval.import_price_eur_kwh,
                        interval.export_price_eur_kwh,
                    )
                    for interval in self.intervals
                ),
                self.provenance.source_key,
            ]
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form.

        Counts, edges and status. Never the series: a day of intervals has no
        business in a payload capped at sixteen list entries.
        """
        horizon = self.economic_price_horizon_end
        start = self.known_window_start
        day = self.target_day
        return {
            "available": self.available,
            "today_available": self.today_available,
            "today_reason": self.today_reason,
            "tomorrow_available": self.tomorrow_available,
            "tomorrow_reason": self.tomorrow_reason,
            "target_day": None if day is None else day.isoformat(),
            "expected_intervals": self.expected_intervals,
            "interval_count": len(self.intervals),
            "intervals_known": self.intervals_known,
            "coverage": round(self.coverage, 4),
            "missing_intervals": self.missing_intervals,
            "intervals_beyond_horizon": self.intervals_beyond_horizon,
            "known_window_start": None if start is None else start.isoformat(),
            "economic_price_horizon_end": (
                None if horizon is None else horizon.isoformat()
            ),
            "market_price_available": self.market_price_available,
            "import_price_available": self.import_price_available,
            "export_price_available": self.export_price_available,
            "flags": list(self.flags),
            "coverage_note": (
                "coverage below 1.0 is normal rather than a fault: the source "
                "publishes a market day and this series is built on the local "
                "civil day, which are the same span only when home assistant "
                "runs in the market's own zone"
            ),
            "horizon_note": (
                "the end of the last contiguously known interval. beyond it there "
                "are no intervals at all -- which is not the same as a price of "
                "zero, and a known zero stays a present interval"
            ),
            "decides_nothing": (
                "price information is recorded and reported. it reaches no "
                "battery decision, no policy, no simulation and no command in "
                "this release"
            ),
        }


# --- mapping ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    """One parsed source block."""

    start_utc: datetime
    end_utc: datetime
    duration_minutes: int
    source_day: date
    market_price: float | None
    market_price_tax: float | None
    import_price: float
    export_price: float | None
    export_basis: str
    sourcing_markup: float | None
    energy_tax: float | None
    vat_ratio_ok: bool | None


def _parse_moment(value: Any) -> datetime | None:
    """Return an offset-aware instant from a source timestamp.

    A naive timestamp is refused rather than assumed to be UTC or local: guessing
    wrong shifts a whole day, and the source always publishes an offset, so a
    naive one means the contract changed.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parse_blocks(
    blocks: Sequence[Mapping[str, Any]],
    *,
    adjustment: float | None,
    apply_vat: bool,
) -> tuple[list[_Row], PriceMappingReport]:
    """Return parsed rows and a report, refusing anything unusable."""
    parsed: dict[datetime, _Row] = {}
    malformed = duplicated = non_monotonic = 0
    previous: datetime | None = None
    durations: set[int] = set()

    for raw in blocks:
        if not isinstance(raw, Mapping):
            malformed += 1
            continue
        start = _parse_moment(raw.get("from"))
        end = _parse_moment(raw.get("till"))
        import_price = _finite(raw.get("total_price_eur_kwh"))
        if start is None or end is None or end <= start or import_price is None:
            malformed += 1
            continue

        start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
        if previous is not None and start_utc < previous:
            non_monotonic += 1
        previous = start_utc
        if start_utc in parsed:
            duplicated += 1
            continue

        minutes = round((end_utc - start_utc).total_seconds() / 60)
        durations.add(minutes)
        market = _finite(raw.get("market_price"))
        tax = _finite(raw.get("market_price_tax"))
        export, basis = reconstruct_export_price(raw, adjustment, apply_vat)
        parsed[start_utc] = _Row(
            start_utc=start_utc,
            end_utc=end_utc,
            duration_minutes=minutes,
            # The civil date the source filed the block under, taken from the
            # published offset rather than recomputed in another zone.
            source_day=start.date(),
            market_price=market,
            market_price_tax=tax,
            import_price=import_price,
            export_price=export,
            export_basis=basis,
            sourcing_markup=_finite(raw.get("sourcing_markup_price")),
            energy_tax=_finite(raw.get("energy_tax_price")),
            vat_ratio_ok=vat_ratio_holds(market, tax),
        )

    report = PriceMappingReport(
        blocks_received=len(blocks),
        blocks_malformed=malformed,
        blocks_duplicated=duplicated,
        blocks_non_monotonic=non_monotonic,
        period_minutes_observed=tuple(sorted(durations)),
    )
    return sorted(parsed.values(), key=lambda row: row.start_utc), report


def build_price_forecast(
    day_blocks: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
    *,
    tz_key: str,
    index_of: Callable[[datetime], int | None],
    target_day: date | None = None,
    expected_intervals: int = 0,
    adjustment: float | None,
    apply_vat: bool,
    today_available: bool,
    tomorrow_available: bool,
    today_reason: str | None = None,
    tomorrow_reason: str | None = None,
    provenance: PriceProvenance | None = None,
    extra_flags: Sequence[str] = (),
) -> PriceForecast:
    """Return one normalised series from the source's day arrays.

    ``day_blocks`` is a sequence of ``(label, blocks)`` pairs -- normally today and
    tomorrow. Both days feed one chronological series; the block's own civil day
    is retained per interval so the merge does not erase where it came from.

    A source period longer than one planning interval is split
    **piecewise-constant**: a price is a *rate*, so every quarter of an hourly
    block carries the same rate. Interpolating between periods would invent a
    price nobody published.
    """
    base = provenance or PriceProvenance()
    intervals: list[PriceInterval] = []
    report = PriceMappingReport()
    flags: list[str] = list(extra_flags)
    markups: set[float] = set()
    taxes: set[float] = set()
    vat_mismatch = False
    measured: set[int] = set()

    for _label, blocks in day_blocks:
        rows, day_report = _parse_blocks(
            blocks, adjustment=adjustment, apply_vat=apply_vat
        )
        mapped = out_of_range = refused = 0

        for row in rows:
            if row.duration_minutes % PRICE_SOURCE_PERIOD_STEP_MINUTES:
                # Not a whole number of planning intervals, so there is no honest
                # way to place it. Refused and counted rather than rounded.
                refused += 1
                continue
            measured.add(row.duration_minutes)
            steps = row.duration_minutes // PRICE_SOURCE_PERIOD_STEP_MINUTES
            placed = False
            for step in range(steps):
                start = row.start_utc + timedelta(minutes=QUARTER_MINUTES * step)
                index = index_of(start)
                if index is None:
                    out_of_range += 1
                    continue
                intervals.append(
                    PriceInterval(
                        index=index,
                        start_utc=start,
                        end_utc=start + timedelta(minutes=QUARTER_MINUTES),
                        source_resolution_minutes=row.duration_minutes,
                        source_day=row.source_day,
                        market_price_eur_kwh=row.market_price,
                        market_price_tax_eur_kwh=row.market_price_tax,
                        import_price_eur_kwh=row.import_price,
                        export_price_eur_kwh=row.export_price,
                        export_basis=row.export_basis,
                    )
                )
                placed = True
            if placed:
                mapped += 1
            if row.sourcing_markup is not None:
                markups.add(row.sourcing_markup)
            if row.energy_tax is not None:
                taxes.add(row.energy_tax)
            if row.vat_ratio_ok is False:
                vat_mismatch = True

        report = report.merged_with(
            replace(
                day_report,
                blocks_mapped=mapped,
                blocks_out_of_range=out_of_range,
                periods_refused=refused,
            )
        )

    intervals.sort(key=lambda interval: interval.start_utc)

    if vat_mismatch:
        flags.append(PRICE_FLAG_VAT_RATIO_UNEXPECTED)
    if len(markups) > 1 or len(taxes) > 1:
        flags.append(PRICE_FLAG_COMPONENTS_VARIED)

    measured_resolution = min(measured) if measured else None
    reported = base.reported_resolution_minutes
    if (
        reported is not None
        and measured_resolution is not None
        and reported != measured_resolution
    ):
        # The reported summary is derived from the first block alone in the
        # source, so a mixed or unexpected resolution can be mislabelled there.
        # The measured value drives the mapping; the disagreement is reported.
        flags.append(PRICE_FLAG_RESOLUTION_DISAGREES)

    resolved_today = today_available and bool(intervals)
    resolved_reason = today_reason
    if today_available and not intervals:
        resolved_reason = (
            PRICE_UNAVAILABLE_EMPTY
            if not report.blocks_received
            else PRICE_UNAVAILABLE_UNUSABLE_ROWS
        )

    return PriceForecast(
        tz_key=tz_key,
        intervals=tuple(intervals),
        target_day=target_day,
        expected_intervals=expected_intervals,
        today_available=resolved_today,
        tomorrow_available=tomorrow_available,
        today_reason=resolved_reason,
        tomorrow_reason=tomorrow_reason,
        provenance=replace(
            base,
            feed_in_adjustment=adjustment,
            apply_feed_in_vat=apply_vat,
            measured_resolution_minutes=measured_resolution,
            sourcing_markup_eur_kwh=(
                next(iter(markups)) if len(markups) == 1 else None
            ),
            energy_tax_eur_kwh=next(iter(taxes)) if len(taxes) == 1 else None,
        ),
        mapping=report,
        flags=tuple(dict.fromkeys(flags)),
    )


def unavailable_price_forecast(
    *,
    tz_key: str,
    reason: str,
    target_day: date | None = None,
    expected_intervals: int = 0,
    provenance: PriceProvenance | None = None,
) -> PriceForecast:
    """Return a series that says why there is no series.

    No intervals at all, rather than a run of empty ones: an absent price is an
    absent interval, and manufacturing placeholders is how a later phase ends up
    reading a gap as free electricity.
    """
    return PriceForecast(
        tz_key=tz_key,
        intervals=(),
        target_day=target_day,
        expected_intervals=expected_intervals,
        today_available=False,
        tomorrow_available=False,
        today_reason=reason,
        tomorrow_reason=reason,
        provenance=provenance or PriceProvenance(),
    )


# --- evidence -----------------------------------------------------------------

#: Per-interval export basis, stored as one character each. A basis can differ
#: between intervals -- an upstream that published an explicit figure for part of
#: a day would produce exactly that -- so it is kept per interval rather than
#: reduced to one day-level label that would be wrong for some of it.
_BASIS_CODES: dict[str, str] = {
    PRICE_EXPORT_BASIS_API_FIELD: "a",
    PRICE_EXPORT_BASIS_ADJUSTMENT: "m",
    PRICE_EXPORT_BASIS_ADJUSTMENT_VAT: "v",
    PRICE_EXPORT_BASIS_UNKNOWN: "-",
}
_BASIS_FROM_CODE: dict[str, str] = {code: basis for basis, code in _BASIS_CODES.items()}


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """What prices were known for one day, at one instant.

    Why record this at all, when nothing in this release learns from it: **which
    future prices were visible when a plan was made is irrecoverable afterwards.**
    Prices are revised and republished, so a later phase reading today's series
    cannot tell what was on screen at nine in the morning. That is a hindsight
    bias which cannot be fixed retroactively, only avoided in advance.

    Four floats an interval, and ``market_price_tax`` is one of them. An earlier
    design stored three and derived the tax from the twenty-one per cent relation
    it satisfies on every observed block. That was revoked: the relation is VAT
    legislation rather than arithmetic, the rate can change, and a stored series
    that discarded the field could not be repaired afterwards -- which would
    defeat the only reason for storing anything. It is checked and flagged
    instead.

    The two fixed components sit at day level, with a flag when they vary within
    the day. That is a genuine observation about contract terms rather than an
    assumed identity, and it is what lets a later phase tell "the market moved"
    from "the energy tax changed on the first of January".

    Holes are holes. The arrays are the length of the day and carry ``None``
    where no price was known, which is not a price of zero.
    """

    issued_at: datetime
    target_day: date
    tz_key: str
    interval_count: int

    available: bool
    unavailable_reason: str | None
    tomorrow_available: bool
    tomorrow_reason: str | None

    market_price: tuple[float | None, ...]
    market_price_tax: tuple[float | None, ...]
    import_price: tuple[float | None, ...]
    export_price: tuple[float | None, ...]
    export_basis: tuple[str, ...]

    sourcing_markup_eur_kwh: float | None
    energy_tax_eur_kwh: float | None

    known_window_start: datetime | None
    economic_price_horizon_end: datetime | None
    intervals_known: int
    intervals_beyond_horizon: int

    flags: tuple[str, ...]
    fingerprint: str
    mapping_version: int = PRICE_MAPPING_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the compact serialisable form.

        Short keys, like every other stored document here: a year of quarter-hour
        arrays is where the bytes are. The raw source arrays are **not** stored --
        twenty-five kilobytes a state change, and reconstructible from these four
        series plus the day-level components.
        """
        return {
            "at": self.issued_at.isoformat(),
            "tz": self.tz_key,
            "n": self.interval_count,
            "a": 1 if self.available else 0,
            "r": self.unavailable_reason,
            "ta": 1 if self.tomorrow_available else 0,
            "tr": self.tomorrow_reason,
            "mp": list(self.market_price),
            "mt": list(self.market_price_tax),
            "ip": list(self.import_price),
            "xp": list(self.export_price),
            "xb": "".join(_BASIS_CODES.get(basis, "-") for basis in self.export_basis),
            "sm": self.sourcing_markup_eur_kwh,
            "et": self.energy_tax_eur_kwh,
            "ws": (
                None
                if self.known_window_start is None
                else self.known_window_start.isoformat()
            ),
            "he": (
                None
                if self.economic_price_horizon_end is None
                else self.economic_price_horizon_end.isoformat()
            ),
            "k": self.intervals_known,
            "bh": self.intervals_beyond_horizon,
            "fl": list(self.flags),
            "f": self.fingerprint,
            "mv": self.mapping_version,
        }

    @classmethod
    def from_dict(cls, target_day: date, raw: Any) -> PriceSnapshot | None:
        """Rebuild a snapshot, or return ``None`` when the entry is unusable."""
        if not isinstance(raw, Mapping):
            return None
        issued = _parse_stored_moment(raw.get("at"))
        count = raw.get("n")
        if issued is None or not isinstance(count, int) or isinstance(count, bool):
            return None
        if not 1 <= count <= 2 * 96:
            return None
        tz_key = raw.get("tz")
        codes = raw.get("xb") if isinstance(raw.get("xb"), str) else ""
        return cls(
            issued_at=issued,
            target_day=target_day,
            tz_key=tz_key if isinstance(tz_key, str) and tz_key else "UTC",
            interval_count=count,
            available=bool(raw.get("a")),
            unavailable_reason=(raw["r"] if isinstance(raw.get("r"), str) else None),
            tomorrow_available=bool(raw.get("ta")),
            tomorrow_reason=(raw["tr"] if isinstance(raw.get("tr"), str) else None),
            market_price=_stored_series(raw.get("mp"), count),
            market_price_tax=_stored_series(raw.get("mt"), count),
            import_price=_stored_series(raw.get("ip"), count),
            export_price=_stored_series(raw.get("xp"), count),
            export_basis=tuple(
                _BASIS_FROM_CODE.get(
                    codes[index] if index < len(codes) else "-",
                    PRICE_EXPORT_BASIS_UNKNOWN,
                )
                for index in range(count)
            ),
            sourcing_markup_eur_kwh=_finite(raw.get("sm")),
            energy_tax_eur_kwh=_finite(raw.get("et")),
            known_window_start=_parse_stored_moment(raw.get("ws")),
            economic_price_horizon_end=_parse_stored_moment(raw.get("he")),
            intervals_known=(raw["k"] if isinstance(raw.get("k"), int) else 0),
            intervals_beyond_horizon=(
                raw["bh"] if isinstance(raw.get("bh"), int) else 0
            ),
            flags=tuple(
                str(flag) for flag in (raw.get("fl") or []) if isinstance(flag, str)
            ),
            fingerprint=str(raw.get("f") or ""),
            mapping_version=(
                raw["mv"] if isinstance(raw.get("mv"), int) else PRICE_MAPPING_VERSION
            ),
        )


def _stored_series(raw: Any, count: int) -> tuple[float | None, ...]:
    """Return a fixed-length series of optional finite floats."""
    source = raw if isinstance(raw, list) else []
    return tuple(
        _finite(source[index]) if index < len(source) else None
        for index in range(count)
    )


def _parse_stored_moment(raw: Any) -> datetime | None:
    """Return a stored timestamp, or ``None`` when it cannot be read."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def build_price_snapshot(
    forecast: PriceForecast, *, issued_at: datetime, interval_count: int
) -> PriceSnapshot:
    """Return the persistable record of one price issuance.

    The series is sparse in memory -- it holds only the intervals it knows -- and
    dense on disk, indexed by the day's own interval identity. Both are the same
    fact stated two ways, and the dense form is what makes a hole on disk
    unambiguous rather than a shorter array.
    """
    market: list[float | None] = [None] * interval_count
    tax: list[float | None] = [None] * interval_count
    import_price: list[float | None] = [None] * interval_count
    export_price: list[float | None] = [None] * interval_count
    basis: list[str] = [PRICE_EXPORT_BASIS_UNKNOWN] * interval_count

    for interval in forecast.intervals:
        if not 0 <= interval.index < interval_count:
            continue
        market[interval.index] = interval.market_price_eur_kwh
        tax[interval.index] = interval.market_price_tax_eur_kwh
        import_price[interval.index] = interval.import_price_eur_kwh
        export_price[interval.index] = interval.export_price_eur_kwh
        basis[interval.index] = interval.export_basis

    return PriceSnapshot(
        issued_at=issued_at,
        target_day=forecast.target_day or issued_at.date(),
        tz_key=forecast.tz_key,
        interval_count=interval_count,
        available=forecast.available,
        unavailable_reason=forecast.today_reason,
        tomorrow_available=forecast.tomorrow_available,
        tomorrow_reason=forecast.tomorrow_reason,
        market_price=tuple(market),
        market_price_tax=tuple(tax),
        import_price=tuple(import_price),
        export_price=tuple(export_price),
        export_basis=tuple(basis),
        sourcing_markup_eur_kwh=forecast.provenance.sourcing_markup_eur_kwh,
        energy_tax_eur_kwh=forecast.provenance.energy_tax_eur_kwh,
        known_window_start=forecast.known_window_start,
        economic_price_horizon_end=forecast.economic_price_horizon_end,
        intervals_known=forecast.intervals_known,
        intervals_beyond_horizon=forecast.intervals_beyond_horizon,
        flags=forecast.flags,
        fingerprint=forecast.fingerprint(),
    )
