"""Upgrading a live v1.0.0-beta.5 installation to v1.0.0-beta.6.

The rule this project will not bend: an update must never cost a day of learned
history, an entity id, or a stored prediction. beta.6 changes a matching rule
and adds one optional field to the summary rows, so the whole upgrade surface is
here -- read the documents beta.5 wrote, keep them, and re-derive only the
verdicts the corrected rule actually changes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONFIG_ENTRY_VERSION,
    FLAG_DEFINITION_CHANGED,
    FORECAST_STORAGE_MINOR_VERSION,
    FORECAST_STORAGE_VERSION,
    STORAGE_VERSION,
)
from custom_components.alpha_ems_manager.history_store import (
    ForecastHistoryStore,
    month_key,
)
from custom_components.alpha_ems_manager.metrics import matcher_version
from custom_components.alpha_ems_manager.storage import LearningStore

from .forecast_helpers import NORMAL

DAY_ONE = NORMAL
DAY_TWO = NORMAL + timedelta(days=1)


def index_key(entry: MockConfigEntry) -> str:
    """Return the storage key of the forecast index document."""
    return f"alpha_ems_manager.{entry.entry_id}.forecast_index"


def month_store_key(entry: MockConfigEntry, day: date) -> str:
    """Return the storage key of the partition holding ``day``."""
    return f"alpha_ems_manager.{entry.entry_id}.forecast.{month_key(day)}"


def learning_key(entry: MockConfigEntry) -> str:
    """Return the storage key of the learning history document."""
    return f"alpha_ems_manager.{entry.entry_id}.learning"


def beta5_index() -> dict:
    """Return an index document exactly as v1.0.0-beta.5 wrote one.

    Note what is *absent*: the summary rows carry no ``mr`` field, because the
    field did not exist. That absence is the signal beta.6 reads.
    """
    return {
        "months": ["2026-08"],
        "days": {
            DAY_ONE.isoformat(): {
                "n": 96,
                "fp": ["0123456789abcdef"],
                "fin": "2026-08-20T00:05:00+00:00",
                "sum": {
                    "n": 96,
                    "c": 0,
                    "fg": [FLAG_DEFINITION_CHANGED],
                },
            },
            DAY_TWO.isoformat(): {"n": 96, "fp": ["fedcba9876543210"]},
        },
    }


def beta5_partition() -> dict:
    """Return a month partition exactly as v1.0.0-beta.5 wrote one."""
    return {
        "days": {
            DAY_ONE.isoformat(): {
                "s": [
                    {
                        "iat": "2026-08-19T10:05:00+00:00",
                        "tz": "Europe/Amsterdam",
                        "n": 96,
                        "h": 0,
                        "av": True,
                        "ur": None,
                        "fp": "0123456789abcdef",
                        "mv": 1,
                        "mp": "aaaaaaaaaaaaaaaa",
                        "bd": "ev:sensor.ev_charger_power",
                        "ctx": {
                            "load_model": {
                                "v": 1,
                                "model_days": 4,
                                "usable_days": 6,
                                "learned_days": 2,
                                "day_type": "weekday",
                                "day_type_pooled": False,
                                "windows_used": [7, 30, 90, 180, 365],
                                "modelled_intervals": 96,
                                "filled_intervals": 0,
                                "confidence_percent": 5.4,
                                "confidence": None,
                            }
                        },
                        "p": [0.125] * 96,
                        "f": "0" * 96,
                    }
                ],
                "o": {
                    "fin": "2026-08-20T00:05:00+00:00",
                    "tz": "Europe/Amsterdam",
                    "n": 96,
                    "a": [0.1] * 94 + [None, None],
                    "s": "0" * 94 + "11",
                    "ev": 0.0,
                    "fl": [FLAG_DEFINITION_CHANGED],
                },
            },
            DAY_TWO.isoformat(): {
                "s": [
                    {
                        "iat": "2026-08-20T00:05:00+00:00",
                        "tz": "Europe/Amsterdam",
                        "n": 96,
                        "h": 0,
                        "av": True,
                        "ur": None,
                        "fp": "fedcba9876543210",
                        "mv": 1,
                        "mp": "aaaaaaaaaaaaaaaa",
                        "bd": "ev:sensor.ev_charger_power",
                        "ctx": {},
                        "p": [0.12] * 96,
                        "f": "0" * 96,
                    }
                ]
            },
        }
    }


def plant_beta5_documents(hass_storage: dict, entry: MockConfigEntry) -> None:
    """Write beta.5 documents into the storage layer under its own versions."""
    hass_storage[index_key(entry)] = {
        "version": FORECAST_STORAGE_VERSION,
        # The minor version beta.5 wrote, which is one behind this release.
        "minor_version": 1,
        "key": index_key(entry),
        "data": beta5_index(),
    }
    hass_storage[month_store_key(entry, DAY_ONE)] = {
        "version": FORECAST_STORAGE_VERSION,
        "minor_version": 1,
        "key": month_store_key(entry, DAY_ONE),
        "data": beta5_partition(),
    }


async def test_a_beta5_forecast_document_is_read_not_discarded(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_storage: dict
) -> None:
    """A minor-version bump must never cost a stored prediction.

    The major version is what decides readability. beta.6 raises only the minor,
    so every snapshot, every actual and every summary row comes back exactly as
    written -- and ``reset_by_schema_migration`` stays false, because nothing was
    reset.
    """
    plant_beta5_documents(hass_storage, setup_integration)

    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    await store.async_load()
    await store.async_ensure_days([DAY_ONE, DAY_TWO])

    assert store.corrupt is False
    assert store.reset_by_migration is False
    assert sorted(store.days) == [DAY_ONE, DAY_TWO]
    assert store.months == {"2026-08"}

    snapshot = store.snapshots(DAY_ONE)[0]
    assert snapshot.fingerprint == "0123456789abcdef"
    assert snapshot.predicted == (0.125,) * 96
    assert snapshot.issued_at == datetime(2026, 8, 19, 10, 5, tzinfo=UTC)
    assert snapshot.baseline_definition == "ev:sensor.ev_charger_power"
    assert snapshot.context["load_model"]["model_days"] == 4

    outcome = store.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.actual[:94] == (0.1,) * 94
    assert outcome.actual[94:] == (None, None)
    assert outcome.flags == (FLAG_DEFINITION_CHANGED,)

    # The rows beta.5 wrote are recognised as first-generation matches.
    assert matcher_version(store.days[DAY_ONE].summary) == 1

    # A beta.5 document carries no photovoltaic, price, reserve or economic
    # evidence, and reading it must not invent any of them. The minor version has
    # moved on five times since -- it is pinned here so a *major* bump, which
    # would decide the document is unreadable, cannot slip in as a minor one.
    assert FORECAST_STORAGE_VERSION == 1
    assert FORECAST_STORAGE_MINOR_VERSION == 6
    assert store.days[DAY_ONE].pv_fingerprints == []
    assert store.days[DAY_ONE].price_fingerprints == []
    assert store.price_snapshots(DAY_ONE) == []
    assert store.days[DAY_ONE].reserve_fingerprints == []
    assert store.reserve_snapshots(DAY_ONE) == []
    assert store.days[DAY_ONE].economic_fingerprints == []
    assert store.economic_snapshots(DAY_ONE) == []


async def test_a_beta5_installation_keeps_every_entity_and_unique_id(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No remove-and-re-add, no renamed entity, no new one."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entities = {
        entity.entity_id: entity.unique_id
        for entity in registry.entities.values()
        if entity.platform == "alpha_ems_manager"
    }
    entry_id = setup_integration.entry_id
    assert entities == {
        "sensor.alpha_ems_expected_house_load_today": (
            f"{entry_id}_expected_house_load_today"
        ),
        "sensor.alpha_ems_expected_house_load_tomorrow": (
            f"{entry_id}_expected_house_load_tomorrow"
        ),
        "sensor.alpha_ems_learning_confidence": f"{entry_id}_learning_confidence",
        "sensor.alpha_ems_learning_days": f"{entry_id}_learning_days",
        "sensor.alpha_ems_forecast_error_yesterday": (
            f"{entry_id}_forecast_error_yesterday"
        ),
        "sensor.alpha_ems_forecast_error_7_days": f"{entry_id}_forecast_error_7d",
        "sensor.alpha_ems_battery_recommendation": (
            f"{entry_id}_battery_recommendation"
        ),
        "sensor.alpha_ems_planned_battery_power": f"{entry_id}_battery_planned_power",
        "sensor.alpha_ems_usable_battery_energy": (f"{entry_id}_battery_usable_energy"),
        "sensor.alpha_ems_dynamic_battery_reserve": (
            f"{entry_id}_dynamic_battery_reserve"
        ),
        "sensor.alpha_ems_economic_action": f"{entry_id}_economic_action",
        "sensor.alpha_ems_control_state": f"{entry_id}_control_state",
        "select.alpha_ems_control_mode": f"{entry_id}_control_mode",
    }


async def test_no_version_a_user_upgrades_through_changes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The config entry and the learning document are untouched by beta.6.

    Only the forecast history's *minor* version moves. The config-entry version
    deciding whether an entry can be loaded at all, and the learning history's
    own schema version, are both frozen -- so an upgrade cannot ask a user to
    remove and re-add, and cannot discard a day of measurements.
    """
    assert CONFIG_ENTRY_VERSION == 2
    assert STORAGE_VERSION == 2
    assert FORECAST_STORAGE_VERSION == 1
    assert setup_integration.version == CONFIG_ENTRY_VERSION


async def test_learned_history_written_by_beta5_survives(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_storage: dict
) -> None:
    """The irreplaceable half, read back interval for interval."""
    entry = setup_integration
    hass_storage[learning_key(entry)] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": learning_key(entry),
        "data": {
            "days": {
                DAY_ONE.isoformat(): {
                    "tz": "Europe/Amsterdam",
                    "n": 96,
                    "m": [0.1] * 96,
                    "e": [0.0] * 96,
                    "x": [1] * 96,
                }
            },
            "balance": {"ok": 374, "total": 378},
            "last_finalized": "2026-08-19T21:45:00+00:00",
        },
    }

    store = LearningStore(hass, entry.entry_id)
    await store.async_load("Europe/Amsterdam")

    record = store.days[DAY_ONE]
    assert record.interval_count == 96
    assert record.measured == [0.1] * 96
    assert record.ev_expected == [True] * 96
    assert record.baseline_total_kwh == 9.6
    assert store.balance.ok_samples == 374
    assert store.balance.total_samples == 378
