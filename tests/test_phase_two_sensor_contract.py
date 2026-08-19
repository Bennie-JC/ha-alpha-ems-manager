"""The published contract of the two Phase-2 sensors, and its agreement with
diagnostics.

``test_entity_contract`` freezes the registry metadata of all six entities. This
file covers the part a frozen table cannot: that every attribute is either a
real figure or ``None``, that the numbers a maintainer downloads are the numbers
the user is looking at, and that neither survives a restart as something stale.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FORECAST_MATCHER_VERSION,
    FORECAST_MIN_INTERVALS_FOR_METRIC,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .forecast_helpers import (
    NORMAL,
    frozen,
    history_before,
    local,
    refresh_at,
    reseed,
    seed,
)
from .synthetic import flat_day

pytestmark = pytest.mark.usefixtures("setup_integration")

DAY_ONE = NORMAL
DAY_TWO = NORMAL + timedelta(days=1)
DAY_THREE = NORMAL + timedelta(days=2)

ERROR_YESTERDAY = "sensor.alpha_ems_forecast_error_yesterday"
ERROR_WINDOW = "sensor.alpha_ems_forecast_error_7_days"

#: Every attribute either sensor is allowed to publish. A new one has to be
#: added here deliberately, which is what stops a debugging field from becoming
#: part of the public surface by accident.
YESTERDAY_ATTRIBUTES = {
    "absolute_error_kwh",
    "error_percent",
    "predicted_kwh",
    "actual_kwh",
    "mae_kwh_per_interval",
    "intervals_compared",
    "intervals_in_day",
    "horizon_days",
    "comparison_basis",
}
WINDOW_ATTRIBUTES = {
    "window_days",
    "days_compared",
    "intervals_compared",
    "mae_kwh_per_interval",
    "bias_kwh_per_interval",
    "predicted_kwh",
    "actual_kwh",
    "comparison_basis",
}
#: Attributes Home Assistant itself adds.
CORE_ATTRIBUTES = {
    "state_class",
    "unit_of_measurement",
    "icon",
    "friendly_name",
    "device_class",
}


async def drive_two_days(coordinator) -> None:
    """Two consecutive scored days, so both sensors carry real numbers."""
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    first = flat_day(DAY_ONE, 9.6)
    reseed(coordinator, {**base, DAY_ONE: first})
    await refresh_at(coordinator, local(DAY_TWO, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: first, DAY_TWO: flat_day(DAY_TWO, 9.6)})
    await refresh_at(coordinator, local(DAY_THREE, 0, 5))


def attributes_of(hass: HomeAssistant, entity_id: str) -> dict:
    """Return one entity's attributes, asserting the entity exists."""
    state = hass.states.get(entity_id)
    assert state is not None
    return dict(state.attributes)


# -- the attribute surface is closed and never fabricated --------------------


async def test_neither_sensor_publishes_an_undeclared_attribute(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Whether or not anything has been scored."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    assert set(attributes_of(hass, ERROR_YESTERDAY)) - CORE_ATTRIBUTES <= (
        YESTERDAY_ATTRIBUTES
    )
    assert set(attributes_of(hass, ERROR_WINDOW)) - CORE_ATTRIBUTES <= WINDOW_ATTRIBUTES

    await drive_two_days(coordinator)

    assert (
        set(attributes_of(hass, ERROR_YESTERDAY)) - CORE_ATTRIBUTES
        == YESTERDAY_ATTRIBUTES
    )
    assert set(attributes_of(hass, ERROR_WINDOW)) - CORE_ATTRIBUTES == WINDOW_ATTRIBUTES


async def test_an_unscored_installation_publishes_no_number_anywhere(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Not in the state, and not in a single attribute either.

    An attribute is as public as a state. Publishing ``predicted_kwh: 0.0``
    while the state reads ``unknown`` hands a template exactly the fabricated
    zero the state refused to give it.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    yesterday = attributes_of(hass, ERROR_YESTERDAY)
    assert hass.states.get(ERROR_YESTERDAY).state == "unknown"
    assert yesterday["intervals_compared"] is None
    assert yesterday["comparison_basis"].startswith("baseline house load")
    numeric = {
        key: value
        for key, value in yesterday.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    assert numeric == {}

    window = attributes_of(hass, ERROR_WINDOW)
    assert hass.states.get(ERROR_WINDOW).state == "unknown"
    assert window["days_compared"] == 0
    assert window["intervals_compared"] == 0
    assert window["predicted_kwh"] is None
    assert window["actual_kwh"] is None
    assert window["mae_kwh_per_interval"] is None
    assert window["bias_kwh_per_interval"] is None
    # The one number that is always true: the window is seven days wide.
    assert window["window_days"] == 7


async def test_the_two_sensors_never_contradict_each_other(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A day counted by one must be counted by the other."""
    coordinator = setup_integration.runtime_data
    await drive_two_days(coordinator)

    yesterday = attributes_of(hass, ERROR_YESTERDAY)
    window = attributes_of(hass, ERROR_WINDOW)

    assert window["days_compared"] == 2
    assert window["intervals_compared"] == 192
    # Yesterday is one of the days in the window, so its compared count cannot
    # exceed the window's, and its energies are a subset of the window's.
    assert yesterday["intervals_compared"] <= window["intervals_compared"]
    assert yesterday["predicted_kwh"] <= window["predicted_kwh"]
    assert yesterday["actual_kwh"] <= window["actual_kwh"]
    # Both sensors describe the same quantity, in the same words.
    assert yesterday["comparison_basis"] == window["comparison_basis"]


# -- diagnostics tells the same story ---------------------------------------


async def test_diagnostics_reports_the_published_sensor_values(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The download and the dashboard must not disagree.

    The rolling statistics in diagnostics are deliberately ungated -- a
    maintainer wants the figure whatever its sample size -- so the payload also
    carries what the entities actually publish, and the threshold that separates
    the two.
    """
    coordinator = setup_integration.runtime_data
    await drive_two_days(coordinator)

    # Diagnostics reads the clock for its own "today", so the download is taken
    # at the same instant as the last refresh -- exactly as it would be live.
    with frozen(local(DAY_THREE, 0, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    published = payload["forecast_history"]["quality"]["published"]

    assert (
        published["minimum_intervals_for_metric"] == FORECAST_MIN_INTERVALS_FOR_METRIC
    )
    assert published["forecast_error_yesterday"]["signed_error_kwh"] == float(
        hass.states.get(ERROR_YESTERDAY).state
    )
    assert published["forecast_error_window"]["wape_percent"] == float(
        hass.states.get(ERROR_WINDOW).state
    )
    assert (
        published["forecast_error_window"]["days_compared"]
        == attributes_of(hass, ERROR_WINDOW)["days_compared"]
    )
    assert (
        published["forecast_error_window"]["intervals_compared"]
        == attributes_of(hass, ERROR_WINDOW)["intervals_compared"]
    )
    # And the ungated statistic is present beside it, for the same window.
    rolling = payload["forecast_history"]["quality"]["rolling"]["7_days"]
    assert rolling["days_compared"] == 2
    assert rolling["wape_percent"] == 22.5


async def test_diagnostics_explains_a_gated_window_rather_than_contradicting_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One scored day: a real statistic in the payload, no rate on the entity."""
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    with frozen(local(DAY_TWO, 0, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    quality = payload["forecast_history"]["quality"]

    assert quality["rolling"]["7_days"]["wape_percent"] == 25.0
    assert quality["published"]["forecast_error_window"]["wape_percent"] is None
    assert quality["published"]["forecast_error_window"]["intervals_compared"] == 96
    assert hass.states.get(ERROR_WINDOW).state == "unknown"
    assert "withholds its rate" in quality["published"]["gate"]


async def test_diagnostics_names_the_matching_generation(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Two records matched under different rules must never be pooled blindly."""
    coordinator = setup_integration.runtime_data
    await drive_two_days(coordinator)

    with frozen(local(DAY_THREE, 0, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    provenance = payload["forecast_history"]["provenance"]
    assert provenance["matcher_version"] == FORECAST_MATCHER_VERSION
    for day in (DAY_ONE, DAY_TWO):
        assert coordinator.history.days[day].summary["mr"] == FORECAST_MATCHER_VERSION


async def test_an_excluded_day_is_reported_with_the_facts_that_excluded_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A flag count says a day was dropped, not why it was dropped.

    Every fact behind each flag is already in the record, so reporting them
    beside it is the difference between chasing a one-line counter and reading
    an answer.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    moved = flat_day(DAY_ONE, 12.0)
    moved.tz_key = "America/New_York"
    reseed(coordinator, {**base, DAY_ONE: moved})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    with frozen(local(DAY_TWO, 0, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    matching = payload["forecast_history"]["matching"]

    assert matching["excluded_day_flags"] == {"timezone_changed": 1}
    assert matching["excluded_days_total"] == 1
    assert matching["excluded_days_reported"] == 1
    entry = matching["excluded_days"][0]
    assert entry["target_day"] == DAY_ONE.isoformat()
    assert entry["flags"] == ["timezone_changed"]
    assert entry["record_timezone"] == "America/New_York"
    assert entry["snapshot_timezones"] == ["Europe/Amsterdam"]
    assert entry["intervals_in_day"] == 96
    assert entry["snapshot_baseline_definitions"] == ["none"]
    assert entry["snapshot_interval_counts"] == [96]


async def test_a_healthy_installation_reports_no_excluded_days(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The list is empty rather than absent, so its absence means something."""
    coordinator = setup_integration.runtime_data
    await drive_two_days(coordinator)

    with frozen(local(DAY_THREE, 0, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    matching = payload["forecast_history"]["matching"]
    assert matching["excluded_day_flags"] == {}
    assert matching["excluded_days"] == []
    assert matching["excluded_days_total"] == 0
    assert matching["restated_last_refresh"] == []


async def test_the_whole_payload_is_still_json_serialisable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Home Assistant serves this over HTTP; a stray date object breaks it."""
    import json

    coordinator = setup_integration.runtime_data
    await drive_two_days(coordinator)
    with frozen(local(DAY_THREE, 0, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    encoded = json.dumps(payload, allow_nan=False)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded


# -- restart persistence -----------------------------------------------------


async def test_both_sensors_come_back_with_the_same_values_after_a_restart(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The evidence is on disk, so the published figures are reproducible.

    Nothing about either sensor is cached across the restart: both are
    recomputed from the reloaded index. Equal values therefore prove the
    document round-tripped, not that a value was carried over.
    """
    coordinator = setup_integration.runtime_data
    await drive_two_days(coordinator)
    before_yesterday = hass.states.get(ERROR_YESTERDAY).state
    before_window = hass.states.get(ERROR_WINDOW).state
    before_attributes = attributes_of(hass, ERROR_WINDOW)
    history = dict(coordinator.store.days)
    await coordinator.async_shutdown_store()

    with frozen(local(DAY_THREE, 0, 15)):
        assert await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()
    restarted = setup_integration.runtime_data
    reseed(restarted, history)
    await refresh_at(restarted, local(DAY_THREE, 0, 20))

    assert hass.states.get(ERROR_YESTERDAY).state == before_yesterday
    assert hass.states.get(ERROR_WINDOW).state == before_window
    after = attributes_of(hass, ERROR_WINDOW)
    for key in ("days_compared", "intervals_compared", "predicted_kwh", "actual_kwh"):
        assert after[key] == before_attributes[key], key
    # No day was matched a second time to produce them.
    assert restarted.last_record.finalized == ()
    assert restarted.last_record.restated == ()


async def test_a_refresh_failure_leaves_no_stale_forecast_error_behind(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A sensor that keeps showing yesterday's figure after a fault is lying.

    Home Assistant marks a coordinator entity unavailable when a refresh fails,
    which is the honest outcome: the figure is not wrong, it is unverified.
    """
    coordinator = setup_integration.runtime_data
    await drive_two_days(coordinator)
    assert hass.states.get(ERROR_YESTERDAY).state != "unknown"

    from unittest.mock import patch

    with patch.object(
        coordinator, "_async_update_data", side_effect=RuntimeError("source gone")
    ):
        await refresh_at(coordinator, local(DAY_THREE, 0, 20))

    for entity_id in (ERROR_YESTERDAY, ERROR_WINDOW):
        assert hass.states.get(entity_id).state == "unavailable"
