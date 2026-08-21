"""Prices exist, and change nothing. The structural half and the behavioural half.

Two kinds of evidence here, and the order matters.

**Structural.** The price layer is not imported by the decision layer, no
identifier in the decision layer is an economic term, and no lifecycle probe
survives anywhere in the package. These cannot pass vacuously and cannot be
satisfied by a coefficient that happens to be zero today.

**Behavioural.** A fully healthy price source, driven through the mode that would
execute if execution were available, produces the same battery figures and zero
commands. This is belt-and-braces: the structural half already makes it true, and
the behavioural half is what a reader can check against a real installation.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.alphaess_device import OWNERSHIP_PROVABLE
from custom_components.alpha_ems_manager.const import (
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_MODE_ACTIVE,
    FRANK_FORBIDDEN_SERVICES,
)

from .conftest import FakeFrank
from .forecast_helpers import NORMAL, local, refresh_at
from .frank_capture import synthetic_day
from .test_control_modes import set_mode
from .test_price_capability import TOMORROW, drive

COMPONENT_DIR = Path("custom_components/alpha_ems_manager")

#: The modules that decide what the battery does. None of them may know a price.
DECISION_MODULES = (
    "plan",
    "policy",
    "simulation",
    "battery",
    "control",
    "safety",
    # Phase 7. It computes a physical requirement and nothing else, so it belongs
    # on this list for the same reason the six above it do -- and the guard
    # existed before the module did, which is a stronger statement than a
    # behavioural comparison could make.
    "reserve",
)

#: The price layer.
PRICE_MODULES = {"price_forecast", "frank_source"}


def local_imports(path: Path) -> set[str]:
    """Return the sibling modules a file imports, from its syntax tree."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            found.add(node.module.split(".")[0])
    return found


# --- structural: the price layer is unreachable from a decision --------------


@pytest.mark.parametrize("module", DECISION_MODULES)
def test_no_decision_module_imports_the_price_layer(module: str) -> None:
    """Not "does not use it" -- cannot reach it.

    An earlier draft of this phase proposed carrying a price on the trajectory
    interval and proving with a regression test that the recommendation was
    unchanged. That was rejected: the PV field was added because the simulator
    consumed it, whereas a price field would have no consumer at all. A field with
    no consumer is an invitation, and it converts something the structure
    guarantees for free into something a test has to keep checking.
    """
    imported = local_imports(COMPONENT_DIR / f"{module}.py")

    assert not imported & PRICE_MODULES, f"{module} imports {imported & PRICE_MODULES}"


def test_the_import_guard_can_actually_fail(tmp_path: Path) -> None:
    """A guard that cannot fail is decoration."""
    offender = tmp_path / "offender.py"
    offender.write_text("from .price_forecast import PriceForecast\n", encoding="utf-8")

    assert local_imports(offender) & PRICE_MODULES


def test_the_price_layer_reaches_no_decision_module() -> None:
    """And the converse: the price modules do not import the decision layer.

    Belt-and-braces on the same boundary from the other side, so a future author
    cannot satisfy the rule above by inverting the dependency.
    """
    for module in sorted(PRICE_MODULES):
        imported = local_imports(COMPONENT_DIR / f"{module}.py")
        assert not imported & set(DECISION_MODULES)


def test_no_module_probes_another_integrations_entry_lifecycle() -> None:
    """``ConfigEntryState`` appears nowhere in the package. Deliberately.

    It was imported here for exactly one purpose -- asking whether a consumed
    integration's config entry was ``LOADED`` -- and that question produced a live
    false negative on every restart, twice. The import is gone with the probe, so
    reintroducing the pattern means reintroducing the import, and this fails.
    """
    for path in COMPONENT_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "ConfigEntryState", path.name
            elif isinstance(node, ast.Attribute):
                assert node.attr != "ConfigEntryState", path.name


def test_the_price_boundary_reads_no_config_entry_internals() -> None:
    """The price probe touches neither ``state`` nor ``runtime_data``.

    ``runtime_data`` would be the more direct route to the source's coordinator
    and is refused on purpose: it is private, it is deleted on unload, and it is
    not the interface another integration is entitled to plan against. Published
    entity state is.
    """
    tree = ast.parse((COMPONENT_DIR / "frank_source.py").read_text(encoding="utf-8"))

    reached: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if isinstance(node.value, ast.Name):
            reached.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node.value, ast.Attribute):
            reached.add(f"{node.value.attr}.{node.attr}")

    # Matched on the *pair*, not on the attribute name alone. ``state.state`` is
    # read constantly here -- that is an entity's published value, which is the
    # whole interface -- so a bare search for "state" would fire on the correct
    # code and get silenced rather than fixed. This project has done that before.
    assert "entry.state" not in reached
    assert "entry.runtime_data" not in reached
    assert not any(pair.endswith(".runtime_data") for pair in reached)

    # ``entry.data`` *is* read, and only for the country the market timezone
    # comes from -- a documented field of the entry, not an internal.
    assert "entry.data" in reached
    assert "state.state" in reached


def test_no_forbidden_source_action_is_named_anywhere_in_the_package() -> None:
    """Belt-and-braces over a property that is already structural.

    Prices are read from state, so this phase adds no service caller at all --
    there is no call site that could name one of these. Named anyway, because the
    cost is a string search and the failure mode it guards against is a future
    author adding a "just refresh it" call.
    """
    for path in COMPONENT_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        # Read from the syntax tree's *string constants*, not the file text, so
        # the const module that names them for this guard is not itself a hit --
        # and prose explaining the rule cannot trip it. This project has silenced
        # a guard that fired on its own documentation before.
        offending = called & set(FRANK_FORBIDDEN_SERVICES)
        if path.name == "const.py":
            continue
        assert not offending, f"{path.name} names {sorted(offending)}"


#: Terms that would mean an economic quantity had arrived on a decision object.
ECONOMIC_WORDS = ("price", "tariff", "cost", "arbitrage", "cheap", "expensive", "eur")


def economic_fields(value: object, depth: int = 0) -> set[str]:
    """Return the economic-looking field names reachable from a decision object.

    Walks the dataclass tree by field *name*, so a value that merely contains one
    of the words -- a timezone, an entity id -- cannot produce a hit. Depth is
    bounded because the plan holds a trajectory of interval objects and the point
    is the shape, not the size.
    """
    if depth > 4 or not is_dataclass(value) or isinstance(value, type):
        return set()
    found: set[str] = set()
    for field in fields(value):
        lowered = field.name.lower()
        found |= {word for word in ECONOMIC_WORDS if word in lowered}
        attribute = getattr(value, field.name, None)
        if is_dataclass(attribute):
            found |= economic_fields(attribute, depth + 1)
        elif isinstance(attribute, (list, tuple)) and attribute:
            found |= economic_fields(attribute[0], depth + 1)
    return found


def test_the_field_name_guard_can_actually_fail() -> None:
    """Proved able to see an offending field before being trusted to say none."""

    @dataclass
    class Offender:
        import_price_eur_kwh: float = 0.0

    assert economic_fields(Offender()) == {"price", "eur"}


# --- behavioural: the figures do not move ------------------------------------


async def test_a_healthy_price_source_changes_no_battery_figure(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Both days published, and the battery plan is byte-for-byte the same.

    The comparison is against the same installation with the price source
    unreadable, so the only difference between the two runs is whether prices
    were available.
    """
    coordinator = setup_integration.runtime_data

    frank.publish(today=None, tomorrow=None)
    await drive(coordinator, hour=9)
    blind = coordinator.data["battery_plan"]
    assert coordinator.price_forecasts[NORMAL].available is False

    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))
    await refresh_at(coordinator, local(NORMAL, 9, 5))
    priced = coordinator.data["battery_plan"]

    assert coordinator.price_forecasts[NORMAL].available is True
    assert coordinator.price_forecasts[NORMAL].intervals_known == 96
    assert priced == blind


async def test_a_healthy_price_source_in_active_mode_still_commands_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank: FakeFrank,
    control_surface: None,
) -> None:
    """The whole point of the phase, stated as an executable claim.

    A fully healthy price source, both days published, driven through ``ACTIVE``
    -- the mode that would write if writing were possible -- and not one service
    call leaves the integration. Execution is unavailable at build time and
    ownership is unproven, and prices do not change either.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))

    # Selected through the real entity, before the patch: choosing the mode is
    # itself a service call, and counting it would defeat the measurement.
    await set_mode(hass, CONTROL_MODE_ACTIVE)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", autospec=True
    ) as async_call:
        await drive(coordinator, hour=9)

    assert async_call.await_count == 0
    assert coordinator.data["control"]["mode"] == CONTROL_MODE_ACTIVE
    assert CONTROL_EXECUTION_AVAILABLE is False
    assert OWNERSHIP_PROVABLE is False
    assert coordinator.price_forecasts[NORMAL].available is True


async def test_the_published_payload_carries_prices_and_no_decision_reads_them(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The series is published for diagnostics and evidence, and consumed nowhere.

    The plan is built from load, PV, state of charge and limits. It is not passed
    a price, which is why the two keys below can sit in the same payload without
    one being able to influence the other.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))
    await drive(coordinator, hour=9)

    payload = coordinator.data
    assert payload["price_today"].available is True
    assert payload["price_tomorrow"].available is True

    # Checked over the plan's **field names**, recursively, rather than over its
    # repr. A substring search on rendered values looks stronger and is worse:
    # the plan carries the timezone ``Europe/Amsterdam``, which contains "eur",
    # so the scan fails on a correct plan. Two earlier guards in this project
    # fired on their own prose, and both got weakened instead of corrected.
    assert not economic_fields(payload["battery_plan"])
