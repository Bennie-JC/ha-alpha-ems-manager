"""Config and options flows for Alpha EMS Manager.

The flow is split into a handful of small, coherent steps rather than one wall
of technical fields. Each step validates its selections against the live state
machine before moving on, so a mistyped or incompatible entity is caught while
the user is still looking at the form.

Two selection styles are used deliberately:

* **Entity selectors** for the battery, house-load, PV and grid sources. The
  AlphaESS Modbus package that provides "Current House Load" is a YAML template
  package with no config entry and no device, so there is nothing to discover;
  the user picks the entities. Nothing is auto-bound, because silently binding
  the wrong entity is far worse than asking.
* **Config-entry pickers** for Frank Quarter Prices and Solcast, which *are*
  real config-entry integrations. Referencing the entry survives entity renames,
  which a hard-coded entity id would not.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    BATTERY_MAX_SOC_PERCENT,
    BATTERY_SIGN_OPTIONS,
    CONF_ALLOW_BATTERY_EXPORT,
    CONF_ALLOW_GRID_CHARGING,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CONTROL_EXECUTION_ENABLED,
    CONF_CONTROL_EXPORT_MARGIN_PERCENT,
    CONF_CONTROL_HORIZON_MINUTES,
    CONF_DAILY_HOUSE_LOAD_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_FRANK_ENTRY_ID,
    CONF_GRID_CHARGE_BUDGET_KWH,
    CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_SIGN,
    CONF_HAS_PV,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_MINIMUM_TRADE_GAIN_EUR,
    CONF_NAME,
    CONF_PV_POWER_ENTITY,
    CONF_SELECTED_SOLCAST_SITE_IDS,
    CONF_SOLCAST_ENTRY_ID,
    CONF_USE_PV_FORECAST,
    CONFIG_ENTRY_VERSION,
    CONTROL_DURATION_STEP_MINUTES,
    DEFAULT_ALLOW_BATTERY_EXPORT,
    DEFAULT_ALLOW_GRID_CHARGING,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_BATTERY_POWER_SIGN,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT,
    DEFAULT_CONTROL_HORIZON_MINUTES,
    DEFAULT_GRID_CHARGE_BUDGET_KWH,
    DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    DEFAULT_GRID_POWER_SIGN,
    DEFAULT_INSTANCE_NAME,
    DEFAULT_MINIMUM_TRADE_GAIN_EUR,
    DOMAIN,
    DOMAIN_FRANK,
    DOMAIN_SOLCAST,
    GRID_SIGN_OPTIONS,
    MAX_BATTERY_CAPACITY_KWH,
    MAX_BATTERY_POWER_KW,
    MAX_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    MAX_CONTROL_EXPORT_MARGIN_PERCENT,
    MAX_CONTROL_HORIZON_MINUTES,
    MAX_GRID_CHARGE_BUDGET_KWH,
    MAX_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    MAX_MINIMUM_TRADE_GAIN_EUR,
    MIN_BATTERY_CAPACITY_KWH,
    MIN_BATTERY_POWER_KW,
    MIN_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    MIN_CONTROL_EXPORT_MARGIN_PERCENT,
    MIN_CONTROL_HORIZON_MINUTES,
    MIN_GRID_CHARGE_BUDGET_KWH,
    MIN_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    MIN_MINIMUM_TRADE_GAIN_EUR,
)
from .solcast_source import discover as discover_solcast
from .solcast_source import read_facts
from .validation import (
    validate_energy_entity,
    validate_percentage_entity,
    validate_power_entity,
)

# Filtered by domain only, deliberately *not* by ``device_class``.
#
# Validation accepts an entity on its ``unit_of_measurement`` (see validation.py),
# because that is what actually determines whether a reading can be interpreted.
# A ``device_class`` filter here is frontend-only -- it never rejects a submitted
# value -- so adding one would hide entities the integration happily accepts and
# make them unselectable through the UI. That is not hypothetical: the AlphaESS
# Modbus source is a YAML template package, and a template sensor with
# ``unit_of_measurement: W`` but no ``device_class`` is exactly the primary
# house-load source this integration was written for.
_POWER_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=Platform.SENSOR)
)
_ENERGY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=Platform.SENSOR)
)
_BATTERY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=Platform.SENSOR)
)


#: The project's first numeric selectors.
#:
#: ``BOX`` rather than ``SLIDER`` for every one of them: these are datasheet
#: figures a user reads off their hardware, not preferences to be dragged, and a
#: slider cannot express 10.1 kWh legibly. Each carries its unit, so the
#: electrical boundary in the label is reinforced by the box itself.
#:
#: The bounds duplicate the model's own validation on purpose. The selector stops
#: an implausible value being typed; ``build_limits`` stops one that arrives any
#: other way -- a hand-edited entry, or a key carried over from a future release.
#: A single guard at either layer alone would be a guard with a hole in it.
def _number_selector(
    *,
    minimum: float,
    maximum: float,
    step: float,
    unit: str,
) -> selector.NumberSelector:
    """Return a bounded numeric box carrying its unit."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


_CAPACITY_SELECTOR = _number_selector(
    minimum=MIN_BATTERY_CAPACITY_KWH,
    maximum=MAX_BATTERY_CAPACITY_KWH,
    step=0.1,
    unit="kWh",
)
_BATTERY_POWER_SELECTOR = _number_selector(
    minimum=MIN_BATTERY_POWER_KW,
    maximum=MAX_BATTERY_POWER_KW,
    step=0.1,
    unit="kW",
)
#: Zero is deliberately allowed: it means "no EMS reserve", and the inverter's
#: own floor still protects the cells. Refusing it would be an arbitrary
#: restriction, so the one genuine rule -- that a floor below the ceiling leaves
#: something usable -- is validated instead. See ``_validate_battery``.
_MIN_SOC_SELECTOR = _number_selector(
    minimum=0.0, maximum=BATTERY_MAX_SOC_PERCENT, step=1.0, unit="%"
)
#: The floor of fifty is not cosmetic. It is what catches a user entering ``0.90``
#: where ``90`` belongs, which would otherwise model a plausible-looking battery
#: that loses ninety per cent of everything put into it.
_EFFICIENCY_SELECTOR = _number_selector(
    minimum=MIN_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    maximum=MAX_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    step=1.0,
    unit="%",
)

#: The Phase-3 hardware facts that have no default, in form order.
#:
#: Nothing can derive them: a percentage sensor says nothing about how many
#: kilowatt-hours a percent is worth, and a power limit cannot be inferred from a
#: capacity without assuming a C-rate. Absent means the battery layer declines to
#: decide and names the missing field -- never a guessed value.
#: The commissioning tightener on how much grid energy one charge run may buy.
#:
#: Zero disables the tightener; it is **not** a zero cap. Stage A's own
#: ``expected_grid_to_battery_kwh`` is the hard ceiling whenever it is published,
#: and this can only ever bind earlier. Fifty kilowatt-hours is well above any
#: single run a domestic pack could take, so the upper bound is a typing guard
#: rather than a policy.
_GRID_BUDGET_SELECTOR = _number_selector(
    minimum=MIN_GRID_CHARGE_BUDGET_KWH,
    maximum=MAX_GRID_CHARGE_BUDGET_KWH,
    step=0.1,
    unit="kWh",
)

_HORIZON_SELECTOR = _number_selector(
    minimum=MIN_CONTROL_HORIZON_MINUTES,
    maximum=MAX_CONTROL_HORIZON_MINUTES,
    step=CONTROL_DURATION_STEP_MINUTES,
    unit="min",
)

_EXPORT_MARGIN_SELECTOR = _number_selector(
    minimum=MIN_CONTROL_EXPORT_MARGIN_PERCENT,
    maximum=MAX_CONTROL_EXPORT_MARGIN_PERCENT,
    step=1,
    unit="%",
)


def _control_schema(
    current: Callable[[str, Any], Any],
) -> vol.Schema:
    """Return the control-settings schema.

    Every field is ``Required`` with a default, because every one always has a
    usable value. The lower bound on the horizon is the load-bearing part: a
    command shorter than one planning interval would lapse before the next
    refresh could renew it, so the selector cannot express one. The safety gate
    checks the same bound independently -- a single guard at either layer alone
    would be a guard with a hole in it.

    **The execution enable is offered from beta.20 on**, and the reason it was
    withheld before no longer holds in the same way. It still cannot make a command
    reach the inverter on its own -- the release barrier is a source constant and
    turning this on does not move it -- but it is now one of two independent
    consents rather than a switch with nothing behind it, and the description says
    so rather than implying otherwise. Withholding it would mean the first Live run
    had to enable execution and open the barrier in the same step.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_CONTROL_HORIZON_MINUTES,
                default=current(
                    CONF_CONTROL_HORIZON_MINUTES, DEFAULT_CONTROL_HORIZON_MINUTES
                ),
            ): _HORIZON_SELECTOR,
            vol.Required(
                CONF_CONTROL_EXPORT_MARGIN_PERCENT,
                default=current(
                    CONF_CONTROL_EXPORT_MARGIN_PERCENT,
                    DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT,
                ),
            ): _EXPORT_MARGIN_SELECTOR,
            vol.Required(
                CONF_GRID_CHARGE_BUDGET_KWH,
                default=current(
                    CONF_GRID_CHARGE_BUDGET_KWH, DEFAULT_GRID_CHARGE_BUDGET_KWH
                ),
            ): _GRID_BUDGET_SELECTOR,
            vol.Required(
                CONF_CONTROL_EXECUTION_ENABLED,
                default=bool(current(CONF_CONTROL_EXECUTION_ENABLED, False)),
            ): selector.BooleanSelector(),
        }
    )


_TRADE_GAIN_SELECTOR = _number_selector(
    minimum=MIN_MINIMUM_TRADE_GAIN_EUR,
    maximum=MAX_MINIMUM_TRADE_GAIN_EUR,
    step=0.01,
    unit="EUR",
)

#: Per kilowatt-hour rather than per trade, and the unit says so. A step of one
#: cent, because a tenth of a cent per kWh is below anything a price curve can
#: justify caring about.
_GRID_CHARGE_MARGIN_SELECTOR = _number_selector(
    minimum=MIN_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    maximum=MAX_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    step=0.01,
    unit="EUR/kWh",
)


def _economics_schema(
    current: Callable[[str, Any], Any],
) -> vol.Schema:
    """Return the economic-settings schema.

    Three fields, all ``Required`` with a default, because all three always have a
    usable value. Zero gain is allowed and means "take every trade the prices
    justify" -- refusing it would be a policy this integration has no business
    imposing, and the reserve keeps its lexicographic priority either way.

    The two opt-ins default to **off** and both are offered, unlike the execution
    enable. They earn their place because they change the *published* plan: a user
    who turns grid charging on sees the economic action, the value forgone and the
    per-run diagnostics move, all without a command being sent.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_MINIMUM_TRADE_GAIN_EUR,
                default=current(
                    CONF_MINIMUM_TRADE_GAIN_EUR, DEFAULT_MINIMUM_TRADE_GAIN_EUR
                ),
            ): _TRADE_GAIN_SELECTOR,
            vol.Required(
                CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH,
                default=current(
                    CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH,
                    DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH,
                ),
            ): _GRID_CHARGE_MARGIN_SELECTOR,
            vol.Required(
                CONF_ALLOW_GRID_CHARGING,
                default=current(CONF_ALLOW_GRID_CHARGING, DEFAULT_ALLOW_GRID_CHARGING),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_ALLOW_BATTERY_EXPORT,
                default=current(
                    CONF_ALLOW_BATTERY_EXPORT, DEFAULT_ALLOW_BATTERY_EXPORT
                ),
            ): selector.BooleanSelector(),
        }
    )


BATTERY_HARDWARE_KEYS = (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
)


def _battery_planning_schema(
    current: Callable[[str, Any], Any] | None = None,
) -> vol.Schema:
    """Return the battery-planning fields.

    ``current`` is supplied by the options flow, and its presence is what makes
    the three hardware fields optional there. In the config flow they are
    required, because a new installation is walked through the form and can be
    complete from the start; in the options flow an installation upgrading from
    an earlier release has none of them, and forcing all three to be entered
    before a minimum state of charge could be changed would be a worse form than
    one that lets a user fill them in when they have the figures to hand.
    """
    if current is None:
        return vol.Schema(
            {
                vol.Required(CONF_BATTERY_CAPACITY_KWH): _CAPACITY_SELECTOR,
                vol.Required(
                    CONF_BATTERY_MIN_SOC_PERCENT,
                    default=DEFAULT_BATTERY_MIN_SOC_PERCENT,
                ): _MIN_SOC_SELECTOR,
                vol.Required(CONF_BATTERY_MAX_CHARGE_KW): _BATTERY_POWER_SELECTOR,
                vol.Required(CONF_BATTERY_MAX_DISCHARGE_KW): _BATTERY_POWER_SELECTOR,
                vol.Required(
                    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
                    default=DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
                ): _EFFICIENCY_SELECTOR,
            }
        )
    return vol.Schema(
        {
            vol.Optional(
                CONF_BATTERY_CAPACITY_KWH,
                description={
                    "suggested_value": current(CONF_BATTERY_CAPACITY_KWH, None)
                },
            ): _CAPACITY_SELECTOR,
            vol.Required(
                CONF_BATTERY_MIN_SOC_PERCENT,
                default=current(
                    CONF_BATTERY_MIN_SOC_PERCENT, DEFAULT_BATTERY_MIN_SOC_PERCENT
                ),
            ): _MIN_SOC_SELECTOR,
            vol.Optional(
                CONF_BATTERY_MAX_CHARGE_KW,
                description={
                    "suggested_value": current(CONF_BATTERY_MAX_CHARGE_KW, None)
                },
            ): _BATTERY_POWER_SELECTOR,
            vol.Optional(
                CONF_BATTERY_MAX_DISCHARGE_KW,
                description={
                    "suggested_value": current(CONF_BATTERY_MAX_DISCHARGE_KW, None)
                },
            ): _BATTERY_POWER_SELECTOR,
            vol.Required(
                CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
                default=current(
                    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
                    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
                ),
            ): _EFFICIENCY_SELECTOR,
        }
    )


def _validate_battery(user_input: dict[str, Any]) -> dict[str, str]:
    """Return errors for the battery-planning fields.

    One rule, and it is a real one rather than an arbitrary narrowing of the
    range: a minimum state of charge at or above the ceiling leaves no usable
    energy at all, so the battery could never be planned with. Expressed against
    the ceiling rather than against the literal 100 so that it keeps working
    unchanged when a later phase makes the ceiling configurable.
    """
    errors: dict[str, str] = {}
    minimum = user_input.get(CONF_BATTERY_MIN_SOC_PERCENT)
    if (
        isinstance(minimum, (int, float))
        and not isinstance(minimum, bool)
        and minimum >= BATTERY_MAX_SOC_PERCENT
    ):
        errors[CONF_BATTERY_MIN_SOC_PERCENT] = "min_soc_not_below_max"
    return errors


def _stored_site_ids(raw: Any) -> list[str]:
    """Return the stored site identifiers, sorted and filtered.

    Deliberately tolerant of a hand-edited document: a null or a number among
    the entries costs that entry rather than the whole form.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    return sorted({item for item in raw if isinstance(item, str) and item})


async def _site_options(
    hass: HomeAssistant,
    *,
    entry_id: str | None,
    stored: Sequence[str],
) -> list[selector.SelectOptionDict] | None:
    """Return the selectable Solcast sites, or ``None`` if none could be read.

    ``None`` means discovery failed or the boundary is unusable, and the caller
    must then leave the stored selection completely alone. Overwriting a valid
    selection because Solcast happened to be reloading would silently change
    which roofs the plan is about.

    A stored identifier the source no longer offers is **kept in the option
    list**, labelled as missing. That is the lesson the two dropdowns above
    already carry: a ``SelectSelector`` validates its submission against its
    option list, so a stored value that has vanished rejects *every* submission
    of the form at schema validation -- leaving the user unable to change any
    unrelated setting. It is also the right behaviour on its own terms, because a
    site that disappears must stay visible rather than being silently dropped.
    """
    capability = discover_solcast(hass, entry_id)
    if not capability.usable or not capability.diagnostic_service:
        return None

    facts = await read_facts(hass)
    if facts is None or not facts.sites:
        return None

    options = [
        selector.SelectOptionDict(value=site.resource_id, label=site.name)
        for site in sorted(facts.sites, key=lambda site: site.name.lower())
    ]
    known = {site.resource_id for site in facts.sites}
    options.extend(
        selector.SelectOptionDict(
            value=site_id, label=f"{site_id} (no longer offered by Solcast)"
        )
        for site_id in sorted(set(stored) - known)
    )
    return options


def _sign_selector(options: tuple[str, ...], key: str) -> selector.SelectSelector:
    """Return a translated dropdown for a sign convention."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(options),
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=key,
        )
    )


def _entry_options(hass: HomeAssistant, domain: str) -> list[selector.SelectOptionDict]:
    """Return selectable config entries of ``domain``."""
    return [
        selector.SelectOptionDict(value=entry.entry_id, label=entry.title)
        for entry in hass.config_entries.async_entries(domain)
    ]


def _valid_default(
    value: str | None, options: list[selector.SelectOptionDict]
) -> str | vol.Undefined:
    """Return ``value`` if it is still a selectable option, else no default.

    A ``SelectSelector`` validates a submitted value against its option list on
    the server side, and a form default is not exempt. Pre-filling a stale
    config-entry id therefore produces a form that renders normally and then
    refuses every submission.
    """
    if value is not None and any(option["value"] == value for option in options):
        return value
    return vol.UNDEFINED


class AlphaEmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Guided setup for one Alpha EMS Manager instance."""

    # v1 was the previous integration's source model. Bumping this is what lets
    # async_migrate_entry recognise and reject a legacy entry instead of loading
    # it with no usable sources.
    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        """Start with an empty draft configuration."""
        self._data: dict[str, Any] = {}

    # -- step 1: identity and shape --------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the instance name, learning source and system shape."""
        errors: dict[str, str] = {}

        # Frank Quarter Prices is a hard requirement, so say so before the user
        # fills in four screens of entity selections.
        if not _entry_options(self.hass, DOMAIN_FRANK):
            return self.async_abort(reason="frank_not_configured")

        if user_input is not None:
            error = validate_power_entity(self.hass, user_input[CONF_HOUSE_LOAD_ENTITY])
            if error:
                errors[CONF_HOUSE_LOAD_ENTITY] = error

            daily = user_input.get(CONF_DAILY_HOUSE_LOAD_ENTITY)
            if daily:
                error = validate_energy_entity(self.hass, daily)
                if error:
                    errors[CONF_DAILY_HOUSE_LOAD_ENTITY] = error

            ev = user_input.get(CONF_EV_POWER_ENTITY)
            if ev:
                error = validate_power_entity(self.hass, ev)
                if error:
                    errors[CONF_EV_POWER_ENTITY] = error
                elif ev == user_input.get(CONF_HOUSE_LOAD_ENTITY):
                    # baseline = max(measured - flexible, 0), so one entity in
                    # both roles makes the baseline exactly zero for every
                    # interval -- valid, complete, learned, and a confident
                    # 0 kWh forecast. Nothing downstream can tell that apart
                    # from a house that used no energy.
                    errors[CONF_EV_POWER_ENTITY] = "ev_entity_same_as_house_load"

            # Reported inline rather than as an abort: turning the forecast off
            # is a perfectly good way forward, and an abort would throw away
            # everything already typed.
            if user_input.get(CONF_USE_PV_FORECAST) and not _entry_options(
                self.hass, DOMAIN_SOLCAST
            ):
                errors[CONF_USE_PV_FORECAST] = "solcast_not_configured"

            if not errors:
                self._data.update(user_input)
                return await self.async_step_battery()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=DEFAULT_INSTANCE_NAME
                ): selector.TextSelector(),
                vol.Required(CONF_HOUSE_LOAD_ENTITY): _POWER_SELECTOR,
                vol.Optional(CONF_DAILY_HOUSE_LOAD_ENTITY): _ENERGY_SELECTOR,
                vol.Optional(CONF_EV_POWER_ENTITY): _POWER_SELECTOR,
                vol.Required(CONF_HAS_PV, default=True): selector.BooleanSelector(),
                vol.Required(
                    CONF_USE_PV_FORECAST, default=False
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    # -- step 2: battery --------------------------------------------------

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the battery sources, sign convention and planning limits.

        The planning figures live here rather than in a step of their own so a
        new installation reaches the end of the flow with a battery that can
        actually be planned with. They are required for that reason -- and
        because the integration already insists on a state-of-charge and a power
        sensor, so it already assumes a battery exists.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            error = validate_percentage_entity(
                self.hass, user_input[CONF_BATTERY_SOC_ENTITY]
            )
            if error:
                errors[CONF_BATTERY_SOC_ENTITY] = error
            error = validate_power_entity(
                self.hass, user_input[CONF_BATTERY_POWER_ENTITY]
            )
            if error:
                errors[CONF_BATTERY_POWER_ENTITY] = error

            errors.update(_validate_battery(user_input))

            if not errors:
                self._data.update(user_input)
                if self._data.get(CONF_HAS_PV):
                    return await self.async_step_solar()
                return await self.async_step_grid()

        schema = vol.Schema(
            {
                vol.Required(CONF_BATTERY_SOC_ENTITY): _BATTERY_SELECTOR,
                vol.Required(CONF_BATTERY_POWER_ENTITY): _POWER_SELECTOR,
                vol.Required(
                    CONF_BATTERY_POWER_SIGN, default=DEFAULT_BATTERY_POWER_SIGN
                ): _sign_selector(BATTERY_SIGN_OPTIONS, "battery_power_sign"),
                **_battery_planning_schema().schema,
            }
        )
        return self.async_show_form(
            step_id="battery",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    # -- step 3: solar (conditional) --------------------------------------

    async def async_step_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the PV production source. Only reached when PV is present."""
        errors: dict[str, str] = {}

        if user_input is not None:
            error = validate_power_entity(self.hass, user_input[CONF_PV_POWER_ENTITY])
            if error:
                errors[CONF_PV_POWER_ENTITY] = error
            if not errors:
                self._data.update(user_input)
                return await self.async_step_grid()

        schema = vol.Schema({vol.Required(CONF_PV_POWER_ENTITY): _POWER_SELECTOR})
        return self.async_show_form(
            step_id="solar",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    # -- step 4: grid -----------------------------------------------------

    async def async_step_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the grid meter source and its sign convention."""
        errors: dict[str, str] = {}

        if user_input is not None:
            error = validate_power_entity(self.hass, user_input[CONF_GRID_POWER_ENTITY])
            if error:
                errors[CONF_GRID_POWER_ENTITY] = error
            if not errors:
                self._data.update(user_input)
                return await self.async_step_sources()

        schema = vol.Schema(
            {
                vol.Required(CONF_GRID_POWER_ENTITY): _POWER_SELECTOR,
                vol.Required(
                    CONF_GRID_POWER_SIGN, default=DEFAULT_GRID_POWER_SIGN
                ): _sign_selector(GRID_SIGN_OPTIONS, "grid_power_sign"),
            }
        )
        return self.async_show_form(
            step_id="grid",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    # -- step 5: consumed integrations ------------------------------------

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the Frank Quarter Prices and Solcast config entries."""
        frank_options = _entry_options(self.hass, DOMAIN_FRANK)
        if not frank_options:
            return self.async_abort(reason="frank_not_configured")

        wants_forecast = bool(self._data.get(CONF_USE_PV_FORECAST))
        solcast_options = _entry_options(self.hass, DOMAIN_SOLCAST)
        if wants_forecast and not solcast_options:
            return self.async_abort(reason="solcast_not_configured")

        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title=self._data.get(CONF_NAME) or DEFAULT_INSTANCE_NAME,
                data=self._data,
            )

        fields: dict[Any, Any] = {
            vol.Required(CONF_FRANK_ENTRY_ID): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=frank_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
        if wants_forecast:
            fields[vol.Required(CONF_SOLCAST_ENTRY_ID)] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=solcast_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="sources",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), user_input
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> AlphaEmsOptionsFlow:
        """Return the options flow handler."""
        return AlphaEmsOptionsFlow()


class AlphaEmsOptionsFlow(OptionsFlow):
    """Lets every selection be changed without re-adding the entry.

    Four pages behind a menu rather than one long form. The source selections,
    the battery-planning figures, the control settings and the economic settings
    are edited on different occasions and by different reasoning -- "which
    sensor", "what hardware", "how to behave", "what is worth trading" -- and
    appending eight more fields to a form that already had thirteen would have
    buried them at the bottom.

    The stored keys stay **flat**. Collapsible sections were the other candidate
    and would have delivered nested values, which the effective-configuration
    rule, the unknown-key preservation rule and the cleared-optional rule all
    read flat. A second step costs one extra click and changes no storage.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the three groups of options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["sources", "battery", "control", "economics"],
        )

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the control settings.

        Four fields now, all with defaults, all describing how Alpha EMS should
        behave rather than what the hardware is -- so unlike the battery step there
        is nothing here that can be missing.
        """
        entry = self.config_entry

        def current(key: str, default: Any = None) -> Any:
            return entry.options.get(key, entry.data.get(key, default))

        if user_input is not None:
            return self.async_create_entry(
                title="", data={**entry.options, **user_input}
            )

        return self.async_show_form(
            step_id="control",
            data_schema=self.add_suggested_values_to_schema(
                _control_schema(current), user_input
            ),
        )

    async def async_step_economics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the economic settings.

        Nothing here can be missing and nothing needs cross-field validation --
        the selector's bounds are the whole rule -- so this is the shortest step
        in the flow. It merges from the existing options like its siblings, so
        editing an opt-in never disturbs a source selection.
        """
        entry = self.config_entry

        def current(key: str, default: Any = None) -> Any:
            return entry.options.get(key, entry.data.get(key, default))

        if user_input is not None:
            return self.async_create_entry(
                title="", data={**entry.options, **user_input}
            )

        return self.async_show_form(
            step_id="economics",
            data_schema=self.add_suggested_values_to_schema(
                _economics_schema(current), user_input
            ),
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the battery-planning figures.

        Saving here must never disturb anything else. The merge starts from the
        existing options for that reason, and the three hardware fields are
        cleared to an explicit ``None`` when the user empties them -- deleting the
        key would not clear them, because the effective configuration falls back
        to the original entry data.
        """
        entry = self.config_entry
        errors: dict[str, str] = {}

        def current(key: str, default: Any = None) -> Any:
            return entry.options.get(key, entry.data.get(key, default))

        if user_input is not None:
            errors = _validate_battery(user_input)
            if not errors:
                merged = {**entry.options, **user_input}
                for optional in BATTERY_HARDWARE_KEYS:
                    if optional not in user_input:
                        merged[optional] = None
                return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="battery",
            data_schema=self.add_suggested_values_to_schema(
                _battery_planning_schema(current), user_input
            ),
            errors=errors,
        )

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the full set of changeable source selections."""
        entry = self.config_entry
        errors: dict[str, str] = {}

        def current(key: str, default: Any = None) -> Any:
            return entry.options.get(key, entry.data.get(key, default))

        if user_input is not None:
            errors = self._validate(user_input)
            if not errors:
                # Start from the existing options so a key that is not part of
                # this form -- now or after a future release adds one -- is
                # never dropped by an unrelated edit.
                merged = {**entry.options, **user_input}
                # An optional field that the user cleared vanishes from
                # user_input entirely. Deleting the key would not clear it,
                # because the effective configuration falls back to entry.data,
                # which still holds the original selection -- so the cleared
                # state is recorded as an explicit None that shadows it.
                for optional in (
                    CONF_DAILY_HOUSE_LOAD_ENTITY,
                    CONF_EV_POWER_ENTITY,
                    CONF_PV_POWER_ENTITY,
                    CONF_SOLCAST_ENTRY_ID,
                ):
                    if optional not in user_input:
                        merged[optional] = None
                return self.async_create_entry(title="", data=merged)

        frank_options = _entry_options(self.hass, DOMAIN_FRANK)
        solcast_options = _entry_options(self.hass, DOMAIN_SOLCAST)

        # Frank is a required dropdown built from the config entries that exist
        # right now. If it has been removed and re-added, the stored entry id is
        # no longer a valid choice and voluptuous rejects the form on submit --
        # leaving the user unable to change *any* option, including repointing an
        # unrelated sensor, with deleting the entry (and all learned history) as
        # the only way out. Aborting with an explanation is far better than a
        # form that renders but can never be saved.
        if not frank_options:
            return self.async_abort(reason="frank_not_configured")

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOUSE_LOAD_ENTITY,
                    default=current(CONF_HOUSE_LOAD_ENTITY),
                ): _POWER_SELECTOR,
                vol.Optional(
                    CONF_DAILY_HOUSE_LOAD_ENTITY,
                    description={
                        "suggested_value": current(CONF_DAILY_HOUSE_LOAD_ENTITY)
                    },
                ): _ENERGY_SELECTOR,
                vol.Optional(
                    CONF_EV_POWER_ENTITY,
                    description={"suggested_value": current(CONF_EV_POWER_ENTITY)},
                ): _POWER_SELECTOR,
                vol.Required(
                    CONF_BATTERY_SOC_ENTITY,
                    default=current(CONF_BATTERY_SOC_ENTITY),
                ): _BATTERY_SELECTOR,
                vol.Required(
                    CONF_BATTERY_POWER_ENTITY,
                    default=current(CONF_BATTERY_POWER_ENTITY),
                ): _POWER_SELECTOR,
                vol.Required(
                    CONF_BATTERY_POWER_SIGN,
                    default=current(
                        CONF_BATTERY_POWER_SIGN, DEFAULT_BATTERY_POWER_SIGN
                    ),
                ): _sign_selector(BATTERY_SIGN_OPTIONS, "battery_power_sign"),
                vol.Required(
                    CONF_HAS_PV, default=bool(current(CONF_HAS_PV, False))
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_PV_POWER_ENTITY,
                    description={"suggested_value": current(CONF_PV_POWER_ENTITY)},
                ): _POWER_SELECTOR,
                vol.Required(
                    CONF_GRID_POWER_ENTITY,
                    default=current(CONF_GRID_POWER_ENTITY),
                ): _POWER_SELECTOR,
                vol.Required(
                    CONF_GRID_POWER_SIGN,
                    default=current(CONF_GRID_POWER_SIGN, DEFAULT_GRID_POWER_SIGN),
                ): _sign_selector(GRID_SIGN_OPTIONS, "grid_power_sign"),
                # Only offered as a default when it is still a valid choice. A
                # stale id -- Frank removed and re-added, so a different entry
                # exists under a new id -- would otherwise fail schema validation
                # on submit; leaving the field empty makes the user pick instead.
                vol.Required(
                    CONF_FRANK_ENTRY_ID,
                    default=_valid_default(current(CONF_FRANK_ENTRY_ID), frank_options),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=frank_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_USE_PV_FORECAST,
                    default=bool(current(CONF_USE_PV_FORECAST, False)),
                ): selector.BooleanSelector(),
                # Same treatment as the Frank dropdown above, and for the same
                # reason. A SelectSelector validates its submission against the
                # option list, so a stored id that no longer appears -- Solcast
                # removed, or removed and re-added under a new entry id --
                # rejected *every* submission of this form at schema validation,
                # before ``_validate`` could turn it into a field error. The user
                # could not change any unrelated setting until they happened to
                # clear this one by hand.
                vol.Optional(
                    CONF_SOLCAST_ENTRY_ID,
                    description={
                        "suggested_value": _valid_default(
                            current(CONF_SOLCAST_ENTRY_ID), solcast_options
                        )
                    },
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=solcast_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        # Which rooftop sites belong to this installation. Offered only when the
        # boundary is usable and sites could actually be read, so the form never
        # presents an empty multi-select -- submitting one would wipe a valid
        # selection, and a form that can destroy data by being saved unchanged is
        # worse than a form that omits the field.
        #
        # The question is membership and nothing else. The user is never asked
        # which site is AC- or DC-coupled, or which one feeds the hybrid: that is
        # not reliably knowable to them, and a guessed topology recorded as fact
        # would be worse than the declared unknown Phase 5 stores instead.
        stored_sites = _stored_site_ids(current(CONF_SELECTED_SOLCAST_SITE_IDS))
        site_options = None
        if current(CONF_USE_PV_FORECAST, False) and current(CONF_SOLCAST_ENTRY_ID):
            site_options = await _site_options(
                self.hass,
                entry_id=current(CONF_SOLCAST_ENTRY_ID),
                stored=stored_sites,
            )
        if site_options is not None:
            offered = {option["value"] for option in site_options}
            schema = schema.extend(
                {
                    vol.Optional(
                        CONF_SELECTED_SOLCAST_SITE_IDS,
                        description={
                            "suggested_value": [
                                site_id
                                for site_id in stored_sites
                                if site_id in offered
                            ]
                            # No stored answer yet means every discovered site,
                            # which is the same default the coordinator persists
                            # on first discovery. Shown rather than assumed, so
                            # the user sees what they are agreeing to.
                            or sorted(offered)
                        },
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=site_options,
                            mode=selector.SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    )
                }
            )

        return self.async_show_form(
            step_id="sources",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    def _validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Return per-field error keys for an options submission."""
        errors: dict[str, str] = {}

        checks = (
            (CONF_HOUSE_LOAD_ENTITY, validate_power_entity, True),
            (CONF_BATTERY_SOC_ENTITY, validate_percentage_entity, True),
            (CONF_BATTERY_POWER_ENTITY, validate_power_entity, True),
            (CONF_GRID_POWER_ENTITY, validate_power_entity, True),
            (CONF_DAILY_HOUSE_LOAD_ENTITY, validate_energy_entity, False),
            (CONF_EV_POWER_ENTITY, validate_power_entity, False),
            (CONF_PV_POWER_ENTITY, validate_power_entity, False),
        )
        for key, validator, required in checks:
            value = user_input.get(key)
            if not value:
                if required:
                    errors[key] = "entity_not_found"
                continue
            error = validator(self.hass, value)
            if error:
                errors[key] = error

        # A system declared to have PV must say where its production is read.
        if user_input.get(CONF_HAS_PV) and not user_input.get(CONF_PV_POWER_ENTITY):
            errors[CONF_PV_POWER_ENTITY] = "pv_entity_required"

        # The flexible load is subtracted from measured house load, so pointing
        # both at one entity makes ``baseline = max(m - m, 0)`` -- exactly zero
        # for every interval of every day. Nothing downstream can detect that:
        # the intervals are valid, the days are complete, they count as learned,
        # and the forecast is a confident 0 kWh. It has to be refused here.
        if user_input.get(CONF_EV_POWER_ENTITY) and user_input.get(
            CONF_EV_POWER_ENTITY
        ) == user_input.get(CONF_HOUSE_LOAD_ENTITY):
            errors[CONF_EV_POWER_ENTITY] = "ev_entity_same_as_house_load"

        # Likewise, forecasting without a Solcast entry cannot work.
        if user_input.get(CONF_USE_PV_FORECAST) and not user_input.get(
            CONF_SOLCAST_ENTRY_ID
        ):
            errors[CONF_SOLCAST_ENTRY_ID] = "solcast_entry_required"

        return errors
