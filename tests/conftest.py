"""Shared fixtures for the Alpha EMS Manager test suite."""

from __future__ import annotations

import asyncio
import sys

# On Windows the default ProactorEventLoop uses a socket for its self-pipe,
# which pytest_socket (enabled by pytest-homeassistant-custom-component) blocks.
# Force the selector loop before any loop is created so the HA test harness can
# run locally on Windows. No effect on Linux/macOS (CI).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_socket
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.alpha_ems_manager.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DAILY_HOUSE_LOAD_ENTITY,
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
    DOMAIN_FRANK,
    DOMAIN_SOLCAST,
    FRANK_KEY_CURRENT_PRICE,
    FRANK_KEY_CURRENT_RETURN_PRICE,
    FRANK_KEY_PRICES_TODAY,
    FRANK_KEY_PRICES_TOMORROW,
    FRANK_KEY_TOMORROW_AVAILABLE,
    SOLCAST_DOMAIN,
    SOLCAST_SERVICE_DIAGNOSTIC,
    SOLCAST_SERVICE_QUERY_FORECAST,
)

from .frank_capture import SYNTHETIC_FEED_IN_ADJUSTMENT, synthetic_day

# On Windows every asyncio event loop creates an ``AF_INET`` socket for its
# self-pipe, which pytest_socket blocks. On Linux/macOS CI the self-pipe uses an
# allowed unix socket, so blocking is fine there. Neutralise the blocker for
# local Windows runs only.
if sys.platform == "win32":
    pytest_socket.disable_socket = lambda *args, **kwargs: None


TEST_TIMEZONE = "Europe/Amsterdam"
TZ = ZoneInfo(TEST_TIMEZONE)

#: A Monday, well inside a quarter and past the morning ramp, so weekday
#: behaviour and mid-day adaptation are both exercisable from the default clock.
SETUP_NOW = datetime(2026, 8, 17, 10, 7, 0, tzinfo=TZ)

#: Dutch DST transition dates. A spring-forward day contains 92 quarters and a
#: fall-back day 100, which the storage and accumulator layers must both honour.
SPRING_FORWARD = datetime(2026, 3, 29, 12, 0, tzinfo=TZ)
FALL_BACK = datetime(2026, 10, 25, 12, 0, tzinfo=TZ)

# Source entities used throughout the suite. They are deliberately named after
# the real AlphaESS Modbus / HomeWizard entities on the maintainer's system.
HOUSE_LOAD = "sensor.alphaess_current_house_load"
DAILY_HOUSE_LOAD = "sensor.alphaess_today_s_house_load"
BATTERY_SOC = "sensor.alphaess_soc_battery"
BATTERY_POWER = "sensor.alphaess_power_battery"
PV_POWER = "sensor.alphaess_current_pv_production"
GRID_POWER = "sensor.p1_meter_active_power"
EV_POWER = "sensor.ev_charger_power"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration in every test."""
    return


def set_sensor(
    hass: HomeAssistant,
    entity_id: str,
    state: object,
    unit: str | None,
    device_class: str | None = None,
    state_class: str | None = "measurement",
) -> None:
    """Write a source sensor state with the attributes validation looks at."""
    attributes: dict[str, object] = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    if device_class is not None:
        attributes["device_class"] = device_class
    if state_class is not None:
        attributes["state_class"] = state_class
    hass.states.async_set(entity_id, state, attributes)


@pytest.fixture
def source_entities(hass: HomeAssistant) -> None:
    """Populate the state machine with a plausible, valid source set."""
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    set_sensor(hass, DAILY_HOUSE_LOAD, 4.2, "kWh", "energy", "total")
    set_sensor(hass, BATTERY_SOC, 55, "%", "battery")
    set_sensor(hass, BATTERY_POWER, -664, "W", "power")
    set_sensor(hass, PV_POWER, 3000, "W", "power")
    set_sensor(hass, GRID_POWER, -336, "W", "power")


def set_absorbing_snapshot(hass: HomeAssistant) -> None:
    """Re-point the live flows at a site that can absorb a discharge.

    ``source_entities`` is deliberately a sunny midday snapshot -- 3 kW of PV
    against 2 kW of house load, *exporting* 336 W -- so a forced discharge there
    would push energy straight onto the grid, and the export gate rightly
    refuses it. Under the beta.8 rule it did not: that rule compared the command
    against the house load alone and so read 2 kW of absorbing capacity on a site
    that had none.

    Any test that needs a *safe* verdict therefore has to describe a site that
    can actually take one. This is the same house load with the sun down and the
    shortfall imported, which balances exactly: 2000 W of load = 0 W of PV + 0 W
    of battery + 2000 W of import, giving 2 kW of capacity.
    """
    set_sensor(hass, PV_POWER, 0, "W", "power")
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, 2000, "W", "power")


#: The Phase-4 control surface, at rest, with the values the live installation
#: reports when no dispatch is running.
CONTROL_SURFACE_AT_REST: dict[str, object] = {
    "sensor.alphaess_dispatch_start": 0,
    "sensor.alphaess_dispatch_mode": 0,
    "sensor.alphaess_dispatch_active_power": 0,
    "sensor.alphaess_dispatch_soc": 0,
    "sensor.alphaess_dispatch_time": 90,
    "sensor.alphaess_max_feed_to_grid": 100,
}


@pytest.fixture
def control_surface(hass: HomeAssistant) -> None:
    """Populate the state machine with a healthy control surface.

    Deliberately **not** autouse. Most of the suite runs without it, which means
    the default condition across the whole project is a control surface that is
    absent -- and an absent one must leave the integration loading, learning and
    forecasting exactly as before. Tests that want a usable one ask for it.

    The values are the ones the live installation reports at rest: no dispatch
    running, the feed-in limit at a hundred percent, both safety automations on,
    and neither of the two features that drive the battery on their own.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        AUTOMATION_DISPATCH_RESET_FULL,
        AUTOMATION_HOLD_MONITOR,
        BOOLEAN_EXCESS_EXPORT,
        BOOLEAN_PEAK_SHAVING,
        CHARGE_FAMILY,
        DISCHARGE_FAMILY,
    )

    for entity_id, value in CONTROL_SURFACE_AT_REST.items():
        hass.states.async_set(entity_id, value)
    for family in (DISCHARGE_FAMILY, CHARGE_FAMILY):
        hass.states.async_set(family.activate, "off")
        hass.states.async_set(family.hold, "off")
        hass.states.async_set(family.power, "0.0")
        hass.states.async_set(family.cutoff_soc, "10")
        hass.states.async_set(family.duration, "120")
        hass.states.async_set(family.timer, "idle")
    hass.states.async_set(AUTOMATION_DISPATCH_RESET_FULL, "on")
    hass.states.async_set(AUTOMATION_HOLD_MONITOR, "on")
    hass.states.async_set(BOOLEAN_EXCESS_EXPORT, "off")
    hass.states.async_set(BOOLEAN_PEAK_SHAVING, "off")


@pytest.fixture
def frank_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Register a Frank Quarter Prices entry for Alpha EMS to reference.

    Alpha EMS requires one, and the options form renders the selection as a
    dropdown built from the entries that actually exist -- so without this the
    stored id would not be a selectable value.
    """
    # ``data`` carries the country, as the live entry does. It is what the
    # market timezone is derived from, and the unique id is the country too --
    # which is why at most two of these entries can ever exist.
    entry = MockConfigEntry(
        domain=DOMAIN_FRANK,
        title="Frank Quarter Prices (NL)",
        unique_id="NL",
        data={"country": "NL"},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def solcast_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Register a Solcast PV Forecast entry."""
    entry = MockConfigEntry(domain=DOMAIN_SOLCAST, title="Solcast PV Forecast")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def config_data(frank_config_entry: MockConfigEntry) -> dict[str, object]:
    """Return a complete, valid config-entry payload."""
    return {
        CONF_NAME: "Alpha EMS",
        CONF_HOUSE_LOAD_ENTITY: HOUSE_LOAD,
        CONF_DAILY_HOUSE_LOAD_ENTITY: DAILY_HOUSE_LOAD,
        CONF_BATTERY_SOC_ENTITY: BATTERY_SOC,
        CONF_BATTERY_POWER_ENTITY: BATTERY_POWER,
        CONF_BATTERY_POWER_SIGN: DEFAULT_BATTERY_POWER_SIGN,
        # Phase-3 planning figures, as the config flow requires them of a new
        # installation: a 10 kWh pack behind a 5 kW inverter. An installation
        # upgrading from an earlier release has none of these, which is what
        # ``test_beta6_upgrade.py`` builds explicitly.
        CONF_BATTERY_CAPACITY_KWH: 10.0,
        CONF_BATTERY_MIN_SOC_PERCENT: DEFAULT_BATTERY_MIN_SOC_PERCENT,
        CONF_BATTERY_MAX_CHARGE_KW: 5.0,
        CONF_BATTERY_MAX_DISCHARGE_KW: 5.0,
        CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: (
            DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT
        ),
        CONF_HAS_PV: True,
        CONF_PV_POWER_ENTITY: PV_POWER,
        CONF_GRID_POWER_ENTITY: GRID_POWER,
        CONF_GRID_POWER_SIGN: DEFAULT_GRID_POWER_SIGN,
        CONF_FRANK_ENTRY_ID: frank_config_entry.entry_id,
        CONF_USE_PV_FORECAST: False,
    }


@pytest.fixture
def mock_config_entry(config_data: dict[str, object]) -> MockConfigEntry:
    """Return an unloaded Alpha EMS config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=config_data,
        options={},
        version=CONFIG_ENTRY_VERSION,
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    source_entities: None,
) -> MockConfigEntry:
    """Set up the integration with valid sources and a fixed timezone."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


#: Stable identifiers of the two live sites, named so no test repeats the literal
#: and a change of fixture cannot leave one file behind.
ACHTERKANT = "1111-2222-3333-a379"
VOORKANT = "4444-5555-6666-f675"

#: The two sites the live account reports, with their real figures and the full
#: field set the live action actually returns -- including the fields Alpha EMS
#: does not read.
#:
#: The extra fields are the point. A fixture carrying only what the parser wants
#: cannot catch a parser that reads the wrong thing, and cannot demonstrate that
#: unread fields stay unread.
LIVE_SITES: tuple[dict[str, Any], ...] = (
    {
        "resource_id": "1111-2222-3333-a379",
        "name": "Achterkant",
        "capacity": 5,
        "capacity_dc": 3.65,
        "azimuth": -75,
        "compass_degrees": 75,
        "compass_direction": "ENE",
        "tilt": 38,
        "install_date": "2023-05-01T00:00:00+00:00",
        "loss_factor": 0.9,
        "tags": [],
    },
    {
        "resource_id": "4444-5555-6666-f675",
        "name": "Voorkant",
        "capacity": 5,
        "capacity_dc": 2.43,
        "azimuth": 105,
        "compass_degrees": 255,
        "compass_direction": "WSW",
        "tilt": 38,
        "install_date": "2023-05-01T00:00:00+00:00",
        "loss_factor": 0.9,
        "tags": [],
    },
)

#: The configuration block the live account reports, verbatim in substance.
LIVE_CONFIGURATION: dict[str, Any] = {
    "key_estimate": "estimate",
    "get_actuals": False,
    "use_actuals": 0,
    "auto_dampen": False,
    "hard_limit": 100.0,
    "excluded_sites": [],
    # Deliberately present, and deliberately never read: nothing may carry key
    # material out of this boundary.
    "api_key": "SECRET-KEY-VALUE",
}


class FakeSolcast:
    """A stand-in for the Solcast integration's two read-only actions.

    Registered with ``SupportsResponse.ONLY`` like the real ones, so a caller that
    forgot ``return_response`` fails here exactly as it would in production.
    """

    def __init__(self) -> None:
        self.sites: list[dict[str, Any]] = [dict(site) for site in LIVE_SITES]
        self.configuration: dict[str, Any] = dict(LIVE_CONFIGURATION)
        self.diagnostic_calls = 0
        self.forecast_calls: list[dict[str, Any]] = []
        #: Site identifiers that should return nothing, for the partial case.
        self.silent_sites: set[str] = set()
        self.fail_diagnostic = False
        self.fail_forecast = False
        #: Whether the diagnostic wraps its payload under ``data``, as the live
        #: action does. Switchable so the flat shape stays covered too.
        self.nested_response = True
        #: kW per site, so a per-site sum is distinguishable from the aggregate.
        self.power_by_site: dict[str, float] = {
            ACHTERKANT: 2.0,
            VOORKANT: 3.0,
        }
        self.aggregate_power = 5.0

    def register(self, hass: HomeAssistant) -> None:
        """Register both actions on the fake domain."""
        hass.services.async_register(
            SOLCAST_DOMAIN,
            SOLCAST_SERVICE_DIAGNOSTIC,
            self._diagnostic,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            SOLCAST_DOMAIN,
            SOLCAST_SERVICE_QUERY_FORECAST,
            self._forecast,
            supports_response=SupportsResponse.ONLY,
        )

    async def _diagnostic(self, call: ServiceCall) -> dict[str, Any]:
        """Return the diagnostic response in the shape the live action uses.

        **Wrapped under ``data``**, which is the shape that exposed the beta.10
        defect. The earlier version of this fake returned the payload flat,
        because it was written from a human-readable transcription of a
        diagnostics download rather than from the raw action response -- so it
        reproduced the same wrong assumption the parser made and could only ever
        confirm it.

        Both actions wrap their result: the forecast query returns a list under
        ``data`` and this one returns a mapping under it.
        """
        self.diagnostic_calls += 1
        if self.fail_diagnostic:
            raise RuntimeError("solcast is reloading")
        payload = {
            "version": "v4.6.1",
            "api_limit": 10,
            "api_used": 8,
            "forecast_health": "fresh",
            "sites": [dict(site) for site in self.sites],
            "configuration": dict(self.configuration),
            "dampening": {"enabled": False, "auto_dampening": False},
        }
        return {"data": payload} if self.nested_response else payload

    async def _forecast(self, call: ServiceCall) -> dict[str, Any]:
        self.forecast_calls.append(dict(call.data))
        if self.fail_forecast:
            raise RuntimeError("no cached data")
        site = call.data.get("site")
        if site in self.silent_sites:
            return {"data": []}
        kw = self.aggregate_power if site is None else self.power_by_site.get(site, 0.0)
        start = datetime.fromisoformat(call.data["start_date_time"])
        end = datetime.fromisoformat(call.data["end_date_time"])
        rows: list[dict[str, Any]] = []
        moment = start
        while moment < end:
            rows.append(
                {
                    "period_start": moment.astimezone(TZ),
                    "pv_estimate": kw,
                    "pv_estimate10": kw / 2.0,
                    "pv_estimate90": kw * 2.0,
                }
            )
            moment += timedelta(minutes=30)
        return {"data": rows}


#: The day the price fixture publishes. Mirrors ``forecast_helpers.NORMAL``,
#: which cannot be imported here because that module imports this one --
#: ``test_price_capability`` asserts the two have not drifted apart.
PRICE_DAY = date(2026, 8, 19)


class FakeFrank:
    """The price source as Alpha EMS actually sees it: published entity state.

    Deliberately **not** a service fake, because there is nothing to fake: prices
    are read from state, no action is called to obtain them, and there is no call
    site in Alpha EMS that could make this integration fetch. A fake with a
    ``calls`` counter would imply a coupling that does not exist.

    Entities are registered with the real unique ids -- ``f"{entry_id}_{key}"`` --
    so the capability probe resolves them the way it does in production, through
    the registry. Hard-coding the entity ids here would test a lookup nobody
    performs, and would hide a rename that the real resolution survives.
    """

    #: ``domain`` and the source's own entity key, per entity.
    KEYS = (
        ("sensor", FRANK_KEY_PRICES_TODAY, "frank_prices_today"),
        ("sensor", FRANK_KEY_PRICES_TOMORROW, "frank_prices_tomorrow"),
        (
            "binary_sensor",
            FRANK_KEY_TOMORROW_AVAILABLE,
            "frank_tomorrow_prices_available",
        ),
        ("sensor", FRANK_KEY_CURRENT_PRICE, "frank_current_price"),
        ("sensor", FRANK_KEY_CURRENT_RETURN_PRICE, "frank_current_return_price"),
    )

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.entity_ids: dict[str, str] = {}

    def register(self, *, keys: tuple[str, ...] | None = None) -> None:
        """Create the source's entities in the registry.

        ``keys`` narrows the set, so a test can describe an installation where one
        entity is genuinely absent rather than merely unavailable. Those are
        different facts and the capability reports them differently.
        """
        registry = er.async_get(self.hass)
        for domain, key, object_id in self.KEYS:
            if keys is not None and key not in keys:
                continue
            entry = registry.async_get_or_create(
                domain,
                DOMAIN_FRANK,
                f"{self.entry.entry_id}_{key}",
                config_entry=self.entry,
                suggested_object_id=object_id,
            )
            self.entity_ids[key] = entry.entity_id

    def rename(self, key: str, entity_id: str) -> str:
        """Rename one entity, as a user may.

        Resolution is by unique id, so this must change nothing. The test that
        uses it is the one that proves no entity id is hard-coded anywhere.
        """
        registry = er.async_get(self.hass)
        updated = registry.async_update_entity(
            self.entity_ids[key], new_entity_id=entity_id
        )
        self.entity_ids[key] = updated.entity_id
        return updated.entity_id

    def set_options(self, **options: Any) -> None:
        """Set the source entry's options, which the export figure derives from."""
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    # -- publication ----------------------------------------------------------

    def publish_day(
        self,
        key: str,
        blocks: list[dict[str, Any]] | None,
        *,
        resolution_minutes: int | None = 15,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Publish one day, or mark it unavailable the way the source does.

        ``blocks is None`` reproduces the unpublished next day **faithfully**, and
        the faithful part is what matters: Home Assistant writes an entity's
        attributes only while it is available, so an unavailable entity carries
        *no* ``prices`` and no ``available`` attribute either. Setting the state to
        ``unavailable`` while leaving attributes in place would be a shape that
        cannot occur, and a test built on it would prove nothing about the real
        one.
        """
        entity_id = self.entity_ids[key]
        if blocks is None:
            self.hass.states.async_set(entity_id, STATE_UNAVAILABLE, {})
            return
        attributes: dict[str, Any] = {"prices": blocks, **(extra or {})}
        if resolution_minutes is not None:
            attributes["resolution_minutes"] = resolution_minutes
        self.hass.states.async_set(entity_id, str(len(blocks)), attributes)

    def publish(
        self,
        *,
        today: list[dict[str, Any]] | None,
        tomorrow: list[dict[str, Any]] | None = None,
        tomorrow_published: bool | str | None = None,
        current_price: float | str | None = None,
        current_return_price: float | str | None = None,
        resolution_minutes: int | None = 15,
    ) -> None:
        """Publish a whole coherent state of the source.

        ``tomorrow_published`` defaults to whether a next day was given, which is
        the coherent pairing. It is settable independently precisely so a test can
        describe the incoherent combinations -- the source claiming a day it is not
        carrying, or carrying one it has not announced -- because those are the
        cases the reason taxonomy has to keep apart.
        """
        self.publish_day(
            FRANK_KEY_PRICES_TODAY, today, resolution_minutes=resolution_minutes
        )
        self.publish_day(
            FRANK_KEY_PRICES_TOMORROW,
            tomorrow,
            resolution_minutes=resolution_minutes,
            extra={"available": True, "last_attempt": "2026-08-20T13:31:00+02:00"},
        )

        published = (
            tomorrow is not None if tomorrow_published is None else (tomorrow_published)
        )
        if FRANK_KEY_TOMORROW_AVAILABLE in self.entity_ids:
            state = (
                published
                if isinstance(published, str)
                else (STATE_ON if published else STATE_OFF)
            )
            self.hass.states.async_set(
                self.entity_ids[FRANK_KEY_TOMORROW_AVAILABLE], state, {}
            )

        for key, value in (
            (FRANK_KEY_CURRENT_PRICE, current_price),
            (FRANK_KEY_CURRENT_RETURN_PRICE, current_return_price),
        ):
            if key not in self.entity_ids:
                continue
            self.hass.states.async_set(
                self.entity_ids[key],
                STATE_UNAVAILABLE if value is None else str(value),
                {},
            )


@pytest.fixture
def frank(hass: HomeAssistant, frank_config_entry: MockConfigEntry) -> FakeFrank:
    """Register the price source's entities and publish a healthy day.

    The entry is **not** marked loaded, and that omission is deliberate: nothing
    in Alpha EMS consults another integration's setup state any more. If a future
    change reintroduced a lifecycle probe, every test taking this fixture would
    fail -- which is a stronger guard than a comment.
    """
    fake = FakeFrank(hass, frank_config_entry)
    fake.register()
    fake.set_options(
        feed_in_adjustment=SYNTHETIC_FEED_IN_ADJUSTMENT, apply_feed_in_vat=False
    )
    fake.publish(today=synthetic_day(PRICE_DAY), tomorrow=None)
    return fake


@pytest.fixture
def solcast(hass: HomeAssistant, solcast_config_entry: MockConfigEntry) -> FakeSolcast:
    """Register the fake Solcast boundary and mark its entry loaded.

    The entry state matters: the capability check refuses to query an integration
    that is not loaded, which is what stops Alpha EMS calling into Solcast while
    it is reloading. A bare ``MockConfigEntry`` is ``NOT_LOADED``.

    Lives here rather than in a test module because two files need it, and
    importing a fixture across test modules shadows the name in every test that
    takes it as a parameter.
    """
    solcast_config_entry.mock_state(hass, ConfigEntryState.LOADED)
    fake = FakeSolcast()
    fake.register(hass)
    return fake
