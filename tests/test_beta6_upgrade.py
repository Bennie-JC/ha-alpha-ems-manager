"""Upgrading a live v1.0.0-beta.6 installation to v1.0.0-beta.7.

The rule this project will not bend: an update must never cost a day of learned
history, an entity id, a stored prediction, or a configuration value. Phase 3
adds five configuration keys, three of which have no default -- so an existing
installation upgrades into a state where the battery layer *cannot* work, and
everything about that has to be honest and reversible.

Nothing here is argued from how ``SourceConfig`` ought to behave. Every claim is
driven through the real config entry, the real stores and the real entities.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    ACTION_DISCHARGE,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_FRANK_ENTRY_ID,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_SIGN,
    CONF_HAS_PV,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_NAME,
    CONF_PV_POWER_ENTITY,
    CONF_USE_PV_FORECAST,
    CONFIG_ENTRY_VERSION,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_BATTERY_POWER_SIGN,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    DEFAULT_GRID_POWER_SIGN,
    DOMAIN,
    FORECAST_STORAGE_VERSION,
    REASON_MISSING_CAPACITY,
    STORAGE_VERSION,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.alpha_ems_manager.storage import LearningStore

from .conftest import (
    BATTERY_POWER,
    BATTERY_SOC,
    GRID_POWER,
    HOUSE_LOAD,
    PV_POWER,
    TEST_TIMEZONE,
)
from .forecast_helpers import NORMAL, frozen, history_before, local, refresh_at, seed

RECOMMENDATION = "sensor.alpha_ems_battery_recommendation"
PLANNED_POWER = "sensor.alpha_ems_planned_battery_power"
USABLE_ENERGY = "sensor.alpha_ems_usable_battery_energy"
BATTERY_ENTITIES = (RECOMMENDATION, PLANNED_POWER, USABLE_ENERGY)

PHASE_ONE_TWO_ENTITIES = (
    "sensor.alpha_ems_expected_house_load_today",
    "sensor.alpha_ems_expected_house_load_tomorrow",
    "sensor.alpha_ems_learning_confidence",
    "sensor.alpha_ems_learning_days",
    "sensor.alpha_ems_forecast_error_yesterday",
    "sensor.alpha_ems_forecast_error_7_days",
)

DAY_ONE = NORMAL
DAY_TWO = NORMAL + timedelta(days=1)


def beta6_config(frank_entry_id: str) -> dict:
    """Return the config-entry data exactly as v1.0.0-beta.6 wrote it.

    Note what is *absent*: none of the five battery-planning keys existed, so an
    upgraded entry has no capacity, no power limits, no minimum state of charge
    and no efficiency. That absence is the whole subject of this file.
    """
    return {
        CONF_NAME: "Alpha EMS",
        CONF_HOUSE_LOAD_ENTITY: HOUSE_LOAD,
        CONF_BATTERY_SOC_ENTITY: BATTERY_SOC,
        CONF_BATTERY_POWER_ENTITY: BATTERY_POWER,
        CONF_BATTERY_POWER_SIGN: DEFAULT_BATTERY_POWER_SIGN,
        CONF_HAS_PV: True,
        CONF_PV_POWER_ENTITY: PV_POWER,
        CONF_GRID_POWER_ENTITY: GRID_POWER,
        CONF_GRID_POWER_SIGN: DEFAULT_GRID_POWER_SIGN,
        CONF_FRANK_ENTRY_ID: frank_entry_id,
        CONF_USE_PV_FORECAST: False,
    }


def beta6_learning_document(entry_id: str) -> dict:
    """Return a learning document as beta.6 wrote it: no state-of-charge array."""
    return {
        "version": STORAGE_VERSION,
        # The minor version beta.6 wrote, one behind this release.
        "minor_version": 1,
        "key": f"alpha_ems_manager.{entry_id}.learning",
        "data": {
            "days": {
                (DAY_ONE - timedelta(days=offset)).isoformat(): {
                    "tz": TEST_TIMEZONE,
                    "n": 96,
                    "m": [0.125] * 96,
                }
                for offset in range(1, 7)
            },
            "balance": {"ok": 374, "total": 378},
            "last_finalized": "2026-08-18T21:45:00+00:00",
        },
    }


def beta6_forecast_documents(entry_id: str) -> dict:
    """Return an index and one partition, as beta.6's evidence layer wrote them."""
    index_key = f"alpha_ems_manager.{entry_id}.forecast_index"
    month_key = f"alpha_ems_manager.{entry_id}.forecast.2026-08"
    return {
        index_key: {
            "version": FORECAST_STORAGE_VERSION,
            "minor_version": 2,
            "key": index_key,
            "data": {
                "months": ["2026-08"],
                "days": {
                    DAY_ONE.isoformat(): {
                        "n": 96,
                        "fp": ["0123456789abcdef"],
                        "fin": "2026-08-20T00:05:00+00:00",
                        "sum": {
                            "n": 96,
                            "c": 96,
                            "ps": 12.0,
                            "as": 9.6,
                            "ae": 2.4,
                            "h": 0,
                            "fg": [],
                            "mr": 2,
                        },
                    }
                },
            },
        },
        month_key: {
            "version": FORECAST_STORAGE_VERSION,
            "minor_version": 2,
            "key": month_key,
            "data": {
                "days": {
                    DAY_ONE.isoformat(): {
                        "s": [
                            {
                                "iat": "2026-08-19T10:05:00+00:00",
                                "tz": TEST_TIMEZONE,
                                "n": 96,
                                "h": 0,
                                "av": True,
                                "ur": None,
                                "fp": "0123456789abcdef",
                                "mv": 1,
                                "mp": "aaaaaaaaaaaaaaaa",
                                "bd": "none",
                                "ctx": {},
                                "p": [0.125] * 96,
                                "f": "0" * 96,
                            }
                        ],
                        "o": {
                            "fin": "2026-08-20T00:05:00+00:00",
                            "tz": TEST_TIMEZONE,
                            "n": 96,
                            "a": [0.1] * 96,
                            "s": "0" * 96,
                            "ev": None,
                            "fl": [],
                        },
                    }
                }
            },
        },
    }


@pytest.fixture
async def upgraded(
    hass: HomeAssistant,
    hass_storage: dict,
    source_entities: None,
    frank_config_entry: MockConfigEntry,
) -> MockConfigEntry:
    """Set up an entry that was written by beta.6 and is being loaded by beta.7.

    Both stores are planted at the versions beta.6 wrote, and the config entry
    carries none of the new keys.
    """
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=beta6_config(frank_config_entry.entry_id),
        options={"future_option": "keep me"},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    hass_storage[f"alpha_ems_manager.{entry.entry_id}.learning"] = (
        beta6_learning_document(entry.entry_id)
    )
    hass_storage.update(beta6_forecast_documents(entry.entry_id))

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# -- the entry loads --------------------------------------------------------


async def test_a_beta6_entry_loads_without_migration(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """No config-entry version bump, so no migration and no remove-and-re-add.

    The new keys are additive with defaults supplied at read time, which is
    precisely why the schema version does not move.
    """
    assert upgraded.state.name == "LOADED"
    assert upgraded.version == CONFIG_ENTRY_VERSION == 2
    assert upgraded.runtime_data is not None


async def test_the_stored_configuration_is_untouched(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """Loading must not write anything into the entry, invented or otherwise."""
    assert upgraded.data == beta6_config(upgraded.data[CONF_FRANK_ENTRY_ID])
    for key in (
        CONF_BATTERY_CAPACITY_KWH,
        CONF_BATTERY_MAX_CHARGE_KW,
        CONF_BATTERY_MAX_DISCHARGE_KW,
        CONF_BATTERY_MIN_SOC_PERCENT,
    ):
        assert key not in upgraded.data
        assert key not in upgraded.options


async def test_the_absent_hardware_facts_read_as_absent(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """And the two that legitimately have defaults get them.

    The distinction matters: a defaulted minimum state of charge is a documented
    choice, while a defaulted *capacity* would be an invented hardware property
    and would silently produce a plan the inverter could not execute.
    """
    config = upgraded.runtime_data.config

    assert config.battery_capacity_kwh is None
    assert config.battery_max_charge_kw is None
    assert config.battery_max_discharge_kw is None
    assert config.battery_min_soc_percent == DEFAULT_BATTERY_MIN_SOC_PERCENT
    assert (
        config.battery_round_trip_efficiency_percent
        == DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT
    )


async def test_every_entity_id_and_unique_id_survives(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """Six existing rows keep their ids exactly; three new ones appear."""
    registry = er.async_get(hass)
    ours = {
        entity.entity_id: entity.unique_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN and entity.config_entry_id == upgraded.entry_id
    }

    for entity_id in PHASE_ONE_TWO_ENTITIES:
        assert entity_id in ours, entity_id
        assert ours[entity_id].startswith(f"{upgraded.entry_id}_")
    for entity_id in BATTERY_ENTITIES:
        assert entity_id in ours, entity_id
    assert len(ours) == 11
    assert not any("_2" in entity_id for entity_id in ours)


# -- Phase 1 and Phase 2 are untouched --------------------------------------


async def test_the_learned_history_is_read_back_intact(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """Six days of measurements, and a state-of-charge array that is simply absent."""
    store = upgraded.runtime_data.store

    assert len(store.days) == 6
    for record in store.days.values():
        assert record.measured == [0.125] * 96
        assert record.interval_count == 96
        # The new array exists in memory, sized and empty -- never zero-filled.
        assert record.soc == [None] * 96
        assert record.soc_sample_count == 0
        # And it changes nothing about whether the day is learnable.
        assert record.is_learned is True
        assert record.completeness == 1.0
        assert record.baseline_total_kwh == pytest.approx(12.0)
    assert store.balance.ok_samples == 374
    assert store.balance.total_samples == 378


async def test_the_forecast_evidence_is_read_back_intact(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """Every prediction and every matched actual beta.6 wrote comes back."""
    history = upgraded.runtime_data.history
    await history.async_ensure_days([DAY_ONE])

    assert history.corrupt is False
    assert history.reset_by_migration is False
    snapshot = history.snapshots(DAY_ONE)[0]
    assert snapshot.fingerprint == "0123456789abcdef"
    assert snapshot.predicted == (0.125,) * 96
    assert snapshot.issued_at == datetime(2026, 8, 19, 10, 5, tzinfo=UTC)
    outcome = history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.actual == (0.1,) * 96
    assert outcome.flags == ()


async def test_phase_one_and_two_still_publish_their_figures(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """The six existing sensors work exactly as they did, with no battery config."""
    coordinator = upgraded.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    assert hass.states.get("sensor.alpha_ems_learning_days").state == "6"
    assert float(hass.states.get("sensor.alpha_ems_learning_confidence").state) > 0.0
    # Today is the *adapted* figure: nothing has been measured today, and half
    # the day has elapsed, so it is the remaining forty-eight intervals at
    # 0.125 kWh. Tomorrow is a whole day. Both are pinned because a battery
    # layer that disturbed the forecast would show up here first.
    today = hass.states.get("sensor.alpha_ems_expected_house_load_today")
    assert float(today.state) == pytest.approx(6.0, abs=0.01)
    tomorrow = hass.states.get("sensor.alpha_ems_expected_house_load_tomorrow")
    assert float(tomorrow.state) == pytest.approx(12.0, abs=0.01)


async def test_the_forecast_error_sensors_still_score_a_stored_day(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """The Phase-2 scoring path is unaffected by an unconfigured battery.

    Driven from the planted beta.6 summary row: predicted 12.0, measured 9.6, so
    the signed error is +2.4 kWh and it must still be published.
    """
    coordinator = upgraded.runtime_data
    history = dict(coordinator.store.days)
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    yesterday = hass.states.get("sensor.alpha_ems_forecast_error_yesterday")
    assert float(yesterday.state) == 2.4
    assert yesterday.attributes["intervals_compared"] == 96
    # And the learned history was not disturbed by reading it.
    assert set(coordinator.store.days) >= set(history)


async def test_no_unknown_option_key_is_lost(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """A key a future release adds must survive an unrelated edit today."""
    from .test_config_flow import battery_options_payload, open_options

    assert upgraded.options["future_option"] == "keep me"

    result = await open_options(hass, upgraded.entry_id, "battery")
    await hass.config_entries.options.async_configure(
        result["flow_id"], battery_options_payload()
    )
    await hass.async_block_till_done()

    assert upgraded.options["future_option"] == "keep me"


# -- Phase 3 declines, honestly ---------------------------------------------


async def test_the_battery_entities_read_unknown_and_say_why(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """Missing hardware facts, so no plan -- and never a fabricated zero."""
    coordinator = upgraded.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    for entity_id in BATTERY_ENTITIES:
        assert hass.states.get(entity_id).state == "unknown", entity_id

    attributes = hass.states.get(RECOMMENDATION).attributes
    assert attributes["reason"] == REASON_MISSING_CAPACITY
    assert attributes["usable_energy_kwh"] is None
    # The default minimum is still reported, so the user can see what it would be.
    assert attributes["configured_min_soc_percent"] == DEFAULT_BATTERY_MIN_SOC_PERCENT


async def test_diagnostics_tells_the_user_what_to_enter(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """A missing setting is the user's to fill in, and must be distinguishable
    from a fault."""
    coordinator = upgraded.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    with frozen(local(DAY_ONE, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, upgraded)

    plan = payload["battery_plan"]
    assert plan["available"] is False
    assert plan["hardware_configured"] is False
    assert plan["unavailable_reason"] == REASON_MISSING_CAPACITY
    assert plan["inputs"]["capacity_kwh"] is None


# -- and then becomes operational -------------------------------------------


async def test_entering_the_values_through_options_makes_phase_three_work(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """The whole point of the upgrade path: no reinstall, no lost history.

    The learned history and the forecast evidence are checked on the far side,
    because an options change reloads the entry and that reload is exactly where
    history could be lost.
    """
    from .test_config_flow import battery_options_payload, open_options

    coordinator = upgraded.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    assert hass.states.get(RECOMMENDATION).state == "unknown"
    await coordinator.async_shutdown_store()

    result = await open_options(hass, upgraded.entry_id, "battery")
    assert result["step_id"] == "battery"
    with frozen(local(DAY_ONE, 12, 10)):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            battery_options_payload(**{CONF_BATTERY_MIN_SOC_PERCENT: 25.0}),
        )
        await hass.async_block_till_done()

    reloaded = upgraded.runtime_data
    assert reloaded.config.battery_capacity_kwh == 10.0
    assert reloaded.config.battery_min_soc_percent == 25.0

    seed(reloaded, history_before(DAY_ONE))
    await refresh_at(reloaded, local(DAY_ONE, 12, 15))

    # 55 % of 10 kWh above a 25 % floor is 3 kWh DC.
    assert hass.states.get(RECOMMENDATION).state == ACTION_DISCHARGE
    assert float(hass.states.get(USABLE_ENERGY).state) == pytest.approx(2.85, abs=0.01)
    assert (
        hass.states.get(USABLE_ENERGY).attributes["configured_min_soc_percent"] == 25.0
    )

    # Learned history survived the reload.
    store = LearningStore(hass, upgraded.entry_id)
    await store.async_load(TEST_TIMEZONE)
    assert len(store.days) == 6


async def test_a_partially_configured_battery_still_declines(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """Entering the capacity but not the power limits is not enough, and says so.

    A power limit inferred from a capacity at an assumed C-rate would be an
    invented hardware property, so the honest answer is to keep declining and
    name the field that is still missing.
    """
    from .test_config_flow import open_options

    result = await open_options(hass, upgraded.entry_id, "battery")
    with frozen(local(DAY_ONE, 12, 10)):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_BATTERY_CAPACITY_KWH: 10.0,
                CONF_BATTERY_MIN_SOC_PERCENT: 20.0,
                CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: 90.0,
            },
        )
        await hass.async_block_till_done()

    reloaded = upgraded.runtime_data
    assert reloaded.config.battery_capacity_kwh == 10.0
    assert reloaded.config.battery_max_charge_kw is None

    seed(reloaded, history_before(DAY_ONE))
    await refresh_at(reloaded, local(DAY_ONE, 12, 15))

    assert hass.states.get(RECOMMENDATION).state == "unknown"
    assert (
        hass.states.get(RECOMMENDATION).attributes["reason"] == "missing_power_limits"
    )


async def test_the_minimum_can_be_changed_without_touching_history(
    hass: HomeAssistant, upgraded: MockConfigEntry
) -> None:
    """A reserve edit is a Phase-3 recalculation and nothing else."""
    from .test_config_flow import battery_options_payload, open_options

    coordinator = upgraded.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    await coordinator.async_shutdown_store()

    learning_before = {
        day: list(record.measured) for day, record in coordinator.store.days.items()
    }

    result = await open_options(hass, upgraded.entry_id, "battery")
    with frozen(local(DAY_ONE, 12, 10)):
        await hass.config_entries.options.async_configure(
            result["flow_id"], battery_options_payload()
        )
        await hass.async_block_till_done()

    store = LearningStore(hass, upgraded.entry_id)
    await store.async_load(TEST_TIMEZONE)
    assert {
        day: list(record.measured) for day, record in store.days.items()
    } == learning_before

    history = upgraded.runtime_data.history
    await history.async_ensure_days([DAY_ONE])
    assert history.snapshots(DAY_ONE)[0].fingerprint == "0123456789abcdef"


async def test_the_storage_minor_version_moved_and_the_major_did_not(
    hass: HomeAssistant, upgraded: MockConfigEntry, hass_storage: dict
) -> None:
    """A minor bump reads every earlier document and rewrites it; nothing migrates."""
    from custom_components.alpha_ems_manager.const import STORAGE_MINOR_VERSION

    assert STORAGE_VERSION == 2
    assert STORAGE_MINOR_VERSION == 2

    coordinator = upgraded.runtime_data
    assert coordinator.store.reset_by_migration is False
    await coordinator.async_shutdown_store()

    document = hass_storage[f"alpha_ems_manager.{upgraded.entry_id}.learning"]
    assert document["version"] == 2
    assert document["minor_version"] == 2
    # The six days beta.6 wrote are all still there.
    assert len(document["data"]["days"]) == 6


def test_the_new_configuration_keys_do_not_collide_with_the_legacy_model() -> None:
    """The v1 model had a ``battery_capacity_kwh_entity``; this must not reuse it."""
    from custom_components.alpha_ems_manager import const

    from .test_legacy_config import LEGACY_DATA

    new_keys = {
        value
        for name, value in vars(const).items()
        if name.startswith("CONF_") and isinstance(value, str)
    }
    assert new_keys.isdisjoint(LEGACY_DATA)
    assert CONF_BATTERY_CAPACITY_KWH == "battery_capacity_kwh"
    assert "battery_capacity_kwh_entity" in LEGACY_DATA
