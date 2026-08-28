"""beta.31: the configured grid-charge margin, traced from disk to the solver.

**Why this exists.** The beta.31 implementation report claimed the installation's
0.05 EUR/kWh margin "is not configured". That claim was an overreach: what had
actually been established was that the value appeared **nowhere in the evidence**
-- not in diagnostics, not in the decision record, not even in the settings
fingerprint -- so it could not be *confirmed* from a diagnostic download. Absence
of publication is not absence of configuration, and treating one as the other is
exactly the kind of inference this project refuses elsewhere.

The whole economic result turns on it: 0.05 EUR/kWh on grid-caused charging is a
real hurdle, and 0.00 is none at all. So the chain is traced here end to end, and
the value is asserted at every point a reader might look at.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONF_ALLOW_BATTERY_EXPORT,
    CONF_ALLOW_GRID_CHARGING,
    CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH,
    CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    CONF_MINIMUM_TRADE_GAIN_EUR,
    CONFIG_ENTRY_VERSION,
    CONTROL_MODE_ACTIVE,
    DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    DOMAIN,
)
from custom_components.alpha_ems_manager.coordinator import SourceConfig
from custom_components.alpha_ems_manager.economic import fingerprint_settings

#: What the installation's UI shows, and therefore what the persisted options must
#: carry if the form was saved.
CONFIGURED_MARGIN = 0.05
CONFIGURED_GAIN = 0.20


async def _entry_with(hass: HomeAssistant, config_data: dict, **options):
    """Return a loaded entry whose *options* carry the economic settings."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=config_data,
        options=options,
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def test_the_default_is_zero_and_that_is_not_the_same_as_unconfigured() -> None:
    """The distinction the report failed to make, stated once.

    A default of zero means an installation that has never touched the setting
    carries no hurdle. It says nothing whatever about an installation that has.
    """
    assert DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH == 0.0


def test_options_shadow_data_so_a_saved_form_reaches_the_config() -> None:
    """**Question 1 and 8: what is persisted, and is the UI showing it?**

    ``SourceConfig.from_entry`` resolves ``entry.options`` first, then
    ``entry.data``, then the default -- and the options flow both *reads* the same
    order for the form's default and writes ``{**entry.options, **user_input}`` on
    submit. So a saved 0.05 is persisted in options and is what the form shows on
    reopening. There is no path by which the UI displays a value the runtime does
    not use, short of an unsaved form.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH: 0.99},
        options={CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH: CONFIGURED_MARGIN},
        version=CONFIG_ENTRY_VERSION,
    )

    config = SourceConfig.from_entry(entry)

    # Options win over data, which is what makes an Options Flow edit effective.
    assert config.grid_charge_margin_eur_per_kwh == CONFIGURED_MARGIN

    # And with nothing in options, data is still honoured before the default.
    from_data = SourceConfig.from_entry(
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH: 0.07},
            options={},
            version=CONFIG_ENTRY_VERSION,
        )
    )
    assert from_data.grid_charge_margin_eur_per_kwh == 0.07


def test_the_fingerprint_separates_two_installations_by_their_margin() -> None:
    """**Question 6, and the reason no historical decision was reproducible.**

    Until beta.31 this digest covered only the fixed per-run threshold, so two
    installations differing by a whole per-kWh margin produced an identical
    fingerprint -- and a recorded plan could not be replayed, because the settings
    it rested on were not recoverable from the evidence.
    """
    common = {
        "minimum_trade_gain_eur": CONFIGURED_GAIN,
        "battery_throughput_cost_eur_per_kwh": 0.0,
        "allow_grid_charging": True,
        "allow_battery_export": True,
        "bucket_kwh": 0.25,
    }

    zero = fingerprint_settings(grid_charge_margin_eur_per_kwh=0.0, **common)
    five = fingerprint_settings(
        grid_charge_margin_eur_per_kwh=CONFIGURED_MARGIN, **common
    )
    throughput = fingerprint_settings(
        **{
            **common,
            "grid_charge_margin_eur_per_kwh": 0.0,
            "battery_throughput_cost_eur_per_kwh": 0.03,
        }
    )

    assert zero != five
    assert zero != throughput
    assert five != throughput


def test_the_margin_has_no_default_in_the_fingerprint() -> None:
    """A parameter with a default is one a future caller can silently drop.

    Which is exactly how this setting spent a release doing nothing: it was read
    into the config and accepted by ``solve``, and the executor call between them
    was the gap.
    """
    with pytest.raises(TypeError):
        fingerprint_settings(  # type: ignore[call-arg]
            minimum_trade_gain_eur=0.2,
            allow_grid_charging=True,
            allow_battery_export=True,
            bucket_kwh=0.25,
        )


async def test_a_configured_margin_reaches_every_reader(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank
) -> None:
    """**Questions 2 to 5 and 7, end to end on a loaded entry.**

    A saved 0.05 must appear in the runtime config, in the planning bundle, in the
    solver's own record of what it used, in diagnostics, and in the decision
    record -- and must survive a reload. Anything less and the number a user reads
    is not the number their money was spent under.
    """
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    from .forecast_helpers import NORMAL, local, refresh_at
    from .frank_capture import synthetic_day
    from .test_beta24_live_charge import charge_now_price
    from .test_control_modes import set_mode

    entry = await _entry_with(
        hass,
        config_data,
        **{
            CONF_MINIMUM_TRADE_GAIN_EUR: CONFIGURED_GAIN,
            CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH: CONFIGURED_MARGIN,
            CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH: 0.0,
            CONF_ALLOW_GRID_CHARGING: True,
            CONF_ALLOW_BATTERY_EXPORT: True,
        },
    )
    coordinator = entry.runtime_data

    # 2. the runtime configuration
    assert coordinator.config.grid_charge_margin_eur_per_kwh == CONFIGURED_MARGIN
    assert coordinator.config.minimum_trade_gain_eur == CONFIGURED_GAIN

    from .forecast_helpers import history_before, seed

    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    await refresh_at(coordinator, local(NORMAL, 0, 0))

    # 3. what the solver was given, recorded on the outcome it produced
    outcome = (coordinator.data or {}).get("economic")
    assert outcome is not None
    assert outcome.grid_charge_margin_eur_per_kwh == CONFIGURED_MARGIN

    # 4. diagnostics, in both the gates block and the settings provenance
    payload = await async_get_config_entry_diagnostics(hass, entry)
    economic = (payload.get("data") or payload)["economic_plan"]
    assert (
        economic["planning"]["gates"]["grid_charge_margin_eur_per_kwh"]
        == CONFIGURED_MARGIN
    )
    assert (
        economic["provenance"]["settings"]["grid_charge_margin_eur_per_kwh"]
        == CONFIGURED_MARGIN
    )

    # 5. the decision record, which is what a replay reads
    records = coordinator.store.decisions
    assert records, "a refresh must leave a decision record"
    assert records[-1]["grid_charge_margin_eur_per_kwh"] == CONFIGURED_MARGIN

    # 7. and it survives a reload, because it lives in the entry rather than in
    #    memory.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.config.grid_charge_margin_eur_per_kwh == CONFIGURED_MARGIN


async def test_an_untouched_installation_carries_no_hurdle(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank
) -> None:
    """The other half of the answer: absent options really do mean zero.

    So a diagnostic reading ``0.0`` after beta.31 is evidence the setting is
    unset, where before beta.31 the same installation published nothing at all and
    no conclusion was available either way.
    """
    entry = await _entry_with(hass, config_data)

    assert entry.runtime_data.config.grid_charge_margin_eur_per_kwh == 0.0
