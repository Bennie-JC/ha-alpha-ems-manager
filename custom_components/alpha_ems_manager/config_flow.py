"""Config flow for the Alpha EMS Manager integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY_KWH_ENTITY,
    CONF_BATTERY_CURRENT_KWH_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CUMULATIVE_HOUSE_LOAD_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR,
    CONF_FRANK_PRICES_TODAY_SENSOR,
    CONF_FRANK_PRICES_TOMORROW_SENSOR,
    CONF_PV_ACTUAL_TODAY_SENSOR,
    CONF_PV_EAST_SENSOR,
    CONF_PV_FORECAST_TODAY_SENSOR,
    CONF_PV_FORECAST_TOMORROW_SENSOR,
    CONF_PV_WEST_SENSOR,
    DEFAULTS,
    DOMAIN,
    NAME,
)

# A sensor-domain entity selector reused for every field.
_SENSOR_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor")
)
# The battery capacity may be exposed via a number/input_number helper, so allow
# both sensor and number domains for that field.
_CAPACITY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["sensor", "number", "input_number"])
)


def _default(key: str, source: dict[str, Any]) -> Any:
    """Return an existing value or the documented default for a key."""
    if key in source:
        return source[key]
    return DEFAULTS.get(key, vol.UNDEFINED)


def _build_schema(source: dict[str, Any]) -> vol.Schema:
    """Build the data schema, pre-filling defaults from ``source``."""
    return vol.Schema(
        {
            # --- Household load ------------------------------------------------
            vol.Required(
                CONF_CUMULATIVE_HOUSE_LOAD_SENSOR,
                default=_default(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR, source),
            ): _SENSOR_SELECTOR,
            # --- PV production -------------------------------------------------
            vol.Required(
                CONF_PV_ACTUAL_TODAY_SENSOR,
                default=_default(CONF_PV_ACTUAL_TODAY_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Required(
                CONF_PV_FORECAST_TODAY_SENSOR,
                default=_default(CONF_PV_FORECAST_TODAY_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Required(
                CONF_PV_FORECAST_TOMORROW_SENSOR,
                default=_default(CONF_PV_FORECAST_TOMORROW_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Optional(
                CONF_PV_EAST_SENSOR,
                default=_default(CONF_PV_EAST_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Optional(
                CONF_PV_WEST_SENSOR,
                default=_default(CONF_PV_WEST_SENSOR, source),
            ): _SENSOR_SELECTOR,
            # --- Frank dynamic prices -----------------------------------------
            vol.Required(
                CONF_FRANK_PRICES_TODAY_SENSOR,
                default=_default(CONF_FRANK_PRICES_TODAY_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Required(
                CONF_FRANK_PRICES_TOMORROW_SENSOR,
                default=_default(CONF_FRANK_PRICES_TOMORROW_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Required(
                CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR,
                default=_default(CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Required(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR,
                default=_default(CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Required(
                CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR,
                default=_default(CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Required(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR,
                default=_default(
                    CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR, source
                ),
            ): _SENSOR_SELECTOR,
            # --- Battery -------------------------------------------------------
            vol.Required(
                CONF_BATTERY_CURRENT_KWH_SENSOR,
                default=_default(CONF_BATTERY_CURRENT_KWH_SENSOR, source),
            ): _SENSOR_SELECTOR,
            vol.Required(
                CONF_BATTERY_CAPACITY_KWH_ENTITY,
                default=_default(CONF_BATTERY_CAPACITY_KWH_ENTITY, source),
            ): _CAPACITY_SELECTOR,
            vol.Optional(
                CONF_BATTERY_SOC_SENSOR,
                default=_default(CONF_BATTERY_SOC_SENSOR, source),
            ): _SENSOR_SELECTOR,
        }
    )


class AlphaEmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Alpha EMS Manager."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        # Only allow a single instance of the integration.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title=NAME, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema({}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AlphaEmsOptionsFlow:
        """Return the options flow handler."""
        return AlphaEmsOptionsFlow()


class AlphaEmsOptionsFlow(OptionsFlow):
    """Handle reconfiguration of selected entities."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Pre-fill with current options, falling back to the original entry data.
        current = {**self.config_entry.data, **self.config_entry.options}

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(current),
        )
