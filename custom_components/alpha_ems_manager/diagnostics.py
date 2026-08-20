"""Diagnostics for Alpha EMS Manager.

This is where everything that does *not* justify an entity goes: per-source
availability, normalised readings, sign conventions, coverage statistics, the
confidence derivation, the energy-balance residual, and the whole Phase-2
forecast-evidence layer beyond the two error figures that reached a sensor.

The payload carries no credentials, tokens or account data -- this integration
holds none, because it never talks to an external service. Nor does it dump the
full year of learned history; that would be megabytes of quarter buckets. Only
the summary a support conversation actually needs is included.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import AlphaEmsConfigEntry
from .const import (
    CONFIG_ENTRY_VERSION,
    FORECAST_MATCHER_VERSION,
    FORECAST_MAX_SNAPSHOTS_PER_TARGET,
    FORECAST_METRIC_WINDOWS,
    FORECAST_MIN_INTERVALS_FOR_METRIC,
    FORECAST_MODEL_VERSION,
    FORECAST_RAW_RETENTION_DAYS,
    FORECAST_STORAGE_VERSION,
    FORECAST_SUMMARY_RETENTION_DAYS,
    MAX_HISTORY_DAYS,
    MIN_DAY_COMPLETENESS,
    MIN_QUARTER_COVERAGE,
    PRICE_MAPPING_VERSION,
    SLOTS_PER_DAY,
    STORAGE_VERSION,
)
from .coordinator import AlphaEmsCoordinator
from .forecast import REASON_NOT_BUILT, DayForecast
from .forecast_history import (
    LIFECYCLE_PENDING,
    LIFECYCLE_UNMATCHED,
    LIFECYCLE_UNRESOLVED,
    LIFECYCLE_VALIDATED,
    baseline_definition,
    lifecycle_from_summary,
    model_params_hash,
)
from .frank_source import FrankCapability, read_current_prices
from .frank_source import discover as discover_frank
from .metrics import compute_window, window_from_summaries
from .plan import plan_as_dict
from .policy import DEFAULT_POLICY, SHIPPED_POLICIES
from .price_forecast import PriceForecast
from .pv_forecast import PvForecast, pv_error_metrics
from .solcast_source import SolcastFacts
from .solcast_source import discover as discover_solcast
from .storage import DayRecord, elapsed_quarters_for, expected_quarters_for


def _source_report(hass: HomeAssistant, entity_id: str | None) -> dict[str, Any]:
    """Summarise one configured source entity."""
    if not entity_id:
        return {"configured": False}
    state = hass.states.get(entity_id)
    if state is None:
        return {"configured": True, "entity_id": entity_id, "exists": False}
    return {
        "configured": True,
        "entity_id": entity_id,
        "exists": True,
        "state": state.state,
        "unit": state.attributes.get("unit_of_measurement"),
        "device_class": state.attributes.get("device_class"),
        "state_class": state.attributes.get("state_class"),
    }


def _fraction(valid: int, expected: int) -> float | None:
    """Return ``valid / expected`` rounded, or ``None`` when nothing is expected.

    ``None`` rather than ``0.0``: a day that has not started yet has no coverage
    to report, and reporting it as zero coverage says the opposite.
    """
    if expected <= 0:
        return None
    return round(min(1.0, valid / expected), 4)


def _coverage_report(records: list[DayRecord]) -> dict[str, Any]:
    """Summarise coverage over a set of *finalised* days.

    Every day here can no longer gain intervals, so the denominator is the full
    civil-day length and no elapsed-time reasoning is needed.
    """
    expected = sum(record.interval_count for record in records)
    measured = sum(record.measured_valid_count for record in records)
    baseline = sum(record.baseline_valid_count for record in records)
    return {
        "days": len(records),
        "real_intervals": expected,
        "measured_valid_intervals": measured,
        "baseline_valid_intervals": baseline,
        "measured_coverage": _fraction(measured, expected),
        "baseline_coverage": _fraction(baseline, expected),
    }


def _current_day_report(
    record: DayRecord | None, day: date, tz: Any, now: datetime
) -> dict[str, Any]:
    """Summarise the day in progress against the quarters that have closed."""
    expected = expected_quarters_for(day, tz)
    elapsed = elapsed_quarters_for(day, tz, now)
    measured = 0 if record is None else record.measured_valid_count
    baseline = 0 if record is None else record.baseline_valid_count
    return {
        "date": day.isoformat(),
        # 92 / 96 / 100 depending on this civil day's daylight-saving shape.
        "expected_intervals": expected,
        "elapsed_intervals": elapsed,
        "measured_valid_intervals": measured,
        "baseline_valid_intervals": baseline,
        "measured_coverage_so_far": _fraction(measured, elapsed),
        "baseline_coverage_so_far": _fraction(baseline, elapsed),
        "counts_toward_learned_days": False,
        "note": (
            "the running day is excluded from learned days, from the confidence "
            "score and from every forecast input until midnight finalises it"
        ),
    }


#: Factual states of the optional day-total cross-check. Deliberately *not* a
#: pass/fail verdict: the two figures are produced by different methods -- a
#: time-weighted integration of an instantaneous power sensor here, the
#: inverter's own accumulator there -- and no defensible tolerance separates
#: "agrees" from "disagrees" without inventing one. The difference is reported
#: and the reader judges it.
VALIDATION_NOT_CONFIGURED = "not_configured"
VALIDATION_SOURCE_UNAVAILABLE = "source_unavailable"
VALIDATION_INSUFFICIENT_COVERAGE = "insufficient_coverage"
VALIDATION_COMPARABLE = "comparable"


def _validation_report(
    validation_kwh: float | None,
    measured_kwh: float | None,
    coverage_so_far: float | None,
) -> dict[str, Any]:
    """Cross-check today's integrated measurement against the vendor day total.

    Diagnostic only, and structurally incapable of being anything else: nothing
    in the learning path reads the validation entity. It exists so a user can
    see, without exporting anything, whether this integration's quarter-hour
    integration of the house-load power sensor lands where the inverter's own
    daily counter does.

    Compared *within* today rather than against the previous day. The vendor
    counter resets at midnight and is never persisted here, so a previous-day
    comparison would require storing an end-of-day snapshot -- forecast-versus-
    actual bookkeeping, which belongs to a later phase and not to this one.

    Coverage is reported alongside because it is the first explanation for a
    gap: intervals this integration rejected are energy the vendor counter
    still saw, so a partly covered day is expected to read low here and that is
    not evidence of a measurement error.
    """
    if validation_kwh is None:
        return {
            "status": VALIDATION_SOURCE_UNAVAILABLE,
            "validation_total_kwh": None,
            "measured_total_kwh": (
                None if measured_kwh is None else round(measured_kwh, 3)
            ),
        }
    measured = 0.0 if measured_kwh is None else measured_kwh
    difference = measured - validation_kwh
    status = (
        VALIDATION_COMPARABLE
        if coverage_so_far is not None and coverage_so_far >= MIN_DAY_COMPLETENESS
        else VALIDATION_INSUFFICIENT_COVERAGE
    )
    return {
        "status": status,
        "validation_total_kwh": round(validation_kwh, 3),
        "measured_total_kwh": round(measured, 3),
        "difference_kwh": round(difference, 3),
        "difference_percent": (
            None
            if validation_kwh == 0
            else round(100.0 * difference / validation_kwh, 2)
        ),
        "measured_coverage_so_far": coverage_so_far,
    }


def _forecast_report(forecast: DayForecast | None) -> dict[str, Any]:
    """Summarise one baseline forecast, including why it is withheld.

    ``unavailable_reason`` is the field that matters in support: the safeguards
    are deliberate, so a withheld forecast is normally healthy, and this says
    which one fired rather than leaving the user guessing.
    """
    if forecast is None:
        return {"available": False, "unavailable_reason": REASON_NOT_BUILT}
    return {
        "available": forecast.available,
        "unavailable_reason": forecast.unavailable_reason,
        "total_kwh": (
            None if forecast.total_kwh is None else round(forecast.total_kwh, 3)
        ),
        # Learned days behind the model. Deliberately distinct from the
        # Learning Days sensor: that counts every day complete enough to learn,
        # this counts the ones actually backing a published forecast.
        "model_days": forecast.source_days,
        # Past days that contributed any observation at all, learned or not.
        "usable_days": forecast.usable_days,
        # Intervals blended from observations of their own behavioural slot,
        # versus intervals with no such observations that were filled from the
        # nearest neighbour. The two together always make up ``interval_count``;
        # a large ``filled_intervals`` means much of the day is extrapolated.
        "modelled_intervals": forecast.modelled_intervals,
        "filled_intervals": forecast.filled_intervals,
        "interval_count": forecast.interval_count,
        "day_type": forecast.day_type,
        "day_type_pooled": forecast.day_type_pooled,
        "windows_used_days": list(forecast.windows_used),
    }


#: Flagged days described individually in the diagnostics payload, newest
#: first. The flag *counts* beside them are always complete; this bounds only
#: the per-day detail, so a long-running installation cannot turn a diagnostics
#: download into a full history dump.
_MAX_EXCLUDED_DAYS_REPORTED = 10


async def _forecast_history_report(
    coordinator: AlphaEmsCoordinator, today: date
) -> dict[str, Any]:
    """Summarise the Phase-2 forecast evidence.

    Everything the two published sensors do not show lives here: the snapshot
    inventory, the lifecycle counts, per-horizon and per-slot error, the
    modelled-versus-filled split, matching health and storage health.

    Deep statistics are bounded to the longest window in
    ``FORECAST_METRIC_WINDOWS``, so a diagnostics download loads at most a
    handful of month partitions rather than a year of them.
    """
    history = coordinator.history
    recorder = coordinator.recorder
    oldest, newest = history.span

    provenance = {
        "forecast_schema_version": FORECAST_STORAGE_VERSION,
        "model_version": FORECAST_MODEL_VERSION,
        # Changes whenever a window, weight or threshold behind the forecast
        # changes. Two records with different values here describe two different
        # models and must never be pooled into one error statistic.
        "model_params_hash": model_params_hash(),
        # Versions the *comparison* rather than the forecast: which prediction a
        # day is scored against, and which days are judged incomparable. A row
        # written under an older value is re-derived while its snapshot and its
        # learning record are both still retained.
        "matcher_version": FORECAST_MATCHER_VERSION,
        "baseline_definition": baseline_definition(coordinator.config.ev_power_entity),
        "raw_retention_days": FORECAST_RAW_RETENTION_DAYS,
        "summary_retention_days": FORECAST_SUMMARY_RETENTION_DAYS,
        "max_snapshots_per_target": FORECAST_MAX_SNAPSHOTS_PER_TARGET,
    }

    storage = {
        "corrupt_on_load": history.corrupt,
        # While true, nothing at all is written: an empty in-memory view must
        # never be flushed over documents whose only problem may have been a
        # transient read error.
        "writes_suspended": history.corrupt,
        "reset_by_schema_migration": history.reset_by_migration,
        "partitions": history.partition_report(),
        "pruned_days": history.pruned_days,
        "snapshot_cap_hits": history.snapshot_cap_hits,
    }

    if history.corrupt:
        return {
            "available": False,
            "note": (
                "the forecast-history index could not be read, so no evidence "
                "is available this session and nothing is being written; "
                "learning and both forecasts are unaffected"
            ),
            "provenance": provenance,
            "storage": storage,
        }

    lifecycle = {
        LIFECYCLE_PENDING: 0,
        LIFECYCLE_VALIDATED: 0,
        LIFECYCLE_UNMATCHED: 0,
        LIFECYCLE_UNRESOLVED: 0,
    }
    horizons: dict[str, int] = {}
    for day, row in history.days.items():
        state = lifecycle_from_summary(
            day,
            today,
            finalized=row.finalized_at is not None,
            summary=row.summary,
        )
        lifecycle[state] += 1
        horizon = (row.summary or {}).get("h")
        if horizon is not None:
            horizons[str(horizon)] = horizons.get(str(horizon), 0) + 1

    # Cheap statistics first: rebuilt from the always-loaded index rows, so
    # these cost no disk access whatever the window.
    rolling = {
        f"{window}_days": window_from_summaries(
            row.summary
            for day, row in history.days.items()
            if today - timedelta(days=window) <= day < today and row.summary
        ).as_dict()
        for window in FORECAST_METRIC_WINDOWS
    }

    deep_window = max(FORECAST_METRIC_WINDOWS)
    start = today - timedelta(days=deep_window)
    await history.async_ensure_days(
        [day for day in history.days if start <= day < today]
    )
    detail = compute_window(recorder.scored_days(start, today))
    by_horizon = {
        str(horizon): compute_window(days).as_dict()
        for horizon, days in sorted(
            recorder.scored_days_by_horizon(start, today).items()
        )
    }

    status_counts: dict[str, int] = {}
    flag_counts: dict[str, int] = {}
    excluded: list[dict[str, Any]] = []
    excluded_total = 0
    for day in sorted(history.days, reverse=True):
        outcome = history.outcome(day)
        if outcome is None:
            continue
        for code in outcome.status:
            status_counts[code] = status_counts.get(code, 0) + 1
        for flag in outcome.flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
        if not outcome.flags:
            continue
        excluded_total += 1
        if len(excluded) >= _MAX_EXCLUDED_DAYS_REPORTED:
            continue
        # A flag on its own says a day was excluded, not why. The facts that
        # decide each flag are all in the record already, so reporting them
        # beside it is the difference between "definition_changed: 1" and a
        # diagnosable day.
        excluded.append(
            {
                "target_day": day.isoformat(),
                "flags": list(outcome.flags),
                "intervals_in_day": outcome.interval_count,
                "intervals_with_valid_actual": len(outcome.valid_indices()),
                "record_timezone": outcome.tz_key,
                # Non-null exactly when some observed interval expected a
                # flexible load, which is what the day's own baseline
                # definition is judged from.
                "flexible_total_kwh": outcome.flexible_total_kwh,
                "snapshot_baseline_definitions": sorted(
                    {
                        snapshot.baseline_definition
                        for snapshot in history.snapshots(day)
                    }
                ),
                "snapshot_interval_counts": sorted(
                    {snapshot.interval_count for snapshot in history.snapshots(day)}
                ),
                "snapshot_timezones": sorted(
                    {snapshot.tz_key for snapshot in history.snapshots(day)}
                ),
            }
        )

    return {
        "available": True,
        "provenance": provenance,
        "inventory": {
            "target_days": len(history.days),
            "snapshots": history.snapshot_total,
            "oldest_target": None if oldest is None else oldest.isoformat(),
            "newest_target": None if newest is None else newest.isoformat(),
            "snapshots_by_scored_horizon": horizons,
            "lifecycle": lifecycle,
            # Rises only when finalisation is suspended or a partition is
            # unreadable; a steady non-zero value here is the thing to chase.
            "unresolved_days": coordinator.last_record.unresolved_days,
            "finalization_suspended": coordinator.last_record.finalization_suspended,
            "finalization_suspended_reason": (
                "the learning store is in its failed-read state, so every "
                "actual would read as missing; matching is deliberately "
                "postponed rather than recording that permanently"
                if coordinator.last_record.finalization_suspended
                else None
            ),
        },
        "issuance": {
            "policy": (
                "change-triggered: a snapshot is written only when the "
                "forecast content and provenance differ from the last one kept "
                "for that target day"
            ),
            "issued_last_refresh": len(coordinator.last_record.issued),
            "duplicates_suppressed": recorder.duplicate_issuances,
            "finalized_last_refresh": [
                day.isoformat() for day in coordinator.last_record.finalized
            ],
        },
        "quality": {
            "sign_convention": (
                "error = predicted - actual; positive means the model "
                "predicted more than was measured"
            ),
            "percentage_basis": (
                "WAPE = sum(|error|) / sum(actual) over the window. No "
                "per-interval percentage is computed: a near-zero overnight "
                "actual makes one meaningless"
            ),
            "rolling": rolling,
            f"detail_{deep_window}_days": detail.as_dict(),
            "by_horizon": by_horizon,
            # The two published sensors, reported beside the figures they are
            # derived from. ``rolling`` is deliberately ungated -- a maintainer
            # wants the statistic whatever its sample size -- so without this a
            # download showed a WAPE of 25 % next to an entity reading
            # ``unknown``, with nothing in the payload explaining which of the
            # two was wrong. Neither was.
            "published": {
                "minimum_intervals_for_metric": FORECAST_MIN_INTERVALS_FOR_METRIC,
                "gate": (
                    "the rolling sensor withholds its rate, and only its rate, "
                    "until the window holds at least that many compared "
                    "intervals; the sample size and the two energy totals are "
                    "reported throughout"
                ),
                "forecast_error_yesterday": coordinator.last_record.yesterday,
                "forecast_error_window": coordinator.last_record.window.as_dict(),
            },
        },
        "matching": {
            "interval_status_counts": status_counts,
            "status_legend": {
                "0": "valid baseline measurement",
                "1": "no usable measured reading",
                "2": "measured present, flexible-load reading unusable",
                "3": "interval had not elapsed",
            },
            "excluded_day_flags": flag_counts,
            # Newest first, and capped so a diagnostics download cannot grow
            # with the history. The counts above are complete either way.
            "excluded_days": excluded,
            "excluded_days_reported": len(excluded),
            "excluded_days_total": excluded_total,
            "restated_last_refresh": [
                day.isoformat() for day in coordinator.last_record.restated
            ],
            "actual_basis": (
                "baseline = max(measured - flexible, 0), the same quantity the "
                "model predicts; a missing actual is never read as zero"
            ),
        },
        "storage": storage,
    }


def _battery_report(coordinator: AlphaEmsCoordinator, tz: Any) -> dict[str, Any]:
    """Summarise the Phase-3 decision layer.

    Everything the three published entities do not show: the input state, both
    floors and where the effective one came from, the derived usable window, the
    reduced trajectory, the per-band split, which limit bound where, the hold
    comparison and the PV-blind projection.

    Bounded by construction. ``plan_as_dict`` publishes no per-interval array and
    caps its one list of binding intervals, because every list anywhere in this
    payload is held to sixteen entries -- a ninety-six-interval trajectory would
    turn a support download into a history dump.
    """
    plan = coordinator.battery_plan
    configured = coordinator.battery_planning_configured
    if plan is None:
        return {
            "available": False,
            # Two quite different absences, told apart. A missing hardware fact
            # is the user's to fill in; a failed evaluation is a fault.
            "note": (
                "no battery plan was produced this refresh: the decision layer "
                "either raised and was isolated, or the coordinator has not "
                "refreshed yet. Learning, both forecasts and the forecast-error "
                "sensors are unaffected"
                if configured
                else "battery planning is not configured; enter the capacity and "
                "the two power limits under Options to enable it"
            ),
            "hardware_configured": configured,
            "controls_nothing": (
                "Phase 3 is observation only and issues no command to the battery"
            ),
        }

    payload = plan_as_dict(plan, tz)
    payload["hardware_configured"] = configured
    payload["policy_catalogue"] = {
        "shipped": [policy.identity for policy in SHIPPED_POLICIES],
        "default": DEFAULT_POLICY.identity,
        "charging_rule": (
            "no policy shipped in this phase ever asks to charge: every reason "
            "to would need photovoltaic, price or dynamic-reserve information "
            "that belongs to a later phase. The charge path exists, is clamped "
            "and is simulated, so what-if comparison and later phases have "
            "somewhere to land"
        ),
    }
    return payload


#: Most sites reported individually. A list in a diagnostics payload is held to
#: sixteen entries, and an account with more roofs than this is described by its
#: counts and its declared set rather than by silently truncating the list -- a
#: truncated list reads as complete, which is worse than an explicit count.
MAX_PV_SITES_REPORTED = 12


def _pv_site_report(
    facts: SolcastFacts | None, selected: tuple[str, ...]
) -> dict[str, Any]:
    """Return what is known about the rooftop sites, bounded."""
    if facts is None:
        return {
            "discovered": None,
            "note": (
                "the source's own diagnostic could not be read this refresh, so "
                "nothing is claimed about which sites exist"
            ),
        }

    known = {site.resource_id for site in facts.sites}
    listed = sorted(facts.sites, key=lambda site: site.name.lower())
    return {
        "discovered": len(facts.sites),
        "selected": len(selected),
        "sites": [site.as_dict() for site in listed[:MAX_PV_SITES_REPORTED]],
        "sites_not_listed": max(0, len(listed) - MAX_PV_SITES_REPORTED),
        "selected_ids": list(selected[:MAX_PV_SITES_REPORTED]),
        # Declared but no longer offered by the source. Reported rather than
        # dropped: a declaration must not narrow itself because of an outage.
        "selected_but_missing": sorted(set(selected) - known)[:MAX_PV_SITES_REPORTED],
        # Present in the account and deliberately not part of this installation.
        "available_but_not_selected": sorted(known - set(selected))[
            :MAX_PV_SITES_REPORTED
        ],
        "excluded_by_source": list(facts.excluded_sites[:MAX_PV_SITES_REPORTED]),
        "question_asked": (
            "which rooftop sites belong to this installation, and nothing else. "
            "the user is never asked which site is AC- or DC-coupled, or which "
            "feeds the hybrid: that is not reliably knowable to them, and a "
            "guessed topology recorded as fact would be worse than the declared "
            "unknown stored instead"
        ),
    }


def _pv_forecast_report(forecast: PvForecast | None) -> dict[str, Any]:
    """Return one day's forecast state, counts only."""
    if forecast is None:
        return {"available": False, "unavailable_reason": "not_evaluated"}
    payload = forecast.as_dict()
    payload["percentiles_available"] = bool(
        [value for value in forecast.p10 if value is not None]
    )
    return payload


def _price_report(forecast: PriceForecast | None) -> dict[str, Any]:
    """Return one day's price series as counts, edges and status.

    Never the series itself. A day is ninety-six intervals and the payload is
    capped at sixteen list entries, so printing prices here would be truncated
    into something misleading rather than merely large.
    """
    if forecast is None:
        return {"available": False, "unavailable_reason": REASON_NOT_BUILT}
    return forecast.as_dict()


def _price_evidence_report(history: Any, today: date) -> dict[str, Any]:
    """Return what price evidence has been recorded, without the arrays."""
    yesterday = today - timedelta(days=1)
    latest = history.latest_price_snapshot(today)
    return {
        "issuances_today": len(history.price_snapshots(today)),
        "issuances_yesterday": len(history.price_snapshots(yesterday)),
        "latest_issued_at": (None if latest is None else latest.issued_at.isoformat()),
        "latest_intervals_known": None if latest is None else latest.intervals_known,
        "latest_flags": [] if latest is None else list(latest.flags),
        "mapping_version": PRICE_MAPPING_VERSION,
        "note": (
            "issuances are change-triggered by content fingerprint, so a day "
            "with one entry means the series has not changed since it was first "
            "read -- not that recording failed. there is deliberately no outcome "
            "half: a price has no 'what actually happened' to be scored against"
        ),
    }


def _price_derived_report(
    hass: HomeAssistant, capability: FrankCapability
) -> dict[str, Any]:
    """Return the source's own derived figures, labelled as context.

    Reported so a reader can see them, and **never** consumed. The cheap and
    expensive zones are computed from margins configured on the *source's* entry,
    and the optimal-period entities are a precomputed answer to a question that
    needs load, generation, state of charge, efficiency, limits and reserve to
    answer honestly. Treating either as input would make this integration's
    behaviour depend on another integration's settings.
    """
    current_import, current_export = read_current_prices(hass, capability)
    return {
        "current_import_eur_kwh": current_import,
        "current_export_eur_kwh": current_export,
        "note": (
            "the source's own current-interval figures, read only to cross-check "
            "the normalised series against what the user can see. a disagreement "
            "is recorded as contract drift and never overrides the series"
        ),
        "zones_and_optimal_periods": (
            "not read. those entities are derived from margins configured on the "
            "source's entry, so consuming them would make this integration's "
            "behaviour depend on somebody else's thresholds"
        ),
    }


def _pv_evidence_report(history: Any, today: date) -> dict[str, Any]:
    """Return the stored forecast-versus-actual evidence, derived on demand.

    The metrics are recomputed from the two stored sides rather than stored, which
    is the rule the load-side metrics already follow: a stored statistic is a
    second source of truth, and the first time it disagreed with the arrays beside
    it, it is the stored one that would be believed.
    """
    yesterday = today - timedelta(days=1)
    snapshot = history.latest_pv_snapshot(yesterday)
    outcome = history.pv_outcome(yesterday)
    return {
        "target_day": yesterday.isoformat(),
        "snapshots_today": len(history.pv_snapshots(today)),
        "snapshots_yesterday": len(history.pv_snapshots(yesterday)),
        "issued_total_kwh": None if snapshot is None else snapshot.total_kwh,
        "comparison": pv_error_metrics(snapshot, outcome),
        "adaptive_correction": (
            "none. Phase 5 records what was forecast and what was measured and "
            "computes no correction, no bias term and no dampening of its own, so "
            "a bad day cannot change the next day's forecast"
        ),
    }


def _pv_actual_report(record: DayRecord | None, elapsed: int) -> dict[str, Any]:
    """Return how much measured generation today has, and how complete it is."""
    if record is None:
        return {"intervals_recorded": 0, "total_kwh": 0.0}
    covered = max(1, elapsed)
    return {
        "intervals_recorded": record.pv_sample_count,
        "total_kwh": record.pv_total_kwh,
        "coverage_so_far": _fraction(record.pv_sample_count, covered),
        "note": (
            "measured generation, integrated on the same machinery as house "
            "load and subject to the same coverage threshold. a missing interval "
            "is missing, never zero"
        ),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AlphaEmsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    # Home Assistant deletes ``runtime_data`` on unload and offers the diagnostics
    # download regardless of entry state, so this must not assume a loaded entry.
    # The state that matters most is the one this release's migration guard
    # produces: a legacy v1 entry sits in MIGRATION_ERROR, and "download
    # diagnostics" is the first thing anyone asks such a user for. Reaching for
    # ``entry.runtime_data`` there raised AttributeError and returned HTTP 500.
    coordinator: AlphaEmsCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return {
            "integration": {
                "loaded": False,
                "state": entry.state.value,
                "config_entry_version": entry.version,
                "expected_config_entry_version": CONFIG_ENTRY_VERSION,
                "note": (
                    "This entry is not loaded, so no runtime data exists. A "
                    "config-entry version below the expected one means the entry "
                    "predates the Phase 1 source model and cannot be migrated; "
                    "remove the integration and add it again."
                ),
            }
        }

    config = coordinator.config
    store = coordinator.store
    oldest, newest = store.span

    now = dt_util.now()
    tz = dt_util.get_default_time_zone()
    today_date = now.date()

    records = list(store.days.values())
    real_intervals = sum(record.interval_count for record in records)
    # Denominator for every *so-far* coverage figure. Counting a running day
    # against its whole civil length reports the unlived remainder of today as
    # missing data, so a healthy install looked broken all morning and recovered
    # by itself at midnight. Each record contributes its own elapsed count, in
    # the zone it was recorded in, so a finalised day still contributes its full
    # 92/96/100 and only today is measured against what has actually happened.
    occurred_intervals = sum(
        elapsed_quarters_for(record.day, record.tz, now) for record in records
    )
    measured_valid = sum(record.measured_valid_count for record in records)
    baseline_valid = sum(record.baseline_valid_count for record in records)

    today = coordinator.today_forecast
    tomorrow = coordinator.tomorrow_forecast
    # Today's *baseline* forecast, before same-day adaptation. This is the
    # object the Today entity gates its availability on, so reporting it is
    # what keeps diagnostics and the entity telling the same story.
    today_baseline = (coordinator.data or {}).get("today_baseline")
    confidence = coordinator.confidence
    # The published learned-day count, i.e. the Learning Days sensor's state.
    learned_days = (coordinator.data or {}).get("learning_days")
    if learned_days is None:
        # No successful refresh yet, so there is no published value to agree
        # with. Fall back to the same filtered computation the coordinator uses,
        # which keeps the field an integer rather than turning it null.
        learned_days = len(coordinator.learned_day_dates())

    return {
        "integration": {
            "version": entry.version,
            "entry_title": entry.title,
            "learning_interval_minutes": 15,
            "slots_per_day": SLOTS_PER_DAY,
        },
        "sources": {
            "house_load": _source_report(hass, config.house_load_entity),
            "daily_house_load_validation": _source_report(
                hass, config.daily_house_load_entity
            ),
            "ev_power": _source_report(hass, config.ev_power_entity),
            "battery_soc": _source_report(hass, config.battery_soc_entity),
            "battery_power": _source_report(hass, config.battery_power_entity),
            "pv_power": _source_report(hass, config.pv_power_entity),
            "grid_power": _source_report(hass, config.grid_power_entity),
        },
        "sign_conventions": {
            "battery_power": config.battery_power_sign,
            "grid_power": config.grid_power_sign,
            "canonical": (
                "house_load >= 0, pv >= 0, battery_charge >= 0, "
                "battery_discharge >= 0, grid_import >= 0, grid_export >= 0"
            ),
        },
        "normalized_flows_now": asdict(coordinator.read_flows()),
        "daily_validation_kwh": coordinator.read_daily_house_load_kwh(),
        # Cross-check only. The validation entity is read here and nowhere else
        # in the integration: it cannot affect interval acceptance, day
        # acceptance, learned days, forecast training, confidence or adaptation,
        # and learning is never rejected because the vendor's daily total
        # disagrees.
        "daily_validation": {
            "role": (
                "diagnostic cross-check only; read by diagnostics and by no "
                "part of the learning, forecast or confidence path"
            ),
            "configured": bool(config.daily_house_load_entity),
            **(
                {"status": VALIDATION_NOT_CONFIGURED}
                if not config.daily_house_load_entity
                else _validation_report(
                    coordinator.read_daily_house_load_kwh(),
                    (coordinator.data or {}).get("measured_so_far_kwh"),
                    _fraction(
                        0
                        if store.days.get(today_date) is None
                        else store.days[today_date].measured_valid_count,
                        elapsed_quarters_for(today_date, tz, now),
                    ),
                )
            ),
        },
        "learning": {
            # Taken from the value the coordinator already published, which is
            # the same object the Learning Days sensor reads, rather than being
            # recomputed here. Recomputing it called ``learned_days()`` without
            # ``before``, so it counted the in-progress day from the moment its
            # baseline coverage crossed MIN_DAY_COMPLETENESS -- around 19:15 on a
            # clean day -- and a download taken that evening reported one more
            # learned day than the entity showed. Reading the published value
            # makes the two incapable of disagreeing.
            "learned_days": learned_days,
            "retained_days": len(store.days),
            # Real quarter-hours, so a fall-back day contributes 100 and a
            # spring-forward day 92.
            "retained_real_intervals": real_intervals,
            "history_start": None if oldest is None else oldest.isoformat(),
            "history_end": None if newest is None else newest.isoformat(),
            # Intervals that have actually elapsed across the retained days.
            # This -- not ``retained_real_intervals`` -- is what the coverage
            # fractions below divide by.
            "occurred_intervals": occurred_intervals,
            "measured_valid_intervals": measured_valid,
            "measured_missing_intervals": max(0, occurred_intervals - measured_valid),
            "measured_coverage": _fraction(measured_valid, occurred_intervals),
            "baseline_valid_intervals": baseline_valid,
            "baseline_coverage": _fraction(baseline_valid, occurred_intervals),
            "coverage_basis": (
                "valid intervals / intervals that have already elapsed; a "
                "finalised day contributes its whole civil-day length "
                "(92/96/100) and the running day only the quarters that have "
                "closed, so the unlived remainder of today is never counted as "
                "missing data"
            ),
            # The same figures over finalised days only. These are the ones that
            # feed learned-day qualification and the confidence score; the block
            # above additionally includes whatever today has managed so far.
            "completed_days": _coverage_report(
                [record for record in records if record.day < today_date]
            ),
            # The running day, reported separately because it is judged by
            # nothing: it cannot be a learned day, cannot enter the confidence
            # score and cannot be an input to its own forecast until it is
            # finalised at midnight.
            "current_day": _current_day_report(
                store.days.get(today_date), today_date, tz, now
            ),
            "rejected_quarters": coordinator.rejected_quarters,
            # Why quarters were rejected, not merely how many. Every route to a
            # rejection ends in "coverage too low", so the bare count could not
            # tell a normal restart apart from a house-load entity that has been
            # publishing kWh instead of W since the day it was selected.
            "rejected_quarters_by_reason": dict(
                coordinator.rejected_quarters_by_reason
            ),
            "last_rejected_quarter": (
                None
                if coordinator.last_rejected_quarter is None
                else coordinator.last_rejected_quarter.isoformat()
            ),
            "last_rejected_reason": coordinator.last_rejected_reason,
            "open_quarter_coverage": round(coordinator.open_quarter_coverage, 3),
            "last_finalized_quarter": store.last_finalized,
            "min_quarter_coverage": MIN_QUARTER_COVERAGE,
            "min_day_completeness": MIN_DAY_COMPLETENESS,
        },
        "flexible_load": {
            "kind": "ev_charging",
            "configured": coordinator.ev_configured,
            "entity": _source_report(hass, config.ev_power_entity),
            "available_now": coordinator.ev_available,
            "current_power_w": coordinator.current_ev_power_w,
            "open_interval_coverage": coordinator.ev_open_quarter_coverage,
            "intervals_without_valid_data": coordinator.invalid_ev_quarters,
            "intervals_without_valid_data_by_reason": dict(
                coordinator.invalid_ev_quarters_by_reason
            ),
            "baseline_rule": (
                "baseline = max(measured - flexible, 0); an interval with a "
                "configured but unreadable flexible load has no valid baseline"
            ),
        },
        # The two forecasts report the same fields, including *why* nothing is
        # published. A withheld forecast is usually correct, but "unknown" alone
        # cannot be told apart from a fault without this -- which is exactly how
        # a live installation ended up showing a day total here for a sensor
        # reading `unknown`. These figures now come from the same availability
        # rule the entities use, so the two can no longer disagree.
        "forecast": {
            "today_total_kwh": (
                None
                if today is None or today.forecast_total_kwh is None
                else round(today.forecast_total_kwh, 3)
            ),
            "today_remaining_kwh": (
                None
                if today is None or today.forecast_remaining_kwh is None
                else round(today.forecast_remaining_kwh, 3)
            ),
            "today_actual_so_far_kwh": (
                None if today is None else round(today.actual_so_far_kwh, 3)
            ),
            "today_adaptation_ratio": (
                None if today is None else round(today.adaptation_ratio, 3)
            ),
            "today_available": None if today is None else today.available,
            "tomorrow_total_kwh": (
                None
                if tomorrow is None or tomorrow.total_kwh is None
                else round(tomorrow.total_kwh, 3)
            ),
            "tomorrow_day_type": None if tomorrow is None else tomorrow.day_type,
            "tomorrow_day_type_pooled": (
                None if tomorrow is None else tomorrow.day_type_pooled
            ),
            "windows_used_days": (
                [] if tomorrow is None else list(tomorrow.windows_used)
            ),
            "forecast_today": _forecast_report(today_baseline),
            "forecast_tomorrow": _forecast_report(tomorrow),
        },
        # Note the population: every figure here is computed over *learned*
        # days only, so ``confidence.coverage`` and ``learning.baseline_coverage``
        # answer different questions and are expected to differ while today is
        # in progress or while a retained day fell short of being learned.
        "confidence": (
            None
            if confidence is None
            else {
                **confidence.as_dict(),
                "population": "learned days only, excluding the day in progress",
            }
        ),
        # Session-scoped counters plus the persisted tally. The session view is
        # what shows whether the sources are updating coherently right now; the
        # persisted pass rate is what feeds the confidence score.
        "energy_balance": {
            **coordinator.balance.as_dict(),
            "source_entities": coordinator.balance_source_entities,
            # Lifted out of the last sample, as advertised, so a residual can be
            # attributed to an operating mode without digging through the nested
            # payload -- and so this cannot contradict ``last_sample.mode``.
            #
            # It used to re-read the state machine instead. That labelled a mode
            # for snapshots ``evaluate_balance`` had refused to judge at all: a
            # partial snapshot returns no verdict, but ``infer_balance_mode``
            # happily describes whichever flows were present, so the payload
            # asserted an operating mode for a system with no balance verdict.
            "active_balance_mode": (
                None
                if coordinator.last_balance is None
                else coordinator.last_balance.mode
            ),
            "source_time_skew_seconds": (
                None
                if coordinator.last_balance is None
                or coordinator.last_balance.coherence is None
                else round(coordinator.last_balance.coherence.skew_seconds, 1)
            ),
            "persisted_pass_rate": store.balance.score,
            "persisted_samples": store.balance.total_samples,
        },
        "storage": {
            "schema_version": STORAGE_VERSION,
            "interval_identity": (
                "chronological index from local midnight; 92/96/100 per civil "
                "day so daylight-saving transitions are represented exactly"
            ),
            "retention_days": MAX_HISTORY_DAYS,
            "corrupt_on_load": store.corrupt,
            # True while the document could not be read. Writes are suspended
            # for the session so an empty in-memory history cannot overwrite a
            # file whose only problem may have been a transient I/O error.
            "writes_suspended": store.corrupt,
            # True when a pre-v2 document was discarded on load. Without this,
            # "the schema migration threw my history away" and "this is a fresh
            # install" are the same payload.
            "reset_by_schema_migration": store.reset_by_migration,
        },
        # Phase 2. Everything the evidence layer records beyond the two
        # published error figures: the inventory, the lifecycle counts, the
        # per-horizon and per-slot breakdowns and the storage health.
        "forecast_history": await _forecast_history_report(coordinator, today_date),
        # Phase 3. The decision and simulation layer: what it read, what it
        # concluded, which limit bound it, and what would have happened. Nothing
        # here is ever executed.
        "battery_plan": _battery_report(coordinator, tz),
        # Phase 4. What the control pipeline made of that decision: which parts
        # of the control surface were found, what the inverter is doing, the
        # intent, the quantised command, the exact ordered command list, the
        # safety verdict and the authorization refusal. Populated identically in
        # shadow and active, which is what makes shadow worth reading: the
        # verdict and the command list below are the real ones. Nothing is sent.
        "control": coordinator.control_report,
        "pv": {
            "enabled": config.use_pv_forecast,
            # Probed **now**, not read from the last refresh. The two are not the
            # same thing, and printing a stale capability beside live source
            # readings is what made the beta.9 defect look like a contradiction:
            # the block said the source was unusable while the field below it
            # said it was available, because they described different instants.
            "capability": discover_solcast(hass, config.solcast_entry_id).as_dict(),
            "capability_at_last_refresh": coordinator.pv_capability.as_dict(),
            "source": (
                None if coordinator.pv_facts is None else coordinator.pv_facts.as_dict()
            ),
            "sites": _pv_site_report(
                coordinator.pv_facts, config.selected_solcast_site_ids
            ),
            "selection_stored": config.solcast_selection_stored,
            "forecast_today": _pv_forecast_report(
                coordinator.pv_forecasts.get(today_date)
            ),
            "forecast_tomorrow": _pv_forecast_report(
                coordinator.pv_forecasts.get(today_date + timedelta(days=1))
            ),
            "mapping": (
                coordinator.pv_forecasts[today_date].mapping.as_dict()
                if today_date in coordinator.pv_forecasts
                else None
            ),
            "provenance": (
                coordinator.pv_forecasts[today_date].provenance.as_dict()
                if today_date in coordinator.pv_forecasts
                else None
            ),
            "absorption": (coordinator.data or {}).get("pv_absorption"),
            "actual_today": _pv_actual_report(
                store.days.get(today_date),
                elapsed_quarters_for(today_date, tz, now),
            ),
            "evidence": _pv_evidence_report(coordinator.history, today_date),
            "quota": {
                "note": (
                    "the two actions Alpha EMS calls read the source's own cache "
                    "and consume none of the account's allowance, so a low "
                    "remaining count explains a stale forecast without being "
                    "caused by this integration"
                ),
            },
            # When the forecast blocks above were computed. Everything in them is
            # a snapshot from that instant; the capability above is not.
            "last_refresh_at": (
                None
                if coordinator.last_refresh_at is None
                else coordinator.last_refresh_at.isoformat()
            ),
            "daylight_note": (
                "the daylight window is advisory: it never modifies a forecast "
                "value and sits on no safety path. generation forecast outside it "
                "is the signature of a timezone or offset error, and is reported "
                "rather than clamped"
            ),
        },
        # Phase 6. What electricity costs, and the fact that it changes nothing.
        # Every field here is a fact or a named absence; none of it reaches a
        # decision, and the guards that make that structural are listed under
        # ``neutrality`` so this block can be read on its own.
        "price": {
            "entry_selected": config.frank_entry_id is not None,
            # Probed **now**, like the PV capability beside it and for the same
            # reason: printing a stale capability next to live readings is what
            # made an earlier defect read as a contradiction.
            "capability": discover_frank(hass, config.frank_entry_id).as_dict(),
            "capability_at_last_refresh": coordinator.price_capability.as_dict(),
            "options": {
                "readable": coordinator.price_options.readable,
                "feed_in_adjustment": coordinator.price_options.adjustment,
                "apply_feed_in_vat": coordinator.price_options.apply_vat,
                "note": (
                    "read from the price integration's own entry, never "
                    "duplicated as an alpha ems setting: the return-price figure "
                    "on the user's dashboard is derived from these, and a second "
                    "copy here would drift away from it"
                ),
            },
            "today": _price_report(coordinator.price_forecasts.get(today_date)),
            "tomorrow": _price_report(
                coordinator.price_forecasts.get(today_date + timedelta(days=1))
            ),
            "mapping": (
                coordinator.price_forecasts[today_date].mapping.as_dict()
                if today_date in coordinator.price_forecasts
                else None
            ),
            "provenance": (
                coordinator.price_forecasts[today_date].provenance.as_dict()
                if today_date in coordinator.price_forecasts
                else None
            ),
            "evidence": _price_evidence_report(coordinator.history, today_date),
            "derived_source_data": _price_derived_report(
                hass, coordinator.price_capability
            ),
            "boundaries": (
                "the source publishes a market day -- midnight to midnight in "
                "the market's own zone -- while this integration plans a home "
                "assistant civil day. for anyone running outside the market zone "
                "those are different spans, so coverage below 1.0 is normal and "
                "part of the local day is priced by a market day that may not be "
                "published yet. mapping is by instant for exactly this reason"
            ),
            "neutrality": (
                "prices are known and change nothing. the price layer is not "
                "imported by plan, policy, simulation, battery, control or "
                "safety; no identifier in those modules is an economic term; and "
                "obtaining prices calls no service at all, so this integration "
                "cannot make the source fetch. all three are asserted "
                "structurally rather than by comparing behaviour"
            ),
        },
        "consumed_integrations": {
            "frank_entry_id": config.frank_entry_id,
            # Established from resolvable entities, never from that entry's setup
            # state. The lifecycle probe this used to call is gone; see the
            # ``price.capability`` block above for what is actually checked.
            "frank_available": coordinator.frank_available,
            "pv_forecast_enabled": config.use_pv_forecast,
            "solcast_entry_id": config.solcast_entry_id,
            "solcast_available": coordinator.solcast_available,
        },
    }
