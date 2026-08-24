"""Stage B with the integration actually running: it computes, and writes nothing.

The pure tests establish what the controller decides. These establish that it is
wired in, that the diagnostics block a live installation will be read from is
populated, and -- the part that matters most before beta.20 -- that a full Shadow
day produces **zero** writes and never acquires the owner marker.

Service handlers are registered for real, so a write attempt would succeed and be
recorded rather than raising. Otherwise an attempted call could be mistaken for an
absent service and the test would pass for the wrong reason.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    PERMITTED_SERVICES,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
    STORAGE_MINOR_VERSION,
)

from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
from .frank_capture import synthetic_day
from .test_control_modes import set_mode
from .test_economic_published import allow_trading

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def writes(hass: HomeAssistant) -> list:
    """Capture every call to a service this integration may make.

    Real handlers, so a write would land rather than raise. The marker is
    ``input_boolean.turn_on``, which is already permitted -- so if Shadow ever
    acquired ownership it would show up here rather than as an error.
    """
    calls: list = []

    async def record(call) -> None:
        calls.append(call)

    for domain, service in PERMITTED_SERVICES:
        hass.services.async_register(domain, service, record)
    assert len(set(PERMITTED_SERVICES)) == 3
    return calls


async def prepared(
    hass: HomeAssistant, entry: MockConfigEntry, frank, mode: str
) -> object:
    """Return a coordinator with prices, history and a mode, ready to refresh."""
    coordinator = entry.runtime_data
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await set_mode(hass, mode)
    return coordinator


# ===========================================================================
# A. the runtime zero-actuation proof
# ===========================================================================


async def test_a_shadow_day_writes_nothing_and_never_claims_ownership(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """**The proof beta.20 will be approved against.**

    Eight quarter-hours with the whole controller running, both opt-ins on, and the
    most permissive mode this release can reach. Asserted positively as well as
    negatively: the controller must actually have been working, or the zero below
    proves nothing.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)

    reports = []
    for quarter in range(8):
        await refresh_at(
            coordinator, local(NORMAL, 10 + quarter // 4, (quarter % 4) * 15)
        )
        report = (coordinator.control_report or {}).get("execution") or {}
        reports.append(report)

    # It was working: a target was seen and a state reached.
    assert any(report.get("plan_id") for report in reports), reports
    assert any(report.get("state") for report in reports)

    # And it wrote nothing at all.
    assert writes == []
    assert CONTROL_EXECUTION_AVAILABLE is False
    # Including the marker, which is the write that would create a claim.
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"
    # And the two fields a send would set are still untouched.
    assert coordinator._last_control_write is None
    assert coordinator._last_control_power_kw is None


async def test_the_execution_block_says_nothing_was_applied(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """A reader must not have to infer it from the absence of something."""
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    await refresh_at(coordinator, local(NORMAL, 10, 30))

    report = (coordinator.control_report or {}).get("execution") or {}

    assert report
    assert "controls_nothing" in report
    power = report.get("power")
    if power is not None:
        assert power["applied_kw"] == 0.0
        assert power["executed"] is False
    assert writes == []


async def test_the_diagnostics_download_carries_the_execution_block(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
) -> None:
    """It is the surface the live installation will be validated from."""
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_SHADOW)
    await refresh_at(coordinator, local(NORMAL, 10, 30))

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert "execution" in payload
    block = payload["execution"]
    assert block.get("mode") == CONTROL_MODE_SHADOW
    assert "ownership" in block
    assert "safety" in block
    assert block["safety"]["ownership_marker_entity"] == BOOLEAN_EXECUTION_OWNER


async def test_off_runs_no_controller_and_writes_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """Off short-circuits the whole report, as it did before Stage B existed."""
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_OFF)
    await refresh_at(coordinator, local(NORMAL, 10, 30))

    assert writes == []
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"


async def test_no_dispatch_helper_is_touched_across_a_shadow_day(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """Stated over the helpers themselves, not over the call list.

    A write that somehow bypassed the recorded services would still show up as a
    changed helper state, so this is the belt to the call list's braces.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    watched = [
        *CHARGE_FAMILY.entities,
        *DISCHARGE_FAMILY.entities,
        BOOLEAN_EXECUTION_OWNER,
    ]
    before = {entity: hass.states.get(entity).state for entity in watched}

    for quarter in range(4):
        await refresh_at(coordinator, local(NORMAL, 11, quarter * 15))

    after = {entity: hass.states.get(entity).state for entity in watched}

    assert after == before


# ===========================================================================
# B. mode transitions
# ===========================================================================


@pytest.mark.parametrize(
    "sequence",
    [
        (CONTROL_MODE_OFF, CONTROL_MODE_SHADOW),
        (CONTROL_MODE_SHADOW, CONTROL_MODE_ACTIVE),
        (CONTROL_MODE_ACTIVE, CONTROL_MODE_SHADOW),
        (CONTROL_MODE_ACTIVE, CONTROL_MODE_OFF),
        (CONTROL_MODE_SHADOW, CONTROL_MODE_OFF),
    ],
)
async def test_every_mode_transition_writes_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
    sequence: tuple[str, str],
) -> None:
    """Including the two that would stop an owned run if one existed.

    There is no owned run to stop, because Shadow never acquires one -- so the
    transitions are exercised for their refusal rather than for their effect, which
    is exactly what beta.19 can prove.
    """
    first, second = sequence
    coordinator = await prepared(hass, setup_integration, frank, first)
    await refresh_at(coordinator, local(NORMAL, 12, 0))
    await set_mode(hass, second)
    await refresh_at(coordinator, local(NORMAL, 12, 15))

    assert writes == []
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"


async def test_a_foreign_dispatch_is_reported_and_left_alone(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """**Someone armed the inverter by hand.**

    The marker is off and a dispatch is running, so it is somebody else's. Alpha
    EMS reports the fact and touches nothing -- no command, no reset, no marker.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    hass.states.async_set(DISCHARGE_FAMILY.activate, "on")
    hass.states.async_set("sensor.alphaess_dispatch_start", 1)

    await refresh_at(coordinator, local(NORMAL, 12, 30))

    report = (coordinator.control_report or {}).get("execution") or {}

    assert report["ownership"]["state"] in ("foreign", "unproven")
    assert report["result"]["reset_required"] is False
    assert writes == []
    # The foreign dispatch is untouched.
    assert hass.states.get(DISCHARGE_FAMILY.activate).state == "on"


# ===========================================================================
# C. Activity: a lifecycle, not a control-loop log
# ===========================================================================


async def test_a_long_run_does_not_produce_a_line_every_quarter(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """**The requirement that the feed is not a fifteen-minute log.**

    Twelve refreshes across three hours. Routine rolling corrections must be
    silent, so the number of distinct lines has to be far below the number of
    refreshes -- and no line may repeat.
    """
    from homeassistant.const import EVENT_LOGBOOK_ENTRY

    logbook: list = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: logbook.append(event.data))
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)

    for quarter in range(12):
        await refresh_at(
            coordinator, local(NORMAL, 10 + quarter // 4, (quarter % 4) * 15)
        )

    messages = [entry["message"] for entry in logbook]

    # Something was said, or the silence proves nothing.
    assert messages
    # Nothing was said twice.
    assert len(messages) == len(set(messages)), messages
    # And far fewer lines than refreshes.
    assert len(messages) < 12, messages
    assert writes == []


async def test_no_shadow_line_ever_claims_a_command_was_sent(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """The one thing this surface must never say."""
    from homeassistant.const import EVENT_LOGBOOK_ENTRY

    logbook: list = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: logbook.append(event.data))
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)

    for quarter in range(6):
        await refresh_at(
            coordinator, local(NORMAL, 13 + quarter // 4, (quarter % 4) * 15)
        )

    for entry in logbook:
        message = entry["message"].lower()
        assert "dispatch started" not in message
        assert "dispatch stopped" not in message
        assert any(
            phrase in message
            for phrase in ("advisory only", "no command sent", "no command was sent")
        ), entry["message"]
    assert writes == []


# ===========================================================================
# D. persistence and restart
# ===========================================================================


async def test_the_published_revisions_are_remembered(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
) -> None:
    """A reboot must not tell Stage B that every target is brand new."""
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_SHADOW)
    await refresh_at(coordinator, local(NORMAL, 10, 30))

    if not coordinator.execution_targets:
        pytest.skip("no execution target for this fixture")

    assert coordinator.store.execution_revisions
    for plan_id, remembered in coordinator.store.execution_revisions.items():
        assert remembered["plan_id"] == plan_id
        assert remembered["revision"] >= 1
        # Only what the revision comparison needs -- not the plan, not the
        # progress, not the economics.
        assert set(remembered) == {
            "plan_id",
            "revision",
            "intent",
            "battery_target_kwh",
            "grid_target_kwh",
            "window_end",
        }


async def test_the_document_declares_the_new_minor_and_stays_readable(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    hass_storage,
) -> None:
    """Additive: minor bumped, major unchanged, old documents still load."""
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_SHADOW)
    await refresh_at(coordinator, local(NORMAL, 10, 30))
    await coordinator.store.async_save_now()

    document = hass_storage[f"alpha_ems_manager.{setup_integration.entry_id}.learning"]

    assert document["version"] == 2
    assert document["minor_version"] == STORAGE_MINOR_VERSION == 5
    # The learning history is untouched by any of this.
    assert "days" in document["data"]


async def test_a_document_without_the_execution_key_still_loads(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Every beta.18 and earlier document. Absence means nothing was running."""
    coordinator = setup_integration.runtime_data

    assert coordinator.store.execution_record is None
    assert isinstance(coordinator.store.execution_revisions, dict)


async def test_progress_is_not_replayed_from_a_stored_target(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
) -> None:
    """**"Ten kilowatt-hours before the reboot and another ten after."**

    What is persisted is the revision and the causal record. Progress is not, and
    must not be: it is re-measured from the state-of-charge series, which is the
    only basis a restart can trust.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_SHADOW)
    await refresh_at(coordinator, local(NORMAL, 10, 30))
    await coordinator.store.async_save_now()

    remembered = coordinator.store.execution_revisions
    for entry in remembered.values():
        for forbidden in (
            "battery_realized_kwh",
            "realized_kwh",
            "delivered_kwh",
            "progress",
        ):
            assert forbidden not in entry
