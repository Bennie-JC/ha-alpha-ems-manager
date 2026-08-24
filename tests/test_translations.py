"""Translations, checked through Home Assistant's own translation loader.

The tests never read the JSON files directly. They drive the real flows, take
the field list from the schema Home Assistant actually renders, and resolve every
key through ``async_get_translations`` -- so a field added to a form without a
matching translation fails here rather than showing a raw key to the user.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    BATTERY_SIGN_OPTIONS,
    DOMAIN,
    GRID_SIGN_OPTIONS,
)

LANGUAGES = ("en", "nl")

#: The field list each config step is expected to render, in order.
EXPECTED_CONFIG_FIELDS: dict[str, list[str]] = {
    "user": [
        "name",
        "house_load_entity",
        "daily_house_load_entity",
        "ev_power_entity",
        "has_pv",
        "use_pv_forecast",
    ],
    "battery": [
        "battery_soc_entity",
        "battery_power_entity",
        "battery_power_sign",
        # Phase 3. Required in the config flow so a new installation ends up with
        # a battery that can actually be planned with.
        "battery_capacity_kwh",
        "battery_min_soc_percent",
        "battery_max_charge_kw",
        "battery_max_discharge_kw",
        "battery_round_trip_efficiency_percent",
    ],
    "solar": ["pv_power_entity"],
    "grid": ["grid_power_entity", "grid_power_sign"],
    "sources": ["frank_entry_id", "solcast_entry_id"],
}

#: The options flow is a menu of three pages. All are checked, in order.
EXPECTED_OPTIONS_MENU = ("sources", "battery", "control", "economics")

EXPECTED_OPTIONS_FIELDS: dict[str, list[str]] = {
    "sources": [
        "house_load_entity",
        "daily_house_load_entity",
        "ev_power_entity",
        "battery_soc_entity",
        "battery_power_entity",
        "battery_power_sign",
        "has_pv",
        "pv_power_entity",
        "grid_power_entity",
        "grid_power_sign",
        "frank_entry_id",
        "use_pv_forecast",
        "solcast_entry_id",
    ],
    "battery": [
        # Optional here, unlike in the config flow: an installation upgrading
        # from an earlier release has none of the three hardware figures, and
        # forcing all three before a minimum state of charge could be changed
        # would be a worse form than one that lets them be filled in later.
        "battery_capacity_kwh",
        "battery_min_soc_percent",
        "battery_max_charge_kw",
        "battery_max_discharge_kw",
        "battery_round_trip_efficiency_percent",
    ],
    # Phase 4. Two fields, both with defaults. The execution-enable key is
    # deliberately absent: while the release barrier makes it unable to change
    # anything, offering it would promise a capability that does not exist.
    "control": [
        "control_horizon_minutes",
        "control_export_margin_percent",
        # beta.20. The commissioning tightener, and the user's half of the two
        # consents a Live write needs. The other half is a release constant, and
        # the form says so rather than implying this switch is sufficient.
        "grid_charge_budget_kwh",
        "control_execution_enabled",
    ],
    # Phase 8. Three fields, all with defaults. The two opt-ins *are* offered,
    # unlike the execution enable, and the difference is that they change the
    # published plan: turning grid charging on moves the economic action, the
    # value forgone and the per-run diagnostics without a command being sent.
    "economics": [
        "minimum_trade_gain_eur",
        # beta.18: an additional per-kWh requirement on marginal grid-caused
        # charging. Listed beside the fixed per-run gain deliberately -- the two
        # are different quantities and the form has to show both.
        "grid_charge_margin_eur_per_kwh",
        "allow_grid_charging",
        "allow_battery_export",
    ],
}

CONFIG_ERRORS = (
    "entity_not_found",
    "invalid_power_entity",
    "invalid_energy_entity",
    "invalid_percentage_entity",
    "entity_not_numeric",
    "min_soc_not_below_max",
    "solcast_not_configured",
)

OPTIONS_ERRORS = (
    *CONFIG_ERRORS[:-1],
    "pv_entity_required",
    "solcast_entry_required",
)

ABORTS = ("frank_not_configured", "solcast_not_configured")


async def bundle(hass: HomeAssistant, language: str, category: str) -> dict[str, str]:
    """Return the flattened translations Home Assistant would serve."""
    return await async_get_translations(hass, language, category, {DOMAIN})


def own_keys(payload: dict[str, str]) -> set[str]:
    """Return only this integration's keys."""
    return {key for key in payload if key.startswith(f"component.{DOMAIN}.")}


async def rendered_config_steps(
    hass: HomeAssistant,
) -> dict[str, list[str]]:
    """Walk the config flow and record the fields each step renders."""
    from .test_config_flow import battery_step, grid_step, user_step

    steps: dict[str, list[str]] = {}

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    steps["user"] = [str(marker) for marker in result["data_schema"].schema]

    payloads = [
        user_step(use_pv_forecast=True),
        battery_step(),
        {"pv_power_entity": "sensor.alphaess_current_pv_production"},
        grid_step(),
    ]
    for payload in payloads:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], payload
        )
        assert result["type"] is FlowResultType.FORM, result
        steps[result["step_id"]] = [
            str(marker) for marker in result["data_schema"].schema
        ]

    return steps


async def test_the_config_steps_render_the_documented_fields(
    hass: HomeAssistant,
    source_entities: None,
    frank_config_entry: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
) -> None:
    """Each step renders exactly the fields this module checks translations for."""
    steps = await rendered_config_steps(hass)
    assert steps == EXPECTED_CONFIG_FIELDS


async def rendered_options_pages(
    hass: HomeAssistant, entry_id: str
) -> dict[str, list[str]]:
    """Walk both options pages and record the fields each renders."""
    pages: dict[str, list[str]] = {}
    for page in EXPECTED_OPTIONS_MENU:
        result = await hass.config_entries.options.async_init(entry_id)
        assert result["type"] is FlowResultType.MENU, result
        assert set(result["menu_options"]) == set(EXPECTED_OPTIONS_MENU)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": page}
        )
        assert result["type"] is FlowResultType.FORM, result
        pages[result["step_id"]] = [
            str(marker) for marker in result["data_schema"].schema
        ]
    return pages


async def test_the_options_form_renders_the_documented_fields(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Both options pages match the documented field lists, in order."""
    pages = await rendered_options_pages(hass, setup_integration.entry_id)

    assert pages == EXPECTED_OPTIONS_FIELDS


@pytest.mark.parametrize("language", LANGUAGES)
async def test_every_config_field_has_a_label_and_a_description(
    hass: HomeAssistant, language: str
) -> None:
    """No config field falls back to showing its raw key."""
    payload = await bundle(hass, language, "config")

    for step, fields in EXPECTED_CONFIG_FIELDS.items():
        prefix = f"component.{DOMAIN}.config.step.{step}"
        assert f"{prefix}.title" in payload, f"{step} has no title in {language}"
        for field in fields:
            label = payload.get(f"{prefix}.data.{field}")
            assert label, f"{step}.{field} has no label in {language}"
            assert label != field, f"{step}.{field} label is just the key"
            assert label.strip() == label

            description = payload.get(f"{prefix}.data_description.{field}")
            assert description, f"{step}.{field} has no description in {language}"
            assert description != field


@pytest.mark.parametrize("language", LANGUAGES)
async def test_every_options_field_has_a_label_and_a_description(
    hass: HomeAssistant, language: str
) -> None:
    """The options form is translated as completely as the config flow."""
    payload = await bundle(hass, language, "options")

    menu_prefix = f"component.{DOMAIN}.options.step.init"
    assert f"{menu_prefix}.title" in payload
    for page in EXPECTED_OPTIONS_MENU:
        entry = payload.get(f"{menu_prefix}.menu_options.{page}")
        assert entry, f"menu option {page} has no label in {language}"
        assert entry != page

    for step, fields in EXPECTED_OPTIONS_FIELDS.items():
        prefix = f"component.{DOMAIN}.options.step.{step}"
        assert f"{prefix}.title" in payload, f"{step} has no title in {language}"
        for field in fields:
            label = payload.get(f"{prefix}.data.{field}")
            assert label, f"{step}.{field} has no label in {language}"
            assert label != field

            description = payload.get(f"{prefix}.data_description.{field}")
            assert description, f"{step}.{field} has no description in {language}"


@pytest.mark.parametrize("language", LANGUAGES)
async def test_every_error_and_abort_is_translated(
    hass: HomeAssistant, language: str
) -> None:
    """A validation failure never shows the user an internal key."""
    config = await bundle(hass, language, "config")
    options = await bundle(hass, language, "options")

    for key in CONFIG_ERRORS:
        message = config.get(f"component.{DOMAIN}.config.error.{key}")
        assert message, f"config error {key} missing in {language}"
        assert message != key

    for key in OPTIONS_ERRORS:
        message = options.get(f"component.{DOMAIN}.options.error.{key}")
        assert message, f"options error {key} missing in {language}"
        assert message != key

    for key in ABORTS:
        message = config.get(f"component.{DOMAIN}.config.abort.{key}")
        assert message, f"abort {key} missing in {language}"
        assert message != key


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_sign_convention_options_are_translated(
    hass: HomeAssistant, language: str
) -> None:
    """The sign dropdowns show prose, not ``negative_is_charge``.

    Getting a sign convention wrong silently corrupts every learned figure, so
    these two dropdowns are the most safety-relevant text in the integration.
    """
    payload = await bundle(hass, language, "selector")

    for selector_key, options in (
        ("battery_power_sign", BATTERY_SIGN_OPTIONS),
        ("grid_power_sign", GRID_SIGN_OPTIONS),
    ):
        for option in options:
            key = f"component.{DOMAIN}.selector.{selector_key}.options.{option}"
            label = payload.get(key)
            assert label, f"{selector_key}.{option} missing in {language}"
            assert label != option


async def test_the_setup_failure_message_is_translated_in_both_languages(
    hass: HomeAssistant,
) -> None:
    """``ConfigEntryError`` resolves through the ``exceptions`` category.

    ``async_setup_entry`` raises it with ``translation_key`` when no house-load
    source is configured, so a missing entry leaves the user with a raw key
    instead of a sentence. The category was covered by nothing.
    """
    for language in ("en", "nl"):
        payload = await bundle(hass, language, "exceptions")
        key = f"component.{DOMAIN}.exceptions.house_load_source_missing.message"
        message = payload.get(key)
        assert message, f"house_load_source_missing missing in {language}"
        assert len(message) > 20


async def test_both_languages_expose_the_same_keys(hass: HomeAssistant) -> None:
    """Dutch and English never drift apart.

    ``entity`` joined the list in beta.19. It was absent before because the
    integration had no ``entity`` block at all -- so every enum state rendered as
    its raw value -- and a category nobody declares is a category this guard
    silently does not cover.
    """
    for category in ("config", "options", "selector", "exceptions", "entity"):
        english = own_keys(await bundle(hass, "en", category))
        dutch = own_keys(await bundle(hass, "nl", category))
        assert dutch == english, f"{category} differs between en and nl"


async def test_the_control_mode_reads_live_while_its_value_stays_active(
    hass: HomeAssistant,
) -> None:
    """The label moved to "Live". The stored value did not.

    Two different things, and conflating them would rename a value that a
    restored entity, every stored document and every test already use. The
    caption is the part that should be clear; the value is the part that must be
    stable.
    """
    from custom_components.alpha_ems_manager.const import (
        CONTROL_MODE_ACTIVE,
        CONTROL_MODE_OPTIONS,
    )

    assert CONTROL_MODE_ACTIVE == "active"
    assert CONTROL_MODE_OPTIONS == ("off", "shadow", "active")

    for language, expected in (("en", "Live"), ("nl", "Live")):
        states = await bundle(hass, language, "entity")
        key = "component.alpha_ems_manager.entity.select.control_mode.state.active"
        assert states.get(key) == expected, states
    english = await bundle(hass, "en", "entity")
    assert (
        english["component.alpha_ems_manager.entity.select.control_mode.state.shadow"]
        == "Shadow"
    )


async def test_dutch_is_actually_translated(hass: HomeAssistant) -> None:
    """The Dutch bundle is a translation, not a copy of the English one."""
    english = await bundle(hass, "en", "config")
    dutch = await bundle(hass, "nl", "config")

    shared = own_keys(english) & own_keys(dutch)
    identical = [key for key in shared if english[key] == dutch[key]]

    # A handful of proper nouns legitimately match; most strings must differ.
    assert len(identical) < len(shared) * 0.2, (
        f"{len(identical)} of {len(shared)} Dutch strings are identical to English"
    )
