"""Orchestration for the Phase-2 forecast evidence layer.

One entry point, :meth:`ForecastRecorder.async_record`, called once per
coordinator refresh after the forecasts have been built. It prunes what has aged
out, issues snapshots that are actually new, matches finished days against what
the house really did, re-derives any match left behind by a superseded rule, and
returns the small set of figures the two Phase-2 sensors publish.

Pruning goes first, and that ordering is load-bearing rather than tidy: see the
comment in :meth:`async_record`.

It never raises into the refresh. A forecast-history failure must degrade the
evidence layer, not take the four Phase-1 sensors down with it: learning and
forecasting do not depend on any of this.

The suspension rule
-------------------

Finalisation is refused while the *learning* store is in its failed-read state.
That store degrades an unreadable document to an empty history so setup can
continue, which is right for availability -- but an empty history means
``baseline_at`` returns ``None`` for every interval of every day. Finalising
against it would write immutable records stating that every actual was missing,
for days whose measurements are very probably sitting intact on disk. That is
the beta.4 write-after-failed-read bug one layer up, and with worse consequences:
the learning document would at least still be there, while these records are
final by design.

Nothing is lost by waiting. Matching is a pure recomputation from persisted
data, so the days simply stay unfinalised and resolve on the next refresh after
a restart that reads the history successfully.

Restatement -- re-deriving an already-written match after a matching *rule* is
corrected -- obeys the same suspension for the same reason, and is bounded
further still. :meth:`ForecastRecorder._async_restate` states each bound.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .const import (
    FORECAST_ERROR_WINDOW_DAYS,
    FORECAST_MATCHER_VERSION,
    FORECAST_MIN_INTERVALS_FOR_METRIC,
)
from .forecast import DayForecast
from .forecast_history import (
    DayOutcome,
    ForecastSnapshot,
    build_outcome,
    build_snapshot,
)
from .history_store import ForecastHistoryStore
from .metrics import (
    ScoredDay,
    WindowSummary,
    best_snapshot,
    day_error_from_summary,
    matcher_version,
    score_day,
    summary_row,
    window_from_summaries,
)
from .storage import LearningStore, expected_quarters_for

_LOGGER = logging.getLogger(__name__)

#: Most days matched, or re-matched, in one refresh. The two share the budget:
#: an update that both corrects a rule and comes back from a long outage must
#: still not put a hundred partition loads on the event loop at once.
#:
#: A Home Assistant that has been off for months comes back with a long backlog,
#: and each day loads a partition and rebuilds an outcome. Bounding the batch
#: keeps that off the event loop in one go; the remainder resolves on the next
#: refresh, fifteen minutes later, which is soon enough for history that is
#: already weeks old. The same lesson as ``MAX_CATCHUP_SECONDS``.
_MAX_FINALIZATIONS_PER_REFRESH = 8


@dataclass(slots=True)
class RecorderResult:
    """What one recording pass produced, for the sensors and diagnostics."""

    #: Snapshots actually persisted this pass. Normally zero.
    issued: tuple[ForecastSnapshot, ...] = ()
    #: Target days matched against reality this pass. Normally zero or one.
    finalized: tuple[date, ...] = ()
    #: Days whose existing match was re-derived under corrected matching rules.
    #: Non-empty only for the few refreshes after an update that changes
    #: ``FORECAST_MATCHER_VERSION``.
    restated: tuple[date, ...] = ()
    #: Day-level error facts for the previous civil day, or ``None``.
    yesterday: dict[str, Any] | None = None
    #: Rolling statistics over the published window.
    window: WindowSummary = field(default_factory=WindowSummary)
    #: True while finalisation is being held back by the learning store.
    finalization_suspended: bool = False
    #: Days that are in the past, carry a prediction, and are still unmatched.
    unresolved_days: int = 0


class ForecastRecorder:
    """Turns each refresh into forecast evidence, and reads the evidence back."""

    def __init__(
        self, store: ForecastHistoryStore, learning_store: LearningStore
    ) -> None:
        """Bind the recorder to the two stores it reconciles."""
        self.store = store
        self.learning = learning_store
        #: The civil day the last pass ran under. Pruning and the finalisation
        #: sweep are day-scoped, so this is what makes them run once per day
        #: rather than ninety-six times.
        self._last_day: date | None = None
        #: Counted for diagnostics: a pass that found the forecast unchanged.
        self.duplicate_issuances = 0

    async def async_record(
        self,
        *,
        now: datetime,
        today: date,
        tz_key: str,
        tz: Any,
        baseline_today: DayForecast,
        tomorrow: DayForecast,
        learned_days: int,
        confidence_percent: float | None,
        confidence: dict[str, Any] | None,
        ev_power_entity: str | None,
    ) -> RecorderResult:
        """Record one refresh and return the published figures."""
        if self.store.corrupt:
            return RecorderResult(finalization_suspended=self.learning.corrupt)

        day_changed = self._last_day != today
        self._last_day = today

        # Pruned *before* this pass issues anything, and the order is the whole
        # point. ``async_prune`` clamps its reference to one day past the newest
        # target already recorded, so a host whose clock is years ahead cannot
        # define "now" and take the retention window with it. Issuing first put
        # the bogus future target *inside* the set the clamp measures against,
        # which made the clamp inert on the only path that reaches it: one
        # refresh under a five-year clock excursion dropped every retained
        # prediction array in the history. Exactly the beta.4 ``get_or_create``
        # defect, one store along.
        if day_changed:
            await self.store.async_prune(today)
            await self.store.async_drop_empty_months()

        issued = await self._async_issue(
            now=now,
            today=today,
            tz_key=tz_key,
            forecasts=(baseline_today, tomorrow),
            learned_days=learned_days,
            confidence_percent=confidence_percent,
            confidence=confidence,
            ev_power_entity=ev_power_entity,
        )

        suspended = self.learning.corrupt
        finalized: tuple[date, ...] = ()
        restated: tuple[date, ...] = ()
        if not suspended:
            finalized = await self._async_finalize(now=now, today=today, tz=tz)
            restated = await self._async_restate(
                now=now, today=today, tz=tz, budget=len(finalized)
            )

        self.store.schedule_save()

        return RecorderResult(
            issued=issued,
            finalized=finalized,
            restated=restated,
            yesterday=self.yesterday_error(today),
            window=self.window_summary(today),
            finalization_suspended=suspended,
            unresolved_days=len(self.store.unfinalized_days(before=today)),
        )

    # -- issuance --------------------------------------------------------

    async def _async_issue(
        self,
        *,
        now: datetime,
        today: date,
        tz_key: str,
        forecasts: tuple[DayForecast, ...],
        learned_days: int,
        confidence_percent: float | None,
        confidence: dict[str, Any] | None,
        ev_power_entity: str | None,
    ) -> tuple[ForecastSnapshot, ...]:
        """Persist any forecast whose content differs from the last one kept."""
        candidates = [
            build_snapshot(
                forecast,
                issued_at=now.astimezone(UTC),
                issuance_day=today,
                tz_key=tz_key,
                learned_days=learned_days,
                confidence_percent=confidence_percent,
                confidence=confidence,
                ev_power_entity=ev_power_entity,
            )
            for forecast in forecasts
            # A forecast for a day already in the past is not a forecast. Only
            # reachable if the clock steps backwards between two refreshes.
            if forecast.day >= today
        ]

        fresh = [
            snapshot
            for snapshot in candidates
            if not self.store.has_fingerprint(snapshot.target_day, snapshot.fingerprint)
        ]
        self.duplicate_issuances += len(candidates) - len(fresh)
        if not fresh:
            # The overwhelmingly common case: the model produced exactly what it
            # produced fifteen minutes ago. Answered from the index, so this
            # path performs no disk access at all.
            return ()

        await self.store.async_ensure_days([snapshot.target_day for snapshot in fresh])
        return tuple(
            snapshot for snapshot in fresh if self.store.add_snapshot(snapshot)
        )

    # -- finalisation ----------------------------------------------------

    async def _async_finalize(
        self, *, now: datetime, today: date, tz: Any
    ) -> tuple[date, ...]:
        """Match finished target days against what the house actually did."""
        pending = self.store.unfinalized_days(before=today)
        if not pending:
            return ()

        batch = pending[:_MAX_FINALIZATIONS_PER_REFRESH]
        if len(pending) > len(batch):
            _LOGGER.debug(
                "Forecast history has %d unmatched days; resolving %d now and "
                "the rest on following refreshes",
                len(pending),
                len(batch),
            )

        await self.store.async_ensure_days(batch)
        resolved: list[date] = []
        for day in batch:
            if self._finalize_day(day, now=now, tz=tz):
                resolved.append(day)
        return tuple(resolved)

    async def _async_restate(
        self, *, now: datetime, today: date, tz: Any, budget: int
    ) -> tuple[date, ...]:
        """Re-derive matches written under a superseded set of matching rules.

        A matching rule that turns out to be wrong is not only wrong going
        forward. The days already matched under it carry its verdict, and
        nothing would ever revisit them: finalisation only looks at days that
        were never matched at all. On a dataset that accrues one irreplaceable
        day at a time, that leaves a permanent scar for every rule ever
        corrected -- and the first correction, in v1.0.0-beta.6, excluded whole
        days for having a single missing quarter.

        So this is safe to do, and only because of how narrowly it is bounded.
        Every input is still on disk, and re-deriving is the same pure function
        over the same two arguments:

        * the **snapshots are never touched**. They are the evidence; only the
          *match* -- a derived reading of them -- is restated.
        * the day must still have retained snapshots. Past the raw-retention
          horizon the arrays are gone and only the summary remains, and a
          re-derivation there would replace a real comparison with an empty one.
        * the day must still have a **learning record**. Once that has been
          pruned, ``build_outcome`` would honestly return "no record" -- and
          writing that over a sound match would destroy the very evidence this
          exists to preserve. So a day whose record has aged out keeps the
          verdict it already has.
        * it runs under the same suspension and the same per-refresh budget as
          finalisation, so an update never lands as one long blocking sweep.
        """
        remaining = _MAX_FINALIZATIONS_PER_REFRESH - budget
        if remaining <= 0:
            return ()

        stale = self._restatable_days(today)
        if not stale:
            return ()

        batch = stale[:remaining]
        _LOGGER.debug(
            "Re-deriving %d of %d forecast matches under matching rules v%d",
            len(batch),
            len(stale),
            FORECAST_MATCHER_VERSION,
        )
        await self.store.async_ensure_days(batch)
        restated: list[date] = []
        for day in batch:
            # Checked only now the partition is loaded: a day whose snapshots
            # turn out to be absent -- an unreadable month, a torn write -- must
            # keep the match it has rather than be re-derived against nothing.
            if not self.store.snapshots(day):
                continue
            if self._finalize_day(day, now=now, tz=tz):
                restated.append(day)
        return tuple(restated)

    def _restatable_days(self, today: date) -> list[date]:
        """Return matched days whose verdict predates the current rules.

        Answered from the always-loaded index and the learning history already
        in memory, so it costs no disk access -- and once every row carries the
        current version it finds nothing and costs nothing at all.
        """
        return sorted(
            day
            for day, row in self.store.days.items()
            if day < today
            and row.finalized_at is not None
            and not row.raw_pruned
            and matcher_version(row.summary) < FORECAST_MATCHER_VERSION
            and day in self.learning.days
        )

    def _finalize_day(self, day: date, *, now: datetime, tz: Any) -> bool:
        """Build and persist one day's outcome. Returns whether it was written."""
        if not self.store.writable(day):
            return False

        snapshots = self.store.snapshots(day)
        record = self.learning.days.get(day)
        outcome = build_outcome(
            day,
            record,
            snapshots,
            finalized_at=now.astimezone(UTC),
            fallback_tz_key=str(tz),
            fallback_interval_count=expected_quarters_for(day, tz),
        )
        scored = self._score(snapshots, outcome)
        summary = summary_row(
            scored,
            interval_count=outcome.interval_count,
            flags=outcome.flags,
        )
        return self.store.set_outcome(outcome, summary)

    @staticmethod
    def _score(
        snapshots: list[ForecastSnapshot], outcome: DayOutcome
    ) -> ScoredDay | None:
        """Return the scored pairing for a day, or ``None`` when uncomparable."""
        snapshot = best_snapshot(snapshots)
        if snapshot is None:
            return None
        return score_day(snapshot, outcome)

    # -- reading back ----------------------------------------------------

    def yesterday_error(self, today: date) -> dict[str, Any] | None:
        """Return the previous civil day's error facts, or ``None``."""
        row = self.store.row(today - timedelta(days=1))
        if row is None or row.finalized_at is None:
            return None
        return day_error_from_summary(row.summary)

    def window_summary(
        self, today: date, window_days: int = FORECAST_ERROR_WINDOW_DAYS
    ) -> WindowSummary:
        """Return rolling statistics over the trailing window.

        Built from the index rows alone, so no partition is loaded and the
        published sensors cost no disk access. The window ends at yesterday: the
        day in progress has not finished, and scoring a partial day against a
        whole-day prediction is the one comparison this design refuses to make.
        """
        start = today - timedelta(days=window_days)
        rows = [
            row.summary
            for day, row in self.store.days.items()
            if start <= day < today and row.summary is not None
        ]
        summary = window_from_summaries(row for row in rows if row is not None)
        if summary.intervals_compared < FORECAST_MIN_INTERVALS_FOR_METRIC:
            # Below roughly two full days the *derived* figures are whichever
            # handful of intervals happened to resolve, and publishing them
            # would invite a fresh installation's noise to be read as forecast
            # quality. The sample size and the two energy totals are facts about
            # the window rather than judgements of the model, so they are
            # reported: dropping them let the sensor publish
            # ``predicted_kwh: 0.0`` and ``actual_kwh: 0.0`` beside an
            # ``intervals_compared`` of ninety-six, which is a claim that the
            # house consumed nothing.
            return WindowSummary(
                days_compared=summary.days_compared,
                intervals_compared=summary.intervals_compared,
                predicted_kwh=summary.predicted_kwh,
                actual_kwh=summary.actual_kwh,
            )
        return summary

    def scored_days(self, start: date, end: date) -> list[ScoredDay]:
        """Return fully scored days in ``[start, end)`` from loaded partitions.

        Used only by diagnostics, which loads the partitions it needs first.
        Days whose partition is absent are skipped rather than counted as
        missing: this is a report, not a verdict.
        """
        scored: list[ScoredDay] = []
        for day in sorted(self.store.days):
            if not start <= day < end:
                continue
            outcome = self.store.outcome(day)
            if outcome is None:
                continue
            result = self._score(self.store.snapshots(day), outcome)
            if result is not None:
                scored.append(result)
        return scored

    def scored_days_by_horizon(
        self, start: date, end: date
    ) -> dict[int, list[ScoredDay]]:
        """Return scored days grouped by the horizon of the prediction used.

        Every retained snapshot is scored, not just the headline one, which is
        what makes "is a day-ahead forecast measurably worse than a day-of one"
        answerable at all.
        """
        grouped: dict[int, list[ScoredDay]] = {}
        for day in sorted(self.store.days):
            if not start <= day < end:
                continue
            outcome = self.store.outcome(day)
            if outcome is None:
                continue
            for snapshot in self.store.snapshots(day):
                result = score_day(snapshot, outcome)
                if result is not None:
                    grouped.setdefault(snapshot.horizon_days, []).append(result)
        return grouped
