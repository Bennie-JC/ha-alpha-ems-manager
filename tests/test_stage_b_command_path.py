"""The Stage-B command path, end to end, with the integration actually running.

**A12 and the runtime half of A13.** The distinction this file exists to draw is
the one beta.19 could not: between *no command was sent* and *the right command was
built and then not sent*. Those look identical from outside and they are the whole
difference between a release that is safe because it is inert and one that is safe
because a barrier holds.

So every assertion here is positive first and negative second. The command must
exist, be a charge, name only charge entities, carry an unsigned magnitude the
register can hold and a cutoff that is an upper bound -- and *then* nothing may be
written, no marker acquired and no ownership claimed.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.alphaess_adapter import (
    ControlActionNotPermitted,
    async_execute,
)
from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    DISPATCH_CUTOFF_SOC,
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_MODE_LABELS,
    DISPATCH_MODE_SELECT,
    DISPATCH_POWER,
    DISPATCH_PV_SWITCH,
    PERMITTED_SERVICES,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    CONTROL_CUTOFF_MAX_PERCENT,
    CONTROL_MAX_POWER_KW,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    OWNERSHIP_OWNED,
)

from .forecast_helpers import NORMAL, local, refresh_at
from .live_capability import assert_charge_only_capability
from .test_stage_b_runtime import prepared

pytestmark = pytest.mark.usefixtures("control_surface")

#: The quarter-hours of the measured campaign: admitted at 00:00 with a window
#: opening at 00:15, then affirmed and actionable for five consecutive refreshes.
CAMPAIGN = [(0, 0), (0, 15), (0, 30), (0, 45), (1, 0), (1, 15)]


async def sweep(coordinator, moments) -> list[dict]:
    """Return the execution block for each of ``moments``, in order."""
    seen = []
    for hour, minute in moments:
        await refresh_at(coordinator, local(NORMAL, hour, minute))
        report = coordinator.control_report or {}
        seen.append(dict(report.get("execution") or {}))
    return seen


def boundary(block: dict) -> dict:
    """Return the write-boundary record for one refresh."""
    return block.get("write_boundary") or {}


# ===========================================================================
# A12. a fully-formed charge command, refused as one unit
# ===========================================================================


async def test_the_stage_b_charge_command_forms_completely_and_is_refused(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """**A12.** The whole chain, asserted link by link, then the refusal.

    An empty command list would be a failure here, not a success: it is exactly
    what beta.19 produced, and it is why F1 survived a full test suite.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    blocks = await sweep(coordinator, CAMPAIGN)

    charging = [b for b in blocks if boundary(b).get("source") == "stage_b"]
    assert charging, "Stage B never became the command source"

    for block in charging:
        record = boundary(block)
        # 1. the intent is a charge, and Stage B is where it came from.
        assert record["action"] == ACTION_CHARGE
        # 2. it resolved to the charge family, which is still how the *action*
        #    is named -- the Dispatch surface is how it is executed.
        assert record["family"] == CHARGE_FAMILY.activate
        steps = record["steps"]
        # 3. a complete ordered command: marker first, enable last. Seven steps
        #    since beta.25, because the Dispatch arm also selects a mode and
        #    asserts the photovoltaic switch.
        assert len(steps) == 7, steps
        assert steps[0]["entity_id"] == BOOLEAN_EXECUTION_OWNER
        assert steps[-1]["entity_id"] == DISPATCH_ENABLE
        entities = {step["entity_id"] for step in steps}
        # 4. no entity of the opposite family anywhere in it -- and no helper
        #    family at all, since Dispatch is the one Live actuator.
        assert not entities & set(DISCHARGE_FAMILY.entities)
        assert not entities & set(CHARGE_FAMILY.entities)
        # 5. **the mode is selected by its exact package label**, because the
        #    package parses the number out of the string.
        modes = [
            step["option"]
            for step in steps
            if step["entity_id"] == DISPATCH_MODE_SELECT
        ]
        assert modes == [DISPATCH_MODE_LABELS[2]], modes
        # 6. **a signed magnitude, and it is negative.** The opposite convention
        #    from the helper families, which take an unsigned magnitude and carry
        #    direction in which family was written. A charge on this surface is a
        #    negative number, and a positive one is refused at the send site.
        powers = [
            step["value"] for step in steps if step["entity_id"] == DISPATCH_POWER
        ]
        assert len(powers) == 1
        assert powers[0] < 0.0, powers
        assert CONTROL_MIN_POWER_KW <= -powers[0] <= CONTROL_MAX_POWER_KW
        # 6. a cutoff that is an *upper* bound, not the discharge floor.
        cutoffs = [
            step["value"] for step in steps if step["entity_id"] == DISPATCH_CUTOFF_SOC
        ]
        assert len(cutoffs) == 1
        assert cutoffs[0] == CONTROL_CUTOFF_MAX_PERCENT
        # 7. and the interlock passed it, because a correct charge is not a fault.
        assert record["refusal"] is None

    # 8. Nothing was written. Not the parameters, and not the marker -- which is
    #    step 1 of the very list above, so "would acquire" is legible and "did not
    #    acquire" is guaranteed by the same refusal that blocks everything else.
    assert writes == []
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"
    assert coordinator._last_control_write is None
    assert coordinator.store.execution_record is None
    for block in blocks:
        power = block.get("power") or {}
        assert power.get("applied_kw") in (None, 0.0)
        assert power.get("executed") in (None, False)
        assert (block.get("ownership") or {}).get("state") != OWNERSHIP_OWNED


async def test_the_completed_charge_command_reaches_the_wire_in_order(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """**The Live positive path, and the first test in this project to expect writes.**

    Through beta.23 this same command was handed to the last barrier and refused;
    the test asserted six steps in and zero service calls out. beta.24 executes a
    charge, so the honest assertion is the opposite one -- and it has to be, because
    a release that claims to charge and cannot demonstrate a service call is not
    demonstrating anything.

    The step list is the real one a Live refresh built, not a constructed fixture.
    What is asserted is the *ordering* the safety argument rests on: the marker
    first so an interruption leaves a clearable stale marker rather than an
    unattributable dispatch, the parameters next, and **activation last** so the
    numbers are settled before anything moves.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    await sweep(coordinator, CAMPAIGN[:2])

    commands = coordinator._pending_commands
    assert len(commands) == 7
    assert commands[-1].entity_id == DISPATCH_ENABLE

    sent = await async_execute(hass, commands)

    assert sent == 7
    assert [call.data["entity_id"] for call in writes] == [
        # The claim first, so an interruption leaves a clearable stale marker
        # rather than an unattributable dispatch.
        BOOLEAN_EXECUTION_OWNER,
        # The mode before the power, because the power register is only honoured
        # in some modes -- writing a rate into an undecided mode commands nothing.
        DISPATCH_MODE_SELECT,
        DISPATCH_POWER,
        DISPATCH_CUTOFF_SOC,
        DISPATCH_DURATION,
        # The photovoltaic switch asserted to its fail-safe on, in case a previous
        # run of ours left it off.
        DISPATCH_PV_SWITCH,
        # **The enable last**, because it is edge-triggered: it is what makes the
        # settled values take effect.
        DISPATCH_ENABLE,
    ]
    # Nothing from the other direction, and nothing from the raw surface.
    assert not [
        call
        for call in writes
        if call.data["entity_id"] in set(DISCHARGE_FAMILY.entities)
    ]
    assert_charge_only_capability()


async def test_the_same_step_list_for_a_discharge_is_refused_as_one_unit(
    hass: HomeAssistant,
) -> None:
    """The negative half, and it must be asserted on the *same* boundary.

    Whole-command refusal is the property: the discharge equivalent of the list
    above goes in and **zero** service calls come out. There are no partial writes,
    which is what makes the activation-last ordering worth having.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        build_command,
        plan_commands,
    )

    from .test_control_pipeline import make_intent

    steps = plan_commands(build_command(make_intent(energy_ac_kwh=0.5)))
    assert len(steps) == 6

    with pytest.raises(ControlActionNotPermitted) as raised:
        await async_execute(hass, steps)

    assert raised.value.reason == "live_charge_only"
    assert set(raised.value.entity_ids) == set(DISCHARGE_FAMILY.entities) - {
        DISCHARGE_FAMILY.timer
    }


async def test_only_the_permitted_services_can_appear_in_a_command(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """The permitted set is closed, and the charge path did not widen it.

    Hardware Gate A established that deactivating the dispatch stops the action and
    clears the vendor timer, so no timer service is needed. This is what keeps that
    finding from quietly eroding.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    blocks = await sweep(coordinator, CAMPAIGN)

    assert len(set(PERMITTED_SERVICES)) == 4
    for block in blocks:
        for step in boundary(block).get("steps") or []:
            domain, service = step["service"].split(".")
            assert (domain, service) in PERMITTED_SERVICES
            assert "timer" not in step["entity_id"]
            # **The read-only dispatch sensors are never a write target**, and
            # that is unchanged: ``sensor.alphaess_dispatch_*`` is the device's own
            # readback. The *writable* Dispatch helpers are a different surface.
            assert "sensor." not in step["entity_id"]
            assert "dispatch_active_power" not in step["entity_id"]
            # **The sign rule is per surface, not global.** Helper families take an
            # unsigned magnitude; the Dispatch power is signed and a charge is
            # negative. Asserting one rule across both is what would let a charge
            # be commanded as a discharge.
            value = step.get("value")
            if value is None:
                continue
            if step["entity_id"] == DISPATCH_POWER:
                assert value <= 0.0, step
            else:
                assert value >= 0.0, step


# ===========================================================================
# A13, at runtime. The measured failure, in the running integration.
# ===========================================================================


async def test_the_carried_run_survives_the_publications_that_replace_it(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """**A13.** One run identity across a campaign whose publication churns.

    This is the sequence that produced nothing at all in beta.19 and in the first
    implementation pass: ten refreshes, ``prepared`` every time, because the target
    being evaluated always opened fifteen minutes later than the refresh that
    published it.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    blocks = await sweep(coordinator, CAMPAIGN)

    carried = [block.get("carried") or {} for block in blocks]
    runs = [(c.get("run") or {}).get("run_id") for c in carried]
    plans = [(c.get("publication") or {}).get("plan_id") for c in carried]

    # One run, and the publication identity churning underneath it.
    live = [r for r in runs if r]
    assert len(set(live)) == 1, runs
    assert len({p for p in plans if p}) == len(CAMPAIGN), plans

    # The first refresh prepares and sends nothing; the next opens the accepted
    # window and Stage B takes the command source.
    assert carried[0]["window_open"] is False
    assert boundary(blocks[0])["source"] == "reserve_guard"
    assert carried[1]["window_open"] is True
    assert carried[1]["affirmed_by_this_publication"] is True
    assert boundary(blocks[1])["source"] == "stage_b"

    # The accepted window is never moved by a later publication.
    starts = {(c.get("run") or {}).get("window_start") for c in carried if c.get("run")}
    assert len(starts) == 1, starts

    # A revision means Stage A moved an energy figure, not that the horizon
    # advanced. beta.19 pinned this at 1 forever; comparing the window end made it
    # a refresh counter instead.
    revisions = {(c.get("run") or {}).get("revision") for c in carried if c.get("run")}
    assert revisions == {1}, revisions

    # And no reserve-guard discharge won the boundary once the charge was
    # actionable, which is the way F1's effect survived the first pass.
    for block, entry in zip(blocks[1:], carried[1:], strict=False):
        if entry.get("window_open"):
            assert boundary(block)["action"] == ACTION_CHARGE

    assert writes == []


async def test_grid_attribution_and_progress_survive_the_quarter_boundaries(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """Both are keyed on the run, so neither resets when the horizon rolls.

    Keyed on the publication they would have reset every fifteen minutes -- the F6
    sawtooth returning by a different route, which is why the re-keying is a
    numbered step with a gate of its own rather than a remark.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    blocks = await sweep(coordinator, CAMPAIGN)

    inside = [
        block
        for block, entry in zip(
            blocks, [b.get("carried") or {} for b in blocks], strict=False
        )
        if (entry.get("run") or {}).get("run_id")
    ]
    assert len(inside) >= 5

    grid = [
        (block.get("power") or {}).get("grid_charged_kwh_estimate") for block in inside
    ]
    measured = [g for g in grid if g is not None]
    # Present, not merely absent-and-therefore-sorted: an all-null list would make
    # the monotonicity assertion below vacuous.
    assert measured, grid
    assert measured == sorted(measured), measured

    # The cap and what is left of it are published beside the integral, and a
    # null cap means unconstrained rather than zero.
    for block in inside:
        power = block.get("power") or {}
        cap = power.get("grid_cap_kwh")
        if cap is not None:
            assert power["grid_remaining_kwh"] >= 0.0
            assert power["grid_remaining_kwh"] <= cap

    # And what the request becomes on the wire, which nothing published before.
    for block in inside:
        power = block.get("power") or {}
        assert power.get("quantised_physical_power_kw") is not None
        assert power["quantised_physical_power_kw"] <= CONTROL_MAX_POWER_KW
        readback = block.get("device_readback") or {}
        assert "dispatch_active" in readback

    delivered = [
        (block.get("progress") or {}).get("battery_realized_kwh") for block in inside
    ]
    seen = [d for d in delivered if d is not None]
    assert seen == sorted(seen), seen

    assert writes == []


# ===========================================================================
# A15. a restart discards the run and keeps the claim
# ===========================================================================


async def test_a_restart_discards_the_carried_run_and_keeps_the_ownership_record(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """**A15.** The two are different questions and are answered differently.

    A carried run is a prediction admitted before the restart, and Stage A
    republishes within one refresh -- so resuming one would buy at most a single
    interval and inherit a whole class of stale-resume risk. The ownership record
    is about not abandoning a live dispatch, so it persists.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    await sweep(coordinator, CAMPAIGN[:2])
    assert coordinator._carried is not None

    # What a restart would restore: the run is not in it.
    payload = coordinator.store.to_dict()
    text = repr(payload)
    assert coordinator._carried.run_id not in text
    assert "carried" not in payload.get("execution", {})

    # And the record, when one exists, is.
    coordinator.store.execution_record = {"run_id": "abc123", "dispatch_start": None}
    restored = coordinator.store.to_dict()
    assert restored["execution"]["record"]["run_id"] == "abc123"

    assert writes == []


# ===========================================================================
# The send site: a failed write costs the write, never the refresh
# ===========================================================================


async def test_a_failed_write_keeps_the_ownership_evidence_and_the_refresh(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
    monkeypatch,
) -> None:
    """**The retry path, and it is the whole reason this is guarded.**

    The send site sits outside the safe report wrapper -- it has to, being the one
    place that awaits the adapter -- so an unavailable helper would otherwise take
    down the entire coordinator update. That is worse than losing one command,
    because the refresh loop is what would retry.

    ``plan_reset`` releases the marker as its *last* step, so a stop interrupted
    partway leaves the marker on with the record intact. That reads as ``owned``
    next refresh and the stop is re-attempted. Clearing the record here would drop
    to ``unproven``, and an unproven dispatch is never touched again -- which is
    the latch-on fault F16 named.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    await sweep(coordinator, CAMPAIGN[:2])

    # A claim standing from an earlier arm, and a stop that fails mid-sequence.
    coordinator.store.execution_record = {"run_id": "r1", "dispatch_start": None}
    coordinator._pending_is_reset = True

    async def explode(*args, **kwargs):
        raise RuntimeError("input_number.set_value is unavailable")

    monkeypatch.setattr(
        "custom_components.alpha_ems_manager.coordinator.async_execute", explode
    )
    report = {
        "authorization": {"authorized": True},
        "commands_planned": 6,
        "execution": {"power": {}, "result": {}},
    }

    # It must not raise: the refresh has to survive to be able to retry.
    await coordinator._async_dispatch(report, dt_util.now())

    assert report["execution"]["result"]["execution_error"] == "write_failed"
    # And the evidence a retry needs is still there.
    assert coordinator.store.execution_record == {
        "run_id": "r1",
        "dispatch_start": None,
    }
    assert coordinator._last_control_write is None


async def test_the_barrier_refusal_withdraws_a_claim_nothing_acted_on(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank,
    writes: list,
) -> None:
    """The other half: a refusal must not leave a record behind.

    The barrier refuses before the first service call, so no dispatch started and
    the claim is withdrawn. Left in place it would read as ownership of whatever
    dispatch appeared next -- including one somebody armed by hand.
    """
    coordinator = await prepared(hass, setup_integration, frank, CONTROL_MODE_ACTIVE)
    await sweep(coordinator, CAMPAIGN)

    assert coordinator.store.execution_record is None
    assert writes == []
