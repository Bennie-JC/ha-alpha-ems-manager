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
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
    STORAGE_MINOR_VERSION,
)

from .conftest import BATTERY_SOC, set_sensor
from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
from .frank_capture import synthetic_day
from .live_capability import assert_charge_only_capability
from .test_beta24_live_charge import charge_now_price
from .test_control_modes import set_mode
from .test_economic_published import allow_trading

pytestmark = pytest.mark.usefixtures("control_surface")


async def prepared(
    hass: HomeAssistant, entry: MockConfigEntry, frank, mode: str
) -> object:
    """Return a coordinator with prices, history and a mode, ready to refresh."""
    coordinator = entry.runtime_data
    seed(coordinator, history_before(NORMAL))
    # **A pack that actually needs the energy it is about to buy.**
    #
    # The shared fixture starts at 55 %, which on this battery covers the whole
    # forecast evening -- so under beta.31 the correct plan is to *hold*, and these
    # suites would have nothing to execute. That was never visible before, because
    # the autonomy reserve made a purchase compulsory regardless of whether the
    # energy was needed or what it cost.
    #
    # Dropping to 30 % states the premise these suites always relied on: there is
    # more demand ahead than the pack holds, so buying in the cheap window is
    # genuinely the right answer and Stage B has a real charge to execute.
    set_sensor(hass, BATTERY_SOC, 30, "%", "battery")
    await hass.async_block_till_done()
    # beta.31: a price shape that gives the plan an economic *reason* to charge at
    # the moment these suites refresh. Until beta.31 the charge appeared because
    # the whole-horizon autonomy reserve made a purchase compulsory at any price;
    # reachability makes nothing compulsory while the pack can hold its floor, so a
    # fixture wanting a charge now has to say why. See ``charge_now_price``.
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
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
    assert_charge_only_capability()
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
    assert "execution_scope" in report
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

    Twelve refreshes across three hours, spanning two charge campaigns -- the first
    reaches its window end and a second is admitted. Routine rolling corrections
    must be silent.

    **Counted per run since beta.24, which is sharper than counting unique lines.**
    The old assertion was that no message repeated, and it was a proxy: with three
    lifecycle events keyed on ``run_id``, two campaigns whose figures happen to
    match legitimately produce the same sentence twice, ninety minutes apart. Two
    real events are not spam. What is actually forbidden is a *run* saying anything
    more than once, so that is what is asserted.
    """
    from homeassistant.const import EVENT_LOGBOOK_ENTRY

    logbook: list = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: logbook.append(event.data))
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)

    runs: list[str] = []
    for quarter in range(12):
        await refresh_at(
            coordinator, local(NORMAL, 10 + quarter // 4, (quarter % 4) * 15)
        )
        execution = (coordinator.control_report or {}).get("execution") or {}
        run_id = ((execution.get("carried") or {}).get("run") or {}).get("run_id")
        if isinstance(run_id, str) and run_id not in runs:
            runs.append(run_id)

    messages = [entry["message"] for entry in logbook]
    # **beta.31: every line is a plan lifecycle line**, so there is no longer an
    # advice surface to tell the execution surface apart from -- the Phase-3
    # sentences that churned with the reserve window are gone, and their churn with
    # them. The whole log is the thing under test.
    plans = {entry.get("plan_id") for entry in logbook if entry.get("plan_id")}

    # Something was said, or the silence proves nothing.
    assert messages
    assert plans
    # At most three lines per plan: planned once, started at most once, and one
    # terminal. A fourth would mean the lifecycle key had stopped identifying the
    # plan -- which is the failure this assertion exists to catch.
    assert len(messages) <= 3 * len(plans), messages
    # Nothing about the routine refreshes in between: twelve refreshes, and far
    # fewer lines than refreshes.
    assert len(messages) < 12, messages
    for plan_id in plans:
        lines = [e["message"] for e in logbook if e.get("plan_id") == plan_id]
        assert sum(1 for m in lines if " Planned — " in m) <= 1, lines
        assert sum(1 for m in lines if " Started — " in m) <= 1, lines
        assert sum(1 for m in lines if m.startswith(("Finished ", "Canceled "))) <= 1, (
            lines
        )
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
        # **Plan-lifecycle lines are not claims that anything was sent**, and
        # they are exempted structurally -- every one carries a plan id -- rather
        # than by loosening the phrase list. Such a line names a plan, a window
        # and an energy, or says a plan was replaced; it says nothing about the
        # actuator, and the assertions above already forbid every phrasing that
        # would. Before beta.41 this fixture planned nothing on these quarters, so
        # no lifecycle line was emitted and the blanket requirement held by
        # accident.
        if "plan id:" in message:
            continue
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
    # **7 since beta.39**, which adds one optional per-day dict: what the energy
    # the day opened with was worth on the value curve that existed then. It is the
    # one datum a forecast revaluation needs and the one datum nothing retained.
    # Additive like every bump before it -- a beta.38 document reads back with the
    # key absent, which is a defined state with its own published reason -- so the
    # major staying at 2 is still the load-bearing half.
    # 2.8 as of beta.42: the sealed per-day benefit and the lifetime cursor, so a
    # corrected battery capacity cannot rewrite a lifetime figure and an evicted
    # day still counts toward it. Additive, and the *major* staying at 2 is the
    # half that guarantees every earlier document is read rather than discarded.
    assert document["minor_version"] == STORAGE_MINOR_VERSION == 8
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
