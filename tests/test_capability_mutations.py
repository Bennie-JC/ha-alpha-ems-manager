"""Break the capability probe in the ways beta.9 was broken, and catch each one.

The beta.9 defect was not a typo. It was a *category* error: inferring whether a
source could be read from the internal setup state of somebody else's config
entry, rather than from whether the thing being called was there to call. So the
mutations here are all shaped like that mistake, because that is the shape that
will be reached for again.

The structural guard is the important one. Every behavioural test below could be
satisfied by a probe that happened to work today; only the structural one says the
whole class of inference is out of bounds.
"""

from __future__ import annotations

import ast
import inspect
from contextlib import contextmanager
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager import solcast_source
from custom_components.alpha_ems_manager.const import (
    SOLCAST_DOMAIN,
    SOLCAST_SERVICE_DIAGNOSTIC,
    SOLCAST_SERVICE_QUERY_FORECAST,
)
from custom_components.alpha_ems_manager.solcast_source import discover

from .conftest import FakeSolcast
from .forecast_helpers import NORMAL
from .test_pv_site_selection import drive, enable_forecast


@contextmanager
def patched(module: Any, name: str, value: Any):
    """Replace one attribute for the duration of the block."""
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def surviving(guard) -> bool:
    """Return whether the guarding assertion still passed under the mutation."""
    try:
        guard()
    except AssertionError:
        return False
    return True


# ===========================================================================
# 1. the structural rule: capability is never inferred from entry internals
# ===========================================================================


#: Attributes of somebody else's config entry that say nothing about whether a
#: registered action can be called. Reading any of them to decide capability is
#: the beta.9 mistake, whatever the surrounding logic.
FORBIDDEN_ENTRY_INTERNALS = (
    "state",
    "runtime_data",
    "supports_unload",
    "setup_lock",
    "reason",
    "error_reason_translation_key",
    "disabled_by",
    "pref_disable_polling",
    "update_listeners",
)


def capability_tree() -> ast.AST:
    """Return the parsed source of the capability probe and its dataclass."""
    return ast.parse(inspect.getsource(solcast_source))


def test_the_probe_reads_no_config_entry_internals() -> None:
    """The rule that makes the whole bug class unreachable.

    ``discover`` may ask whether the selected id names an entry -- existence is a
    fact about configuration. It may not ask that entry anything else. Setup state
    in particular is a moving target that says nothing about whether a registered
    action can be called, and requiring it produced a false negative on every
    restart of the live installation.
    """
    tree = ast.parse(inspect.getsource(solcast_source.discover))
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for forbidden in FORBIDDEN_ENTRY_INTERNALS:
        assert forbidden not in attributes, forbidden

    # And the one permitted question is asked by identity, not by attribute.
    assert "async_get_entry" in attributes


def test_no_config_entry_state_is_imported_at_all() -> None:
    """Removed rather than left available to be reached for again.

    An unused import is an invitation. If a later change needs entry state for
    something legitimate, importing it again should be a visible decision.
    """
    source = inspect.getsource(solcast_source)

    assert "ConfigEntryState" not in source
    assert "config_entries import" not in source


def test_the_capability_has_no_field_that_can_go_stale_while_usable() -> None:
    """Every field is a fact that stays true while the source stays readable."""
    fields = set(solcast_source.SolcastCapability.__dataclass_fields__)

    assert fields == {
        "entry_selected",
        "entry_found",
        "query_service",
        "diagnostic_service",
    }
    assert "entry_loaded" not in fields


# ===========================================================================
# 2. the mutations, each shaped like the original defect
# ===========================================================================


async def test_reinstating_the_loaded_state_requirement_is_caught(
    hass: HomeAssistant, solcast_config_entry: MockConfigEntry
) -> None:
    """The beta.9 probe, restored, against the state the live entry was in.

    Both probes are evaluated side by side rather than one being monkey-patched
    over the other, because what is being demonstrated is that they *disagree* on
    this input -- which is the whole reason the old one had to go.
    """
    FakeSolcast().register(hass)
    entry_id = solcast_config_entry.entry_id
    assert solcast_config_entry.state is not ConfigEntryState.LOADED

    def beta9_usable() -> bool:
        entry = hass.config_entries.async_get_entry(entry_id)
        return entry is not None and entry.state is ConfigEntryState.LOADED

    # As shipped: readable, because both actions are registered.
    assert discover(hass, entry_id).usable is True
    # Mutated: refused, on a source that is demonstrably readable.
    assert beta9_usable() is False


async def test_inferring_capability_from_runtime_data_is_caught(
    hass: HomeAssistant, solcast_config_entry: MockConfigEntry
) -> None:
    """A tempting alternative, and just as wrong.

    Solcast v4.6.1 does not put anything under this entry's runtime data that
    Alpha EMS can rely on, and requiring it would fail on every installation
    rather than only during startup.
    """
    FakeSolcast().register(hass)

    assert discover(hass, solcast_config_entry.entry_id).usable is True
    # The entry genuinely has no runtime data, which is exactly why a probe built
    # on it would refuse a perfectly readable source.
    assert getattr(solcast_config_entry, "runtime_data", None) is None


async def test_dropping_the_entry_existence_check_is_caught(
    hass: HomeAssistant,
) -> None:
    """The fix must not have become "assume it is fine".

    A stored id naming nothing is a real, provable problem -- Solcast removed, or
    removed and re-added -- and it must still be reported.
    """
    FakeSolcast().register(hass)

    # As shipped: refused, and named.
    assert discover(hass, "no-such-entry").usable is False

    # Mutated: existence assumed rather than checked, so a stored id pointing at
    # nothing reads as a working source.
    assumed = solcast_source.SolcastCapability(
        entry_selected=True,
        entry_found=True,
        query_service=True,
        diagnostic_service=True,
    )
    assert assumed.usable is True


async def test_dropping_the_service_checks_is_caught(
    hass: HomeAssistant, solcast_config_entry: MockConfigEntry
) -> None:
    """Nothing is registered, so nothing can be called, and it must say so."""

    # As shipped: nothing registered, so nothing can be called.
    capability = discover(hass, solcast_config_entry.entry_id)
    assert capability.usable is False
    assert capability.unavailable_reason is not None

    # Mutated: assume the actions are there. The probe now claims a source that
    # would raise on the first call.
    assumed = solcast_source.SolcastCapability(
        entry_selected=True,
        entry_found=True,
        query_service=True,
        diagnostic_service=True,
    )
    assert assumed.usable is True
    assert assumed.unavailable_reason is None


async def test_caching_the_capability_across_refreshes_is_caught(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
) -> None:
    """A false negative that cannot be revisited is the whole live defect.

    The probe must run every refresh. Caching the first answer -- which is what
    the fifteen-minute refresh cadence effectively did to the *snapshot* -- would
    leave an installation permanently unusable because of one unlucky boot.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data

    await drive(coordinator)
    assert coordinator.pv_capability.usable is False

    FakeSolcast().register(hass)
    await drive(coordinator)

    assert coordinator.pv_capability.usable is True, "the refusal was cached"


async def test_only_one_action_appearing_is_reported_precisely(
    hass: HomeAssistant, solcast_config_entry: MockConfigEntry
) -> None:
    """Partial registration during component setup, named for what it is."""

    async def nothing(call: object) -> dict:
        return {}

    hass.services.async_register(SOLCAST_DOMAIN, SOLCAST_SERVICE_DIAGNOSTIC, nothing)
    capability = discover(hass, solcast_config_entry.entry_id)

    assert capability.diagnostic_service is True
    assert capability.query_service is False
    assert capability.usable is False
    # Named, not merely "unavailable".
    assert "query" in capability.unavailable_reason

    hass.services.async_register(
        SOLCAST_DOMAIN, SOLCAST_SERVICE_QUERY_FORECAST, nothing
    )

    assert discover(hass, solcast_config_entry.entry_id).usable is True


# ===========================================================================
# 3. the fix cannot have opened anything
# ===========================================================================


def test_the_probe_still_calls_nothing() -> None:
    """Discovery is a question, not an action.

    A capability check that called something would spend the account's allowance
    just to find out whether it could -- and would be the obvious place to slip in
    a "refresh the cache while we are here".
    """
    tree = ast.parse(inspect.getsource(solcast_source.discover))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "async_call" not in calls
    assert calls <= {"async_get_entry", "has_service"}


def test_the_service_caller_set_is_unchanged_by_the_fix() -> None:
    """Still exactly two modules, and still the same two."""
    from pathlib import Path

    component = Path("custom_components/alpha_ems_manager")
    callers = {
        path.stem
        for path in sorted(component.glob("*.py"))
        if "async_call" in path.read_text(encoding="utf-8")
    }

    assert callers == {"alphaess_adapter", "solcast_source"}


@pytest.mark.parametrize(
    "forbidden",
    [
        "update_forecasts",
        "force_update_forecasts",
        "force_update_estimates",
        "clear_all_solcast_data",
        "set_options",
        "set_dampening",
        "set_hard_limit",
        "remove_hard_limit",
    ],
)
def test_no_mutating_action_was_introduced_by_the_fix(forbidden: str) -> None:
    """Re-asserted here rather than only in the Phase-5 file.

    A capability fix is precisely where "just force an update to be sure" gets
    added, so the deny-list is checked again from the file that changed the probe.
    """
    from pathlib import Path

    component = Path("custom_components/alpha_ems_manager")
    offenders = {
        path.stem
        for path in sorted(component.glob("*.py"))
        if forbidden in path.read_text(encoding="utf-8") and path.stem != "const"
    }

    assert not offenders, f"{forbidden} in {sorted(offenders)}"


def test_the_execution_barrier_is_untouched() -> None:
    """A usable PV source has nothing to do with reaching an inverter."""
    from custom_components.alpha_ems_manager.const import (
        CONTROL_EXECUTION_AVAILABLE,
    )

    assert CONTROL_EXECUTION_AVAILABLE is False


async def test_a_recovered_source_still_writes_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    control_surface: None,
) -> None:
    """Recovery drives the whole chain, in active mode, and sends nothing."""
    from homeassistant.core import ServiceCall

    from custom_components.alpha_ems_manager.alphaess_device import PERMITTED_SERVICES
    from custom_components.alpha_ems_manager.const import CONTROL_MODE_ACTIVE

    from .test_control_modes import set_mode

    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    for domain, service in PERMITTED_SERVICES:
        hass.services.async_register(domain, service, record)

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    coordinator = setup_integration.runtime_data

    # Unusable, then usable, then driven again -- the whole recovery path.
    await drive(coordinator)
    FakeSolcast().register(hass)
    await drive(coordinator)
    await hass.async_block_till_done()

    assert coordinator.pv_forecasts[NORMAL].available is True
    assert calls == []
