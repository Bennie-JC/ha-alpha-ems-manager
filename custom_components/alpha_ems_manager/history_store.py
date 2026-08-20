"""Persistent, partitioned storage for Phase-2 forecast evidence.

Separate from the learning history on purpose. The two have different write
patterns, different retention horizons and, most importantly, different failure
blast radii: the learning document is discarded outright by its own schema
migration, and forecast evidence that vanishes with it is not evidence. Neither
store can damage the other here.

Layout
------

``alpha_ems_manager.<entry_id>.forecast_index``
    Always loaded, and small. Carries the schema version, one lightweight row
    per target day -- interval count, the fingerprints already kept, whether the
    day is finalised, and the day's summary facts -- and the list of month
    partitions that exist.

    Because the fingerprints live here, the hot path costs no I/O at all: a
    refresh that produces the forecast it produced fifteen minutes ago compares
    two strings in memory and stops.

``alpha_ems_manager.<entry_id>.forecast.<YYYY-MM>``
    One partition per calendar month of *target* days, holding the per-interval
    prediction arrays and the matched actuals. Loaded only when a day in that
    month is written, finalised or scored.

Home Assistant's ``Store`` rewrites an entire document on every save. A single
year-long file would put roughly a megabyte through the executor on each
issuance and would lose the whole history to one corrupt byte; a month partition
is about a hundred kilobytes and confines the damage to the month.

Two rules carried over from the learning store, both of which exist there
because both were once broken and both destroyed history:

* **Never write after a failed read.** A store that could not be read refuses to
  write for the rest of the session, so an empty in-memory view can never be
  flushed over a file whose only problem was a transient I/O error.
* **Prune before inserting, against a clamped reference.** A clock excursion
  must not be able to define "now" and delete the retention window behind it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    FORECAST_INDEX_KEY_TEMPLATE,
    FORECAST_MAX_SNAPSHOTS_PER_TARGET,
    FORECAST_MONTH_KEY_TEMPLATE,
    FORECAST_RAW_RETENTION_DAYS,
    FORECAST_STORAGE_MINOR_VERSION,
    FORECAST_STORAGE_VERSION,
    FORECAST_STORE_SAVE_DELAY,
    FORECAST_SUMMARY_RETENTION_DAYS,
)
from .forecast_history import DayOutcome, ForecastSnapshot
from .price_forecast import PriceSnapshot
from .pv_forecast import PvOutcome, PvSnapshot

_LOGGER = logging.getLogger(__name__)


def month_key(day: date) -> str:
    """Return the partition key a target day belongs to."""
    return f"{day.year:04d}-{day.month:02d}"


def _parse_day(key: Any) -> date | None:
    """Return the date a document key names, or ``None`` when unusable."""
    try:
        return date.fromisoformat(str(key))
    except (TypeError, ValueError):
        return None


class _ForecastStoreBackend(Store[dict[str, Any]]):
    """Store subclass that discards an unmappable older schema rather than guess."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise the backend with its migration flag cleared."""
        super().__init__(*args, **kwargs)
        #: Set when a document written under an incompatible major version was
        #: thrown away, so diagnostics can distinguish "a migration discarded
        #: this" from "this installation is new".
        self.discarded_legacy_document = False

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Discard an incompatible document rather than reinterpreting it.

        There is no earlier major version yet, so this is a guard for a future
        one. Reading an unknown layout as if it were this one would produce
        forecast evidence that is confidently wrong, which is worse than
        starting the evidence again -- the learning history it describes is
        untouched either way.
        """
        if old_major_version > FORECAST_STORAGE_VERSION:
            # Written by a newer release. Refusing to reinterpret it downward is
            # the only safe move; the file itself is left alone because writes
            # are suspended by the caller when this happens.
            self.discarded_legacy_document = True
            return {}
        if old_major_version < FORECAST_STORAGE_VERSION:
            self.discarded_legacy_document = True
            _LOGGER.warning(
                "Discarding Alpha EMS Manager forecast history written under "
                "schema v%s: it cannot be mapped onto the v%s layout. Learning "
                "history is untouched; forecast-error evidence restarts from "
                "this point",
                old_major_version,
                FORECAST_STORAGE_VERSION,
            )
            return {}
        return old_data


@dataclass(slots=True)
class DayIndexRow:
    """The lightweight per-target-day record kept in the index document.

    Deliberately holds no per-interval array. Everything the hot path and the
    lifecycle counts need -- how many snapshots exist, what they fingerprint to,
    whether the day is finalised, and the reduced facts behind its summary -- is
    answerable from here without touching a month partition.
    """

    #: Real interval count of the target civil day, while raw evidence is kept.
    interval_count: int | None = None
    #: Fingerprints of the snapshots retained, oldest first.
    fingerprints: list[str] = field(default_factory=list)
    #: ISO instant the day was finalised, or ``None`` while it is still open.
    finalized_at: str | None = None
    #: Reduced summary facts, kept long after the raw arrays are pruned. These
    #: are sufficient statistics, not metric definitions: MAE is ``abs_error /
    #: compared`` and WAPE is ``abs_error / actual``, so a later release can
    #: still change how it reports them.
    summary: dict[str, Any] | None = None
    #: True once the per-interval arrays have been pruned from the partitions.
    raw_pruned: bool = False
    #: Content fingerprints of the photovoltaic issuances recorded for this day.
    #: Kept in the index rather than the partition so the ninety-odd refreshes a
    #: day that change nothing need not load a month of arrays to discover it.
    pv_fingerprints: list[str] = field(default_factory=list)
    #: Content fingerprints of the price issuances recorded for this day, for the
    #: same reason: the source republishes a handful of times a day, so the
    #: overwhelmingly common answer is "nothing changed" and it should not cost a
    #: partition load to reach it.
    price_fingerprints: list[str] = field(default_factory=list)

    @property
    def snapshot_count(self) -> int:
        """Return how many immutable snapshots this day retains."""
        return len(self.fingerprints)

    def to_dict(self) -> dict[str, Any]:
        """Return the compact serialisable form."""
        payload: dict[str, Any] = {}
        if self.interval_count is not None:
            payload["n"] = self.interval_count
        if self.fingerprints:
            payload["fp"] = list(self.fingerprints)
        if self.finalized_at is not None:
            payload["fin"] = self.finalized_at
        if self.summary is not None:
            payload["sum"] = self.summary
        if self.raw_pruned:
            payload["rp"] = True
        if self.pv_fingerprints:
            payload["pvfp"] = list(self.pv_fingerprints)
        if self.price_fingerprints:
            payload["prfp"] = list(self.price_fingerprints)
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> DayIndexRow | None:
        """Rebuild a row, or return ``None`` when the entry is unusable."""
        if not isinstance(raw, dict):
            return None
        count = raw.get("n")
        fingerprints = raw.get("fp")
        summary = raw.get("sum")
        finalized = raw.get("fin")
        return cls(
            interval_count=(
                count
                if isinstance(count, int) and not isinstance(count, bool)
                else None
            ),
            fingerprints=(
                [str(value) for value in fingerprints]
                if isinstance(fingerprints, list)
                else []
            ),
            finalized_at=finalized if isinstance(finalized, str) else None,
            summary=summary if isinstance(summary, dict) else None,
            raw_pruned=bool(raw.get("rp")),
            pv_fingerprints=(
                [str(value) for value in raw.get("pvfp")]
                if isinstance(raw.get("pvfp"), list)
                else []
            ),
            price_fingerprints=(
                [str(value) for value in raw.get("prfp")]
                if isinstance(raw.get("prfp"), list)
                else []
            ),
        )


@dataclass(slots=True)
class _Partition:
    """One loaded month of raw evidence."""

    key: str
    store: _ForecastStoreBackend
    snapshots: dict[date, list[ForecastSnapshot]] = field(default_factory=dict)
    outcomes: dict[date, DayOutcome] = field(default_factory=dict)
    #: Photovoltaic evidence, namespaced beside the load evidence rather than in
    #: a store of its own.
    #:
    #: A third partitioned store would have duplicated seven hundred lines of
    #: partitioning, atomic writes, write ordering, corruption suspension,
    #: never-write-after-failed-read and the month sweep -- and Phase 3 rejected
    #: exactly that reasoning for plan storage. The counter-argument is different
    #: failure blast radii, which is weaker here: load and PV evidence for the
    #: same day are analysed together, and the load snapshots already share a
    #: partition with their own outcomes.
    pv_snapshots: dict[date, list[PvSnapshot]] = field(default_factory=dict)
    pv_outcomes: dict[date, PvOutcome] = field(default_factory=dict)
    #: Price evidence, namespaced beside the other two for the same reason -- and
    #: with no outcome counterpart, deliberately. A price has no "what actually
    #: happened" to be scored against: it was the price. The record exists so a
    #: later phase can know *what was visible when a plan was made*, which is the
    #: one thing that cannot be recovered afterwards.
    price_snapshots: dict[date, list[PriceSnapshot]] = field(default_factory=dict)
    #: Set when this partition could not be read. Writes to it are suspended for
    #: the session; the rest of the history keeps working.
    corrupt: bool = False
    dirty: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the compact serialisable form."""
        days: dict[str, Any] = {}
        known = (
            set(self.snapshots)
            | set(self.outcomes)
            | set(self.pv_snapshots)
            | set(self.pv_outcomes)
            | set(self.price_snapshots)
        )
        for day in sorted(known):
            entry: dict[str, Any] = {}
            snapshots = self.snapshots.get(day)
            if snapshots:
                entry["s"] = [snapshot.to_dict() for snapshot in snapshots]
            outcome = self.outcomes.get(day)
            if outcome is not None:
                entry["o"] = outcome.to_dict()
            # Omitted entirely on an installation without PV, exactly as the
            # flexible-load arrays are in the learning store.
            pv_snapshots = self.pv_snapshots.get(day)
            if pv_snapshots:
                entry["pvs"] = [snapshot.to_dict() for snapshot in pv_snapshots]
            pv_outcome = self.pv_outcomes.get(day)
            if pv_outcome is not None:
                entry["pvo"] = pv_outcome.to_dict()
            # Omitted entirely on an installation with no price source, exactly
            # as the photovoltaic arrays are without a forecast.
            price_snapshots = self.price_snapshots.get(day)
            if price_snapshots:
                entry["prs"] = [snapshot.to_dict() for snapshot in price_snapshots]
            if entry:
                days[day.isoformat()] = entry
        return {"days": days}


class ForecastHistoryStore:
    """Loads, prunes and persists one config entry's forecast evidence."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise a per-entry forecast-history store."""
        self._hass = hass
        self._entry_id = entry_id
        self._index: _ForecastStoreBackend = self._make_store(
            FORECAST_INDEX_KEY_TEMPLATE.format(entry_id=entry_id)
        )
        self._partitions: dict[str, _Partition] = {}
        #: Month keys known to exist on disk, whether or not they are loaded.
        self.months: set[str] = set()
        self.days: dict[date, DayIndexRow] = {}
        #: Set when the index could not be read. Everything is suspended: with
        #: no index there is no way to tell an empty history from an unreadable
        #: one, and writing either would be a guess.
        self.corrupt = False
        self.reset_by_migration = False
        self._index_dirty = False
        #: Counted rather than silently swallowed, so a cap that bites is
        #: visible in diagnostics instead of reading as full coverage.
        self.snapshot_cap_hits = 0
        self.pruned_days = 0

    def _make_store(self, key: str) -> _ForecastStoreBackend:
        """Return a backend for one document."""
        return _ForecastStoreBackend(
            self._hass,
            FORECAST_STORAGE_VERSION,
            key,
            minor_version=FORECAST_STORAGE_MINOR_VERSION,
            # Forecast evidence is immutable once written, so a torn write
            # during a power cut would corrupt records that can never be
            # regenerated. The learning store can rebuild a lost quarter from
            # the next day's measurements; this cannot rebuild a prediction.
            atomic_writes=True,
        )

    # -- lifecycle -------------------------------------------------------

    async def async_load(self) -> None:
        """Load the index document.

        A failure here degrades to an empty view *and* suspends every write, so
        a transient read error can never be promoted into permanent loss.
        """
        self._index.discarded_legacy_document = False
        try:
            raw = await self._index.async_load()
        except Exception:
            _LOGGER.warning(
                "Forecast history could not be read. Forecast-error evidence is "
                "unavailable for this session and nothing will be written to "
                "disk until the problem is resolved and Home Assistant is "
                "restarted: the existing documents are left untouched in case "
                "they are still intact. Load learning and forecasting are "
                "otherwise unaffected"
            )
            self.corrupt = True
            return
        self.reset_by_migration = self._index.discarded_legacy_document

        if not isinstance(raw, dict):
            return

        months = raw.get("months")
        if isinstance(months, list):
            self.months = {str(value) for value in months}

        days_raw = raw.get("days")
        if isinstance(days_raw, dict):
            for key, value in days_raw.items():
                day = _parse_day(key)
                if day is None:
                    continue
                row = DayIndexRow.from_dict(value)
                if row is not None:
                    self.days[day] = row

    async def async_partition(self, key: str) -> _Partition:
        """Return the loaded partition for a month key, loading it on demand."""
        partition = self._partitions.get(key)
        if partition is not None:
            return partition

        store = self._make_store(
            FORECAST_MONTH_KEY_TEMPLATE.format(entry_id=self._entry_id, month=key)
        )
        partition = _Partition(key=key, store=store)
        self._partitions[key] = partition

        try:
            raw = await store.async_load()
        except Exception:
            _LOGGER.warning(
                "Forecast-history partition %s could not be read. Its records "
                "are excluded from forecast-error statistics for this session "
                "and it will not be written to; every other month keeps "
                "working",
                key,
            )
            partition.corrupt = True
            return partition

        if not isinstance(raw, dict):
            return partition
        days_raw = raw.get("days")
        if not isinstance(days_raw, dict):
            return partition

        for day_key, value in days_raw.items():
            day = _parse_day(day_key)
            if day is None or not isinstance(value, dict):
                continue
            snapshots_raw = value.get("s")
            if isinstance(snapshots_raw, list):
                rebuilt = [
                    snapshot
                    for snapshot in (
                        ForecastSnapshot.from_dict(day, entry)
                        for entry in snapshots_raw
                    )
                    if snapshot is not None
                ]
                if rebuilt:
                    partition.snapshots[day] = rebuilt
            outcome = DayOutcome.from_dict(day, value.get("o"))
            if outcome is not None:
                partition.outcomes[day] = outcome
            pv_raw = value.get("pvs")
            if isinstance(pv_raw, list):
                rebuilt_pv = [
                    snapshot
                    for snapshot in (
                        PvSnapshot.from_dict(day, entry) for entry in pv_raw
                    )
                    if snapshot is not None
                ]
                if rebuilt_pv:
                    partition.pv_snapshots[day] = rebuilt_pv
            pv_outcome = PvOutcome.from_dict(day, value.get("pvo"))
            if pv_outcome is not None:
                partition.pv_outcomes[day] = pv_outcome
            price_raw = value.get("prs")
            if isinstance(price_raw, list):
                rebuilt_price = [
                    snapshot
                    for snapshot in (
                        PriceSnapshot.from_dict(day, entry) for entry in price_raw
                    )
                    if snapshot is not None
                ]
                if rebuilt_price:
                    partition.price_snapshots[day] = rebuilt_price
        return partition

    async def async_ensure_days(self, days: list[date]) -> None:
        """Load every partition needed to read or write the given target days."""
        for key in sorted({month_key(day) for day in days}):
            await self.async_partition(key)

    def writable(self, day: date) -> bool:
        """Return whether evidence for a target day may be written right now."""
        if self.corrupt:
            return False
        partition = self._partitions.get(month_key(day))
        return partition is not None and not partition.corrupt

    # -- reading ---------------------------------------------------------

    def snapshots(self, day: date) -> list[ForecastSnapshot]:
        """Return the retained snapshots for a target day, oldest first."""
        partition = self._partitions.get(month_key(day))
        if partition is None:
            return []
        return list(partition.snapshots.get(day, ()))

    def outcome(self, day: date) -> DayOutcome | None:
        """Return the finalised outcome for a target day, if there is one."""
        partition = self._partitions.get(month_key(day))
        if partition is None:
            return None
        return partition.outcomes.get(day)

    def row(self, day: date) -> DayIndexRow | None:
        """Return the index row for a target day."""
        return self.days.get(day)

    def is_finalized(self, day: date) -> bool:
        """Return whether a target day has already been matched against reality."""
        row = self.days.get(day)
        return row is not None and row.finalized_at is not None

    def has_fingerprint(self, day: date, fingerprint: str) -> bool:
        """Return whether this exact forecast has already been recorded.

        Answered from the index, so the common case -- a refresh reproducing the
        forecast it produced fifteen minutes ago -- costs no disk access at all.
        """
        row = self.days.get(day)
        return row is not None and fingerprint in row.fingerprints

    def unfinalized_days(self, before: date) -> list[date]:
        """Return target days that have evidence but were never matched.

        Ordered oldest first, so a catch-up after a long outage resolves days in
        the order they happened.
        """
        return sorted(
            day
            for day, row in self.days.items()
            if day < before and row.finalized_at is None and row.fingerprints
        )

    # -- writing ---------------------------------------------------------

    def price_snapshots(self, day: date) -> list[PriceSnapshot]:
        """Return the price issuances recorded for a target day."""
        partition = self._partitions.get(month_key(day))
        if partition is None:
            return []
        return list(partition.price_snapshots.get(day, ()))

    def latest_price_snapshot(self, day: date) -> PriceSnapshot | None:
        """Return the most recent price issuance for a target day, or ``None``."""
        recorded = self.price_snapshots(day)
        return recorded[-1] if recorded else None

    def has_price_fingerprint(self, day: date, fingerprint: str) -> bool:
        """Return whether a price issuance with this content is already recorded.

        Answered from the index, so the ninety-odd refreshes a day that change
        nothing never load a month of arrays to find that out.
        """
        row = self.days.get(day)
        return row is not None and fingerprint in row.price_fingerprints

    def add_price_snapshot(self, snapshot: PriceSnapshot) -> bool:
        """Persist a price issuance, unless it duplicates one already recorded.

        Change-triggered by content fingerprint, like both series before it. The
        source republishes a handful of times a day, so this bounds growth by how
        often the data actually changes rather than by a cap that would need
        tuning -- and the per-day ceiling is still there as a backstop.
        """
        day = snapshot.target_day
        if not self.writable(day):
            return False
        if self.has_price_fingerprint(day, snapshot.fingerprint):
            return False

        row = self.days.setdefault(day, DayIndexRow())
        if len(row.price_fingerprints) >= FORECAST_MAX_SNAPSHOTS_PER_TARGET:
            self.snapshot_cap_hits += 1
            _LOGGER.warning(
                "Forecast history already holds %d price snapshots for %s, "
                "which is the per-day ceiling, so this issuance is not being "
                "recorded. The records already kept are unaffected",
                len(row.price_fingerprints),
                day.isoformat(),
            )
            return False

        partition = self._partitions[month_key(day)]
        partition.price_snapshots.setdefault(day, []).append(snapshot)
        partition.dirty = True

        row.price_fingerprints.append(snapshot.fingerprint)
        self.months.add(month_key(day))
        self._index_dirty = True
        return True

    def pv_snapshots(self, day: date) -> list[PvSnapshot]:
        """Return the PV issuances recorded for a target day."""
        partition = self._partitions.get(month_key(day))
        if partition is None:
            return []
        return list(partition.pv_snapshots.get(day, ()))

    def latest_pv_snapshot(self, day: date) -> PvSnapshot | None:
        """Return the most recent PV issuance for a target day, or ``None``.

        The most recent is what the headline comparison uses. Every earlier one is
        retained, because a forecast issued at breakfast and one issued at noon
        are different claims about the same day and a later phase will want both.
        """
        snapshots = self.pv_snapshots(day)
        if not snapshots:
            return None
        return max(snapshots, key=lambda snapshot: snapshot.issued_at)

    def pv_outcome(self, day: date) -> PvOutcome | None:
        """Return the finalised PV comparison for a target day, or ``None``."""
        partition = self._partitions.get(month_key(day))
        if partition is None:
            return None
        return partition.pv_outcomes.get(day)

    def has_pv_fingerprint(self, day: date, fingerprint: str) -> bool:
        """Return whether this PV forecast has already been recorded.

        Read from the index rather than the partition, so the ninety-odd refreshes
        a day that change nothing do not have to load a month of arrays to find
        that out.
        """
        row = self.days.get(day)
        return row is not None and fingerprint in row.pv_fingerprints

    def add_pv_snapshot(self, snapshot: PvSnapshot) -> bool:
        """Persist a PV issuance, unless it duplicates one already recorded.

        Change-triggered by content fingerprint, exactly as the load snapshots
        are. The source updates a handful of times a day at best, so this bounds
        growth naturally rather than by a cap that would have to be tuned.
        """
        day = snapshot.target_day
        if not self.writable(day):
            return False
        if self.has_pv_fingerprint(day, snapshot.fingerprint):
            return False

        row = self.days.setdefault(day, DayIndexRow())
        if len(row.pv_fingerprints) >= FORECAST_MAX_SNAPSHOTS_PER_TARGET:
            self.snapshot_cap_hits += 1
            _LOGGER.warning(
                "Forecast history already holds %d photovoltaic snapshots for "
                "%s, which is the per-day ceiling, so this issuance is not being "
                "recorded. The records already kept are unaffected",
                len(row.pv_fingerprints),
                day.isoformat(),
            )
            return False

        partition = self._partitions[month_key(day)]
        partition.pv_snapshots.setdefault(day, []).append(snapshot)
        partition.dirty = True

        row.pv_fingerprints.append(snapshot.fingerprint)
        row.interval_count = snapshot.interval_count
        self.months.add(month_key(day))
        self._index_dirty = True
        return True

    def set_pv_outcome(self, outcome: PvOutcome) -> bool:
        """Persist a finalised PV comparison.

        Idempotent, like its load-side counterpart: finalisation is a pure
        recomputation from the stored snapshot and the learning history, so
        writing it twice produces the same document.
        """
        day = outcome.target_day
        if not self.writable(day):
            return False

        partition = self._partitions[month_key(day)]
        partition.pv_outcomes[day] = outcome
        partition.dirty = True
        self.days.setdefault(day, DayIndexRow())
        self.months.add(month_key(day))
        self._index_dirty = True
        return True

    def add_snapshot(self, snapshot: ForecastSnapshot) -> bool:
        """Persist an issuance, unless it duplicates the last one for its target.

        Returns whether the snapshot was actually kept. A duplicate is not an
        error: it is the expected outcome of ninety-four refreshes out of every
        ninety-six.
        """
        day = snapshot.target_day
        if not self.writable(day):
            return False
        if self.has_fingerprint(day, snapshot.fingerprint):
            return False

        row = self.days.setdefault(day, DayIndexRow())
        if row.snapshot_count >= FORECAST_MAX_SNAPSHOTS_PER_TARGET:
            self.snapshot_cap_hits += 1
            _LOGGER.warning(
                "Forecast history already holds %d snapshots for %s, which is "
                "the per-day ceiling, so this issuance is not being recorded. "
                "The model is changing far more often than the design expects; "
                "the records already kept are unaffected",
                row.snapshot_count,
                day.isoformat(),
            )
            return False

        partition = self._partitions[month_key(day)]
        partition.snapshots.setdefault(day, []).append(snapshot)
        partition.dirty = True

        row.fingerprints.append(snapshot.fingerprint)
        row.interval_count = snapshot.interval_count
        self.months.add(month_key(day))
        self._index_dirty = True
        return True

    def set_outcome(self, outcome: DayOutcome, summary: dict[str, Any] | None) -> bool:
        """Persist a finalised day and its reduced summary facts.

        Idempotent by construction: finalisation is a pure recomputation from
        the learning history, so writing it twice produces the same document.
        """
        day = outcome.target_day
        if not self.writable(day):
            return False

        partition = self._partitions[month_key(day)]
        partition.outcomes[day] = outcome
        partition.dirty = True

        row = self.days.setdefault(day, DayIndexRow())
        row.finalized_at = outcome.finalized_at.isoformat()
        row.summary = summary
        self.months.add(month_key(day))
        self._index_dirty = True
        return True

    # -- retention -------------------------------------------------------

    def _prune_targets(self, reference: date) -> tuple[list[date], list[date]]:
        """Return the days to expire entirely and the days to reduce to summary.

        Split out from the work so the partitions holding those days can be
        loaded first. Dropping a day from the index while its arrays stayed
        behind in an unloaded partition would leak exactly the bytes the
        retention window exists to reclaim.
        """
        if self.days:
            horizon = max(self.days) + timedelta(days=1)
            if reference > horizon:
                reference = horizon

        raw_cutoff = reference - timedelta(days=FORECAST_RAW_RETENTION_DAYS - 1)
        summary_cutoff = reference - timedelta(days=FORECAST_SUMMARY_RETENTION_DAYS - 1)

        expire: list[date] = []
        reduce: list[date] = []
        for day, row in self.days.items():
            if day < summary_cutoff:
                expire.append(day)
            elif (
                day < raw_cutoff
                and not row.raw_pruned
                # An unfinalised day is work in progress, not stale data.
                # Dropping its prediction would leave a record that can never be
                # answered, so it waits for the finalisation pass instead.
                and row.finalized_at is not None
            ):
                reduce.append(day)
        return sorted(expire), sorted(reduce)

    async def async_prune(self, reference: date) -> int:
        """Drop evidence that has aged out. Returns the number of days affected.

        ``reference`` is clamped to at most one day past the newest target day
        already recorded, exactly as the learning store clamps its own. A host
        without a real-time clock hands Home Assistant a date years ahead until
        NTP corrects it, and an unclamped reference would take the whole
        retention window with it.

        Raw per-interval arrays and the reduced summary rows expire on different
        horizons. Losing the arrays is acceptable once the learning history that
        produced them has itself been pruned -- past that point the raw evidence
        could no longer explain *why* a forecast was wrong, only that it was --
        while the summary rows are small enough to keep for years.
        """
        if self.corrupt:
            return 0

        expire, reduce = self._prune_targets(reference)
        if not expire and not reduce:
            return 0

        await self.async_ensure_days(expire + reduce)

        for day in expire:
            self.days.pop(day, None)
            self._drop_raw(day)
        for day in reduce:
            row = self.days.get(day)
            if row is None:
                continue
            self._drop_raw(day)
            row.raw_pruned = True
            row.fingerprints = []
            row.interval_count = None

        affected = len(expire) + len(reduce)
        self._index_dirty = True
        self.pruned_days += affected
        return affected

    def _drop_raw(self, day: date) -> None:
        """Remove one day's per-interval arrays from its partition, if loaded."""
        partition = self._partitions.get(month_key(day))
        if partition is None or partition.corrupt:
            return
        removed = partition.snapshots.pop(day, None)
        removed_outcome = partition.outcomes.pop(day, None)
        if removed is not None or removed_outcome is not None:
            partition.dirty = True

    def empty_month_keys(self) -> list[str]:
        """Return loaded partitions that no longer hold anything."""
        return sorted(
            key
            for key, partition in self._partitions.items()
            if not partition.corrupt
            and not partition.snapshots
            and not partition.outcomes
        )

    async def async_drop_empty_months(self) -> None:
        """Delete partitions emptied by pruning, so files do not accumulate."""
        if self.corrupt:
            return
        for key in self.empty_month_keys():
            partition = self._partitions.pop(key, None)
            self.months.discard(key)
            self._index_dirty = True
            if partition is not None:
                await partition.store.async_remove()

    # -- persistence -----------------------------------------------------

    def _index_document(self) -> dict[str, Any]:
        """Return the full serialisable index."""
        return {
            "months": sorted(self.months),
            "days": {
                day.isoformat(): payload
                for day, payload in (
                    (day, row.to_dict()) for day, row in sorted(self.days.items())
                )
                if payload
            },
        }

    def schedule_save(self) -> None:
        """Queue debounced writes for whatever changed.

        Partitions are queued before the index, for the reason given in
        :meth:`async_save_now`.
        """
        if self.corrupt:
            return
        for partition in self._partitions.values():
            if partition.corrupt or not partition.dirty:
                continue
            partition.store.async_delay_save(
                partition.to_dict, FORECAST_STORE_SAVE_DELAY
            )
            partition.dirty = False
        if self._index_dirty:
            self._index.async_delay_save(
                self._index_document, FORECAST_STORE_SAVE_DELAY
            )
            self._index_dirty = False

    async def async_save_now(self) -> None:
        """Write immediately. Used on unload and on Home Assistant stop.

        Refused after a failed read, for the same reason the learning store
        refuses: the in-memory view is a fallback, not the truth, and flushing
        it would destroy a document that may be perfectly intact.

        **Partitions are written before the index, and the order matters.** The
        index is what dedup consults: once a fingerprint is recorded there, that
        forecast is never issued again. So a crash between the two writes is
        only safe in one direction. Index last means the worst case is an
        already-written array the index has not claimed yet, which the next
        refresh simply writes again. Index first would mean a fingerprint
        claiming an array that was never written, and dedup would then refuse to
        ever produce it -- a prediction lost permanently to a power cut.
        """
        if self.corrupt:
            return
        for partition in self._partitions.values():
            if partition.corrupt:
                continue
            await partition.store.async_save(partition.to_dict())
            partition.dirty = False
        await self._index.async_save(self._index_document())
        self._index_dirty = False

    async def async_remove(self) -> None:
        """Delete every document belonging to this entry.

        Every partition is removed, not only the loaded ones. Without the month
        list from the index, a removed entry would orphan up to a year of files
        in ``.storage`` that nothing could ever reach again.
        """
        keys = set(self.months) | set(self._partitions)
        for key in sorted(keys):
            partition = self._partitions.get(key)
            store = (
                partition.store
                if partition is not None
                else self._make_store(
                    FORECAST_MONTH_KEY_TEMPLATE.format(
                        entry_id=self._entry_id, month=key
                    )
                )
            )
            await store.async_remove()
        self._partitions.clear()
        self.months.clear()
        self.days.clear()
        await self._index.async_remove()

    # -- diagnostics -----------------------------------------------------

    @property
    def snapshot_total(self) -> int:
        """Return how many immutable snapshots are retained across all days."""
        return sum(row.snapshot_count for row in self.days.values())

    @property
    def span(self) -> tuple[date | None, date | None]:
        """Return the ``(oldest, newest)`` target days on record."""
        if not self.days:
            return None, None
        ordered = sorted(self.days)
        return ordered[0], ordered[-1]

    def partition_report(self) -> list[dict[str, Any]]:
        """Return the load and health state of each known partition."""
        report: list[dict[str, Any]] = []
        for key in sorted(self.months | set(self._partitions)):
            partition = self._partitions.get(key)
            report.append(
                {
                    "month": key,
                    "loaded": partition is not None,
                    "corrupt": bool(partition is not None and partition.corrupt),
                    "days": (
                        None
                        if partition is None
                        else len(set(partition.snapshots) | set(partition.outcomes))
                    ),
                }
            )
        return report
