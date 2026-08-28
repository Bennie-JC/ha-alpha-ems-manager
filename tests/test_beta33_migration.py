"""A withdrawn setting must leave existing installations untouched.

``control_horizon_minutes`` was removed from the Control options page in beta.33.
Removing a field is the easy half; the half that breaks installations is what
happens to the value people already saved.

The rules, and each has a test below:

* the stored key **stays** -- nothing rewrites or deletes a user's options;
* the stored key is **never read** -- an old value cannot be quietly honoured,
  which is why the runtime field was deleted rather than merely hidden;
* **the config-entry version does not move.** ``async_migrate_entry`` is a
  deliberate refusal rather than a converter, so bumping it would make every
  existing entry fail to load. That is the one mistake here that would be
  catastrophic, and MIGRATE-004 exists to make it impossible to make quietly.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONF_CONTROL_EXPORT_MARGIN_PERCENT,
    CONF_CONTROL_HORIZON_MINUTES,
    CONFIG_ENTRY_VERSION,
    CONTROL_HORIZON_MINUTES,
    CONTROL_MODE_SHADOW,
    DOMAIN,
)

pytestmark = pytest.mark.usefixtures("control_surface")


async def entry_with(hass: HomeAssistant, config_data: dict, options: dict):
    """Set up an entry carrying exactly these options."""
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


@pytest.mark.parametrize("stored", [20, 25, 45, 60])
async def test_migrate_001_an_entry_carrying_the_withdrawn_key_still_loads(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, stored: int
) -> None:
    """MIGRATE-001. Every value the old selector offered still sets up cleanly."""
    entry = await entry_with(hass, config_data, {CONF_CONTROL_HORIZON_MINUTES: stored})

    assert entry.state.recoverable is False or entry.runtime_data is not None
    assert entry.runtime_data is not None
    assert entry.version == CONFIG_ENTRY_VERSION
    # The key is still there. Nothing rewrote the user's options.
    assert entry.options[CONF_CONTROL_HORIZON_MINUTES] == stored


async def test_migrate_002_a_stored_sixty_is_inert_at_the_control_intent(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank
) -> None:
    """MIGRATE-002. The value that would have mattered most, proven inert.

    Sixty was the top of the old range, so this is where a setting still being
    read would show. The control intent is where it used to land: through beta.32
    ``ControlIntent.horizon_minutes`` was the stored figure. It is now the internal
    constant regardless, and an entry storing sixty publishes twenty.

    *Mutation: restore the reader and this asserts 20 against a published 60.*
    """
    entry = await entry_with(hass, config_data, {CONF_CONTROL_HORIZON_MINUTES: 60})

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.alpha_ems_control_mode", "option": CONTROL_MODE_SHADOW},
        blocking=True,
    )
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    control = entry.runtime_data.data["control"]
    intent = control["intent"]
    assert intent is not None, "shadow must publish an intent to prove anything here"
    assert intent["horizon_minutes"] == CONTROL_HORIZON_MINUTES
    assert intent["horizon_minutes"] != 60

    # And there is no runtime field left for anything else to read.
    assert not hasattr(entry.runtime_data.config, "control_horizon_minutes")


async def test_migrate_003_an_entry_without_the_key_loads_on_the_constant(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank
) -> None:
    """MIGRATE-003. A fresh install has no key and needs none."""
    entry = await entry_with(hass, config_data, {})

    assert entry.runtime_data is not None
    assert CONF_CONTROL_HORIZON_MINUTES not in entry.options
    assert CONF_CONTROL_HORIZON_MINUTES not in entry.data


async def test_migrate_004_the_config_entry_version_must_not_move(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank
) -> None:
    """MIGRATE-004. **Bumping the version would brick every install.**

    ``async_migrate_entry`` refuses anything below the current version rather than
    converting it -- v1 and v2 share no configuration keys, so there is nothing to
    convert. Withdrawing a field changes no key that any entry depends on, so it
    needs no bump, and taking one would make every stored v2 entry fail to load.

    *Mutation: raise ``CONFIG_ENTRY_VERSION`` and this test fails.*
    """
    from custom_components.alpha_ems_manager import async_migrate_entry

    assert CONFIG_ENTRY_VERSION == 2

    entry = await entry_with(hass, config_data, {CONF_CONTROL_HORIZON_MINUTES: 35})
    assert entry.version == CONFIG_ENTRY_VERSION
    assert await async_migrate_entry(hass, entry) is True


async def test_migrate_005_saving_control_options_leaves_the_stale_key_alone(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank
) -> None:
    """MIGRATE-005. The page saves without the field and rewrites nothing else.

    The options write is a merge over existing options, so a key the schema no
    longer offers is carried forward untouched. Deleting it would be a rewrite of
    the user's stored configuration to no purpose.
    """
    entry = await entry_with(
        hass,
        config_data,
        {
            CONF_CONTROL_HORIZON_MINUTES: 45,
            CONF_CONTROL_EXPORT_MARGIN_PERCENT: 10,
            "something_unknown": "keep me",
        },
    )

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "control"}
    )
    assert flow["step_id"] == "control"
    assert CONF_CONTROL_HORIZON_MINUTES not in flow["data_schema"].schema

    await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            CONF_CONTROL_EXPORT_MARGIN_PERCENT: 25,
            "grid_charge_budget_kwh": 0.0,
            "control_execution_enabled": False,
        },
    )
    await hass.async_block_till_done()

    # The change landed, the withdrawn key survived, and the unrelated key with it.
    assert entry.options[CONF_CONTROL_EXPORT_MARGIN_PERCENT] == 25
    assert entry.options[CONF_CONTROL_HORIZON_MINUTES] == 45
    assert entry.options["something_unknown"] == "keep me"


def test_migrate_006_no_orphaned_label_survives_the_removal() -> None:
    """MIGRATE-006. Schema and both translation files agree it is gone.

    An orphaned label is how a withdrawn setting comes back looking configurable:
    the field vanishes from the form while its description stays in the strings,
    and the next reader assumes the form is what is broken.
    """
    import json
    import pathlib

    from custom_components.alpha_ems_manager import config_flow

    root = pathlib.Path(config_flow.__file__).parent
    assert "control_horizon_minutes" not in (root / "config_flow.py").read_text(
        encoding="utf-8"
    )

    for name in ("en", "nl"):
        text = (root / "translations" / f"{name}.json").read_text(encoding="utf-8")
        assert "control_horizon_minutes" not in text, name
        # Still valid JSON after the surgery, and the sibling fields survived.
        data = json.loads(text)
        assert data, name
        assert "control_export_margin_percent" in text, name
