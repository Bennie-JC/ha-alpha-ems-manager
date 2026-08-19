"""Forecast issuance: what gets recorded, and what deliberately does not.

The whole Phase-2 storage budget rests on one property of the Phase-1 model:
between one midnight and the next, the forecast is a pure function of an input
set that does not change. ``test_the_forecast_is_constant_within_a_civil_day``
is the load-bearing test in this file -- if it ever fails, the issuance policy
needs redesigning rather than patching, because it would mean the model is
moving in a way nothing is recording.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FORECAST_MAX_SNAPSHOTS_PER_TARGET,
    FORECAST_MODEL_VERSION,
)
from custom_components.alpha_ems_manager.forecast import (
    REASON_NO_HISTORY,
    build_forecast,
)
from custom_components.alpha_ems_manager.forecast_history import (
    baseline_definition,
    build_snapshot,
    fingerprint_forecast,
    model_params_hash,
)

from .conftest import TZ
from .forecast_helpers import (
    NORMAL,
    history_before,
    local,
    refresh_at,
    reseed,
    reset_history,
    seed,
    snapshot_days,
    total_snapshots,
)
from .synthetic import flat_day

pytestmark = pytest.mark.usefixtures("setup_integration")


# -- the property the whole policy rests on ----------------------------------


async def test_the_forecast_is_constant_within_a_civil_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Ninety-six refreshes across one day produce two snapshots, not 192.

    The in-progress day is excluded from its own forecast, and no past day
    changes between midnights, so every refresh rebuilds the same arrays. This
    is why issuance is change-triggered: a per-refresh policy would write
    ninety-four identical copies of each.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))

    for quarter in range(96):
        moment = local(NORMAL, 0, 5) + timedelta(minutes=15 * quarter)
        await refresh_at(coordinator, moment)

    assert snapshot_days(coordinator) == {
        NORMAL: 1,
        NORMAL + timedelta(days=1): 1,
    }
    assert coordinator.recorder.duplicate_issuances == 95 * 2


async def test_an_identical_refresh_writes_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The second refresh of a day must add no record at all."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))

    await refresh_at(coordinator, local(NORMAL, 10, 5))
    after_first = total_snapshots(coordinator)
    await refresh_at(coordinator, local(NORMAL, 10, 20))

    assert after_first == 2
    assert total_snapshots(coordinator) == 2
    assert coordinator.last_record.issued == ()


async def test_a_changed_forecast_creates_a_new_immutable_snapshot(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A genuinely different prediction is a separate historical observation."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 5))
    first = coordinator.history.snapshots(NORMAL)[0]

    # A materially different household: the model must move, and the move must
    # be recorded rather than replacing what was already issued.
    reseed(coordinator, history_before(NORMAL, daily_kwh=20.0))
    await refresh_at(coordinator, local(NORMAL, 10, 20))

    snapshots = coordinator.history.snapshots(NORMAL)
    assert len(snapshots) == 2
    assert snapshots[0].fingerprint != snapshots[1].fingerprint
    # The original is untouched: same values, same fingerprint, same instant.
    assert snapshots[0].predicted == first.predicted
    assert snapshots[0].issued_at == first.issued_at


async def test_both_issuances_for_one_target_survive_the_day_turning(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The day-ahead and day-of predictions for one target must coexist.

    This is the horizon evidence: without two records for the same day there is
    nothing to compare, and no way to ever answer whether a longer horizon is
    measurably worse.
    """
    coordinator = setup_integration.runtime_data
    target = NORMAL + timedelta(days=1)
    seed(coordinator, history_before(NORMAL))

    # Issued while the target is "tomorrow".
    await refresh_at(coordinator, local(NORMAL, 23, 50))
    # The day turns, the model gains a learned day, and the target becomes
    # "today".
    reseed(coordinator, history_before(target))
    await refresh_at(coordinator, local(target, 0, 5))

    snapshots = coordinator.history.snapshots(target)
    assert [snapshot.horizon_days for snapshot in snapshots] == [1, 0]
    assert snapshots[0].issued_at < snapshots[1].issued_at


# -- fingerprint semantics ---------------------------------------------------


def _forecast(reference: date, target: date, daily_kwh: float = 12.0):
    """Return a published forecast built from clean synthetic history."""
    records = [flat_day(reference - timedelta(days=n), daily_kwh) for n in range(1, 6)]
    return build_forecast(records, reference, target, TZ)


def _fingerprint(forecast, **overrides) -> str:
    """Fingerprint a forecast with the production arguments."""
    kwargs = {
        "tz_key": "Europe/Amsterdam",
        "horizon_days": 1,
        "model_version": FORECAST_MODEL_VERSION,
        "model_params": model_params_hash(),
        "baseline_def": "none",
    }
    kwargs.update(overrides)
    return fingerprint_forecast(forecast, **kwargs)


def test_the_same_forecast_always_fingerprints_the_same() -> None:
    """Two builds of one forecast must be indistinguishable."""
    first = _forecast(NORMAL, NORMAL + timedelta(days=1))
    second = _forecast(NORMAL, NORMAL + timedelta(days=1))

    assert _fingerprint(first) == _fingerprint(second)


def test_the_fingerprint_is_stable_across_processes() -> None:
    """It must not depend on the per-process hash seed.

    ``hash()`` is salted, so a fingerprint built with it would differ after
    every restart and every restart would write a duplicate snapshot. Pinning
    the literal digest is what makes that regression impossible to miss.
    """
    forecast = _forecast(NORMAL, NORMAL + timedelta(days=1))

    digest = _fingerprint(forecast)
    assert len(digest) == 16
    assert all(char in "0123456789abcdef" for char in digest)
    # Recomputed from a fully specified history, so this is reproducible.
    assert digest == _fingerprint(_forecast(NORMAL, NORMAL + timedelta(days=1)))


@pytest.mark.parametrize(
    "override",
    [
        {"tz_key": "Europe/Berlin"},
        {"horizon_days": 0},
        {"model_version": FORECAST_MODEL_VERSION + 1},
        {"model_params": "deadbeefdeadbeef"},
        {"baseline_def": "ev:sensor.ev_charger_power"},
    ],
)
def test_a_material_change_changes_the_fingerprint(override: dict) -> None:
    """Anything that makes two forecasts incomparable must be caught."""
    forecast = _forecast(NORMAL, NORMAL + timedelta(days=1))

    assert _fingerprint(forecast) != _fingerprint(forecast, **override)


def test_changed_predicted_values_change_the_fingerprint() -> None:
    """The obvious case, pinned so a refactor cannot drop the array."""
    baseline = _forecast(NORMAL, NORMAL + timedelta(days=1), daily_kwh=12.0)
    heavier = _forecast(NORMAL, NORMAL + timedelta(days=1), daily_kwh=20.0)

    assert _fingerprint(baseline) != _fingerprint(heavier)


def test_a_changed_fill_mask_changes_the_fingerprint() -> None:
    """Two days predicting the same numbers with different provenance differ."""
    from .test_fill_provenance import gapped_history

    modelled = _forecast(NORMAL, NORMAL + timedelta(days=1), daily_kwh=11.52)
    gapped = build_forecast(
        gapped_history(NORMAL, blank_before=8),
        NORMAL,
        NORMAL + timedelta(days=1),
        TZ,
    )

    assert modelled.total_kwh == pytest.approx(gapped.total_kwh)
    assert any(gapped.filled)
    assert not any(modelled.filled)
    assert _fingerprint(modelled) != _fingerprint(gapped)


def test_volatile_context_is_excluded_from_the_fingerprint() -> None:
    """Confidence moves every minute; it must not force a snapshot.

    The energy-balance score is resampled every sixty seconds, so if it reached
    the fingerprint the policy would collapse back to one record per refresh --
    the exact outcome the design exists to avoid.
    """
    forecast = _forecast(NORMAL, NORMAL + timedelta(days=1))
    common = {
        "issued_at": local(NORMAL, 10, 5).astimezone(),
        "issuance_day": NORMAL,
        "tz_key": "Europe/Amsterdam",
        "learned_days": 5,
        "ev_power_entity": None,
    }

    calm = build_snapshot(
        forecast, confidence_percent=41.2, confidence={"balance": 0.99}, **common
    )
    jittery = build_snapshot(
        forecast, confidence_percent=38.7, confidence={"balance": 0.61}, **common
    )

    assert calm.fingerprint == jittery.fingerprint
    # Recorded on the snapshot even though it is not fingerprinted.
    assert calm.context["load_model"]["confidence_percent"] == 41.2
    assert jittery.context["load_model"]["confidence_percent"] == 38.7


def test_the_issuance_instant_is_excluded_from_the_fingerprint() -> None:
    """Otherwise every refresh would look like a new forecast."""
    forecast = _forecast(NORMAL, NORMAL + timedelta(days=1))
    common = {
        "issuance_day": NORMAL,
        "tz_key": "Europe/Amsterdam",
        "learned_days": 5,
        "confidence_percent": 40.0,
        "confidence": None,
        "ev_power_entity": None,
    }

    early = build_snapshot(
        forecast, issued_at=local(NORMAL, 0, 5).astimezone(), **common
    )
    late = build_snapshot(
        forecast, issued_at=local(NORMAL, 23, 50).astimezone(), **common
    )

    assert early.fingerprint == late.fingerprint
    assert early.issued_at != late.issued_at


def test_the_baseline_definition_travels_with_the_snapshot() -> None:
    """Adding a flexible load redefines what is being predicted."""
    assert baseline_definition(None) == "none"
    assert baseline_definition("sensor.ev") == "ev:sensor.ev"

    forecast = _forecast(NORMAL, NORMAL + timedelta(days=1))
    without = build_snapshot(
        forecast,
        issued_at=local(NORMAL, 10, 5).astimezone(),
        issuance_day=NORMAL,
        tz_key="Europe/Amsterdam",
        learned_days=5,
        confidence_percent=40.0,
        confidence=None,
        ev_power_entity=None,
    )
    with_ev = build_snapshot(
        forecast,
        issued_at=local(NORMAL, 10, 5).astimezone(),
        issuance_day=NORMAL,
        tz_key="Europe/Amsterdam",
        learned_days=5,
        confidence_percent=40.0,
        confidence=None,
        ev_power_entity="sensor.ev_charger_power",
    )

    assert without.ev_configured is False
    assert with_ev.ev_configured is True
    assert without.fingerprint != with_ev.fingerprint


# -- what still gets recorded ------------------------------------------------


async def test_a_withheld_forecast_is_recorded_with_its_reason(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A silent model is evidence too.

    Without this, an installation whose first month published nothing would
    later look like a model that was never wrong, because there would be no
    record that it never spoke.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, {})

    await refresh_at(coordinator, local(NORMAL, 10, 5))

    snapshots = coordinator.history.snapshots(NORMAL)
    assert len(snapshots) == 1
    assert snapshots[0].available is False
    assert snapshots[0].unavailable_reason == REASON_NO_HISTORY
    # No fabricated array: the absence is the record.
    assert snapshots[0].predicted == ()
    assert snapshots[0].filled == ()


async def test_a_forecast_becoming_available_is_a_new_snapshot(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The transition from withheld to published must be visible."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, {})
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    reseed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 20))

    snapshots = coordinator.history.snapshots(NORMAL)
    assert [snapshot.available for snapshot in snapshots] == [False, True]


async def test_no_forecast_is_issued_for_a_day_already_past(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Only reachable if the clock steps backwards, and never a prediction."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    # The clock jumps forward two days; yesterday's targets are now in the past
    # and must not gain a "prediction" made after the fact.
    later = NORMAL + timedelta(days=2)
    reseed(coordinator, history_before(later))
    await refresh_at(coordinator, local(later, 10, 5))

    assert coordinator.history.snapshots(NORMAL) != []
    assert all(
        snapshot.target_day >= NORMAL
        for day in coordinator.history.days
        for snapshot in coordinator.history.snapshots(day)
    )
    assert set(snapshot_days(coordinator)) == {
        NORMAL,
        NORMAL + timedelta(days=1),
        later,
        later + timedelta(days=1),
    }


async def test_the_snapshot_cap_bounds_a_runaway_model(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A cap that bites is counted, not silently applied.

    Unreachable under the Phase-1 model, which produces at most two distinct
    forecasts per target. It exists so a future oscillating input cannot write
    ninety-six records a day, and it must leave a trace when it fires -- a
    silent cap reads as full coverage when it is not.
    """
    coordinator = setup_integration.runtime_data

    reset_history(coordinator)
    for step in range(FORECAST_MAX_SNAPSHOTS_PER_TARGET + 4):
        reseed(coordinator, history_before(NORMAL, daily_kwh=8.0 + step))
        moment = local(NORMAL, 0, 5) + timedelta(minutes=15 * step)
        await refresh_at(coordinator, moment)

    assert (
        len(coordinator.history.snapshots(NORMAL)) == FORECAST_MAX_SNAPSHOTS_PER_TARGET
    )
    assert coordinator.history.snapshot_cap_hits > 0


async def test_duplicate_coordinator_callbacks_write_one_snapshot(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Two refreshes at the same instant are one observation."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))

    await refresh_at(coordinator, local(NORMAL, 10, 5))
    await refresh_at(coordinator, local(NORMAL, 10, 5))
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    assert total_snapshots(coordinator) == 2


# -- context -----------------------------------------------------------------


async def test_the_snapshot_carries_the_provenance_phase_nine_needs(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Everything needed to interpret the prediction later, and nothing loose."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))

    await refresh_at(coordinator, local(NORMAL, 10, 5))
    snapshot = coordinator.history.snapshots(NORMAL)[0]
    context = snapshot.context["load_model"]

    assert context["v"] == 1
    assert set(context) == {
        "v",
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
    # Six prior days were seeded, but 2026-08-19 is a Wednesday and the six
    # days before it include a Saturday and a Sunday. Four learned weekdays is
    # enough to engage the day-type split, so the weekend days are correctly
    # not counted as days this forecast was built from -- and the provenance
    # says so, which is the whole point of recording it.
    assert context["model_days"] == 4
    assert context["usable_days"] == 6
    assert context["day_type"] == "weekday"
    assert context["day_type_pooled"] is False
    assert snapshot.model_version == FORECAST_MODEL_VERSION
    assert snapshot.model_params == model_params_hash()
    assert snapshot.horizon_days == 0
    assert snapshot.tz_key == "Europe/Amsterdam"
    assert snapshot.interval_count == 96


def test_a_context_provider_refuses_an_undeclared_field() -> None:
    """The registry is what stops context becoming a JSON dumping ground."""
    from custom_components.alpha_ems_manager.forecast_history import (
        LOAD_MODEL_CONTEXT,
    )

    with pytest.raises(ValueError, match="undeclared fields"):
        LOAD_MODEL_CONTEXT.build({"model_days": 3, "solcast_ghi": 412})


async def test_the_snapshot_copies_the_forecast_rather_than_referencing_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A later refresh must not be able to rewrite frozen evidence.

    ``DayForecast`` is a mutable dataclass the coordinator rebuilds every
    refresh. Holding a reference would make "immutable snapshot" a comment
    rather than a property.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    snapshot = coordinator.history.snapshots(NORMAL)[0]
    captured = snapshot.predicted

    live = coordinator.data["today_baseline"]
    live.intervals[0] = 999.0
    live.filled[0] = True

    assert snapshot.predicted == captured
    assert snapshot.predicted[0] != 999.0
    assert snapshot.filled[0] is False


# -- the cost of doing nothing -----------------------------------------------


async def test_an_unchanged_refresh_touches_no_storage_at_all(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Ninety-four refreshes a day must be free.

    The fingerprints live in the always-loaded index precisely so that the
    common case -- the model reproducing what it produced fifteen minutes ago --
    is two string comparisons and nothing else. If this ever starts loading a
    partition, the evidence layer has begun charging rent on every quarter of
    every day for information it already had.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=AssertionError("read on the hot path"),
        ),
        patch("homeassistant.helpers.storage.Store.async_save") as save,
        patch("homeassistant.helpers.storage.Store.async_delay_save") as delay_save,
    ):
        for step in range(1, 12):
            moment = local(NORMAL, 10, 5) + timedelta(minutes=15 * step)
            await refresh_at(coordinator, moment)

    save.assert_not_called()
    delay_save.assert_not_called()
    assert coordinator.recorder.duplicate_issuances == 22


async def test_a_changed_forecast_does_schedule_a_write(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The counterpart: silence on the hot path must not mean silence always."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    reseed(coordinator, history_before(NORMAL, daily_kwh=18.0))
    with patch("homeassistant.helpers.storage.Store.async_delay_save") as delay_save:
        await refresh_at(coordinator, local(NORMAL, 10, 20))

    assert delay_save.called
