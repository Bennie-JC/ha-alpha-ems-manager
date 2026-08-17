"""Alpha EMS Manager must never talk to an external service.

It is a fusion layer over entities other integrations already provide. If it
started polling the Frank, AlphaESS, Solcast or HomeWizard APIs itself it would
duplicate traffic, risk rate limits on the user's account, and defeat the whole
architectural point.

Both halves are checked: statically, that the source contains no HTTP client at
all, and dynamically, that a full setup plus a learning cycle opens no session.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alpha_ems_manager.const import DOMAIN

COMPONENT_DIR = Path("custom_components/alpha_ems_manager")

#: Modules that would let the integration reach the network.
FORBIDDEN_IMPORTS = (
    "aiohttp",
    "requests",
    "httpx",
    "urllib",
    "http.client",
    "websockets",
    "socket",
)

#: Hostnames belonging to the integrations whose entities are consumed.
FORBIDDEN_HOSTS = (
    "frankenergie.nl",
    "alphaess.com",
    "alphacloud",
    "solcast.com",
    "api.solcast",
    "homewizard",
)


def source_files() -> list[Path]:
    """Return every Python file in the integration."""
    return sorted(COMPONENT_DIR.glob("*.py"))


def test_the_integration_ships_python_files() -> None:
    """Guard the glob above against silently matching nothing."""
    assert len(source_files()) >= 8


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_module_imports_an_http_client(path: Path) -> None:
    """No source file imports anything capable of making a request."""
    source = path.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("import ", "from ")):
            continue
        for forbidden in FORBIDDEN_IMPORTS:
            assert not stripped.startswith(
                (f"import {forbidden}", f"from {forbidden}")
            ), f"{path.name} imports {forbidden}: {stripped}"


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_module_mentions_a_vendor_endpoint(path: Path) -> None:
    """No source file contains an API hostname for a consumed integration."""
    source = path.read_text(encoding="utf-8").lower()
    for host in FORBIDDEN_HOSTS:
        assert host not in source, f"{path.name} mentions {host}"


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_module_contains_a_url_literal(path: Path) -> None:
    """No source file contains a URL at all.

    Stronger than the vendor-name check above: this catches an endpoint for a
    service nobody thought to add to the list.
    """
    source = path.read_text(encoding="utf-8")

    assert "://" not in source, f"{path.name} contains a URL literal"


def test_the_manifest_declares_no_dependencies_and_no_polling() -> None:
    """An empty ``requirements`` list is the strongest static guarantee.

    No API client library is installed, so none can be called.
    """
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text("utf-8"))

    assert manifest["requirements"] == []
    assert manifest["iot_class"] == "calculated"
    assert "dependencies" not in manifest or manifest["dependencies"] == []


async def test_setup_and_a_learning_cycle_open_no_http_session(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """A full setup plus a finalised quarter makes no outbound request."""
    from .conftest import HOUSE_LOAD, TEST_TIMEZONE, set_sensor
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    with (
        patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession"
        ) as session,
        patch(
            "homeassistant.helpers.aiohttp_client.async_create_clientsession"
        ) as created,
    ):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        for _ in range(16):
            freezer.tick(timedelta(seconds=60))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

    session.assert_not_called()
    created.assert_not_called()

    # And the cycle really did run, so this is not a vacuous pass.
    coordinator = mock_config_entry.runtime_data
    assert coordinator.store.days[START.date()].measured_valid_count == 1


async def test_the_coordinator_has_no_polling_interval(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Updates are event-driven, so there is no background poll loop."""
    coordinator = setup_integration.runtime_data

    assert coordinator.update_interval is None


async def test_availability_is_read_from_the_config_entry_registry(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """Frank and Solcast presence is determined without contacting them.

    The entry is registered but never set up, so it is not loaded -- which is
    exactly what the coordinator should report.
    """
    coordinator = setup_integration.runtime_data

    assert coordinator.frank_available is False
    assert coordinator.solcast_available is False
    assert coordinator.config.frank_entry_id == frank_config_entry.entry_id


async def test_no_entity_from_a_consumed_integration_is_duplicated(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Alpha EMS never republishes a price, forecast or AlphaESS entity."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    ours = {
        entity.entity_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    }

    for entity_id in ours:
        assert "price" not in entity_id
        assert "solcast" not in entity_id
        assert "forecast_today" not in entity_id
        assert "alphaess" not in entity_id
