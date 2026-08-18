"""Diagnostics for Alpha EMS Manager.

This is where everything that does *not* justify an entity goes: per-source
availability, normalised readings, sign conventions, coverage statistics, the
confidence derivation and the energy-balance residual.

The payload carries no credentials, tokens or account data -- this integration
holds none, because it never talks to an external service. Nor does it dump the
full year of learned history; that would be megabytes of quarter buckets. Only
the summary a support conversation actually needs is included.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import AlphaEmsConfigEntry
from .const import (
    CONFIG_ENTRY_VERSION,
    MAX_HISTORY_DAYS,
    MIN_DAY_COMPLETENESS,
    MIN_QUARTER_COVERAGE,
    SLOTS_PER_DAY,
    STORAGE_VERSION,
)
from .coordinator import AlphaEmsCoordinator
from .forecast import REASON_NOT_BUILT, DayForecast
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
        "consumed_integrations": {
            "frank_entry_id": config.frank_entry_id,
            "frank_available": coordinator.frank_available,
            "pv_forecast_enabled": config.use_pv_forecast,
            "solcast_entry_id": config.solcast_entry_id,
            "solcast_available": coordinator.solcast_available,
        },
    }
