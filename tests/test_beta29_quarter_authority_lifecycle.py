"""beta.29: the open quarter is the execution authority, for both intents.

The hardware timeline this file reproduces, from the 19:45-20:00 export quarter:

    19:30  the quarter is admitted, one refresh ahead
    19:45  the quarter opens; the fresh publication covers 20:00 onward and
           therefore cannot affirm the 19:45 run, so CarriedRun ends with
           stage_a_hold / no_affirming_net_export_publication
    19:45  the controller computes -0.16 kW desired grid, +0.778 kW required
           dispatch, +0.7 kW applied, and builds the full Mode 2 START sequence
    19:45  ...and the device stays inactive

Three defects, one root. ``authorize_export`` required provable ownership
unconditionally, and before the first write there is nothing to own -- so the export
path refused every START, forever. ``_stage_b_intent`` asked the quarter only for
``net_export``, so an open *charge* quarter whose run had ended produced no command at
all. And ``_refresh_outcome`` was recorded before authorization ran, so it reported a
run-level ``target_reached`` for a refresh that had actually been refused
``ownership_not_provable``.

The run ending at 19:45 is **expected**: Stage A's horizon head is
``elapsed_intervals + 1``, so a 19:45 refresh publishes from 20:00 and the 19:45 run
cannot affirm itself. That is the fact ``CarriedQuarter`` exists for, and it is why
none of the fixes below touch ``carry_forward`` or ``carry_quarter``.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    DISCHARGE_FAMILY,
    DISPATCH_ENABLE,
    DISPATCH_POWER,
    SENSOR_DISPATCH_START,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXPORT_REFUSE_DISPATCH_FOREIGN,
    EXPORT_REFUSE_INCOHERENT,
    EXPORT_REFUSE_NOT_OWNED,
    EXPORT_REFUSE_RECORD_MISMATCH,
    EXPORT_REFUSE_RESERVE_FLOOR,
    QUARTER_END_EXPIRED,
    TICK_STOPPED_TARGET_REACHED,
)
from custom_components.alpha_ems_manager.execution import (
    TARGET_TOLERANCE_KWH,
    quarter_intent_for,
)
from custom_components.alpha_ems_manager.safety import authorize_export

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge, step_once
from .test_beta27_live_export import authorised, startable
from .test_beta27_quarter_execution import install, quarter_at

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def withdraw_publications(coordinator, monkeypatch) -> None:
    """Make Stage A publish nothing further, as it did across the 19:45 boundary.

    The strongest form of the hardware condition: not merely "no affirming run for
    this quarter" but no publication at all. If the open quarter survives this, it
    survives every weaker version.
    """
    monkeypatch.setattr(
        type(coordinator), "_execution_targets", lambda self, **kwargs: ()
    )


def orphan_the_quarter(coordinator, quarter) -> None:
    """Install an open quarter whose parent run has ended, as at 19:45."""
    install(coordinator, quarter)
    coordinator._carried = None


async def at_rest(hass, coordinator) -> None:
    """Put the inverter genuinely at rest, as it was at 19:45.

    **Both halves of ``dispatch_active``**, which is ``bool(start) or
    bool(active_modes)`` -- the enable boolean alone leaves the device's own start
    instant still reporting a running dispatch, and the refresh then takes the
    *stop* path instead of the start path. A test that missed this would assert
    ``authorized`` against a reset and prove nothing about starting.

    The start sensor is set to ``"0"`` rather than ``"unknown"``: an unavailable
    entity is refused as ``missing_control_entity`` long before ownership is asked,
    which is a different refusal from the one under test.
    """
    hass.states.async_set(DISPATCH_ENABLE, "off")
    hass.states.async_set(SENSOR_DISPATCH_START, "0")
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    coordinator.store.execution_record = None
    await hass.async_block_till_done()


async def after_dark(hass) -> None:
    """Drop production to zero, as it was at 19:45.

    **Not cosmetic.** An export's required discharge is
    ``house - pv + export``, so with the fixture's daytime 3 kW of production
    against 2 kW of house load a small export target needs *no* battery at all --
    production is already sending more than the plan asked for, and commanding zero
    is the correct answer. The hardware quarter under investigation was at 19:45,
    after sunset, which is why it needed +0.778 kW.
    """
    from .conftest import PV_POWER, set_sensor

    set_sensor(hass, PV_POWER, 0, "W", "power")
    await hass.async_block_till_done()


# ===========================================================================
# 1 + 2. the exact hardware timeline, once per intent
# ===========================================================================


async def test_the_1945_export_timeline_still_starts(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The hardware defect, end to end.** Open export quarter, no parent run.

    Asserts the whole chain the hardware broke at: a command is built, it is
    *authorized*, and it is *sent* on the Dispatch surface with a positive power.
    beta.28 got as far as building it.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await after_dark(hass)
    await at_rest(hass, coordinator)
    orphan_the_quarter(
        coordinator,
        quarter_at(
            10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=0.25, export=0.04
        ),
    )
    withdraw_publications(coordinator, monkeypatch)
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    # The parent run really is gone, which is the condition under test.
    assert coordinator._carried is None
    # An intent was built from the quarter alone.
    intent = report.get("intent") or {}
    assert intent.get("action") == ACTION_DISCHARGE, intent
    # It was authorised -- the half beta.28 failed.
    authorization = report.get("authorization") or {}
    assert authorization.get("authorized") is True, authorization
    # **And it was actually sent, enable last.** Asserting authorisation alone would
    # pass on a *reset*, which is also authorised; the enable turning on is what
    # distinguishes a START.
    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written[0] == BOOLEAN_EXECUTION_OWNER, written
    assert written[-1] == DISPATCH_ENABLE, written
    assert hass.states.get(DISPATCH_ENABLE).state == "on"
    powers = [
        call.data["value"]
        for call in live_surface.calls
        if call.data["entity_id"] == DISPATCH_POWER
    ]
    # Positive Mode 2, which is what an export is on this surface.
    assert powers and powers[0] > 0.0, powers


async def test_the_same_timeline_for_a_grid_charge(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The fix is not export-specific.**

    An open charge quarter whose run has ended produced *no command at all* before
    beta.29 -- the beta.26 skipped-quarter fault, still live and masked only because
    charge runs usually span several quarters and get affirmed.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await at_rest(hass, coordinator)
    orphan_the_quarter(coordinator, quarter_at(10, 45, battery=1.0, authorised=1.0))
    withdraw_publications(coordinator, monkeypatch)
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    assert coordinator._carried is None
    intent = report.get("intent") or {}
    assert intent.get("action") == ACTION_CHARGE, intent
    assert (report.get("authorization") or {}).get("authorized") is True

    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written[0] == BOOLEAN_EXECUTION_OWNER, written
    assert written[-1] == DISPATCH_ENABLE, written
    assert hass.states.get(DISPATCH_ENABLE).state == "on"
    powers = [
        call.data["value"]
        for call in live_surface.calls
        if call.data["entity_id"] == DISPATCH_POWER
    ]
    # Negative Mode 2 for a charge, unchanged.
    assert powers and powers[0] < 0.0, powers


async def test_a_charge_setpoint_is_negative_and_an_export_positive(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The signed convention, unchanged, on the quarter-driven path."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    orphan_the_quarter(coordinator, quarter_at(10, 45, battery=1.0, authorised=1.0))
    charge = coordinator._dispatch_setpoint(local(NORMAL, 10, 46))
    assert charge is not None and charge.applied_kw < 0.0

    orphan_the_quarter(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.5),
    )
    export = coordinator._dispatch_setpoint(local(NORMAL, 10, 46))
    assert export is not None and export.applied_kw > 0.0


# ===========================================================================
# 3-5. the START gate: inactive-startable vs active-owned
# ===========================================================================


def test_a_start_with_nothing_running_is_authorised() -> None:
    """**The exact bug.** ``dispatch_active`` false, ``owned`` false ⇒ permitted.

    There is no dispatch to own and no causal record to match until the write lands.
    Requiring either made the export path unstartable on every refresh forever.
    """
    verdict = authorize_export(startable())

    assert verdict.safe, verdict.inhibit_reason


def test_the_relaxation_applies_only_when_nothing_is_running() -> None:
    """**The mandatory invariant.** Once active, ownership is a hard requirement.

    The one-line statement of the whole fix: ``owned=False`` is tolerated *only*
    while ``dispatch_active=False``.
    """
    assert authorize_export(startable()).safe

    active_unowned = authorize_export(
        authorised(dispatch_active=True, owned=False, foreign_dispatch=False)
    )
    assert not active_unowned.safe
    assert active_unowned.inhibit_reason == EXPORT_REFUSE_NOT_OWNED


def test_a_foreign_active_dispatch_is_still_refused() -> None:
    """Something running that we cannot prove is ours refuses before ownership.

    This is what protects the site in the start case: the relaxation is reachable
    only when nothing is active, so a foreign dispatch can never slip past it.
    """
    verdict = authorize_export(
        authorised(dispatch_active=True, owned=False, foreign_dispatch=True)
    )

    assert not verdict.safe
    assert verdict.inhibit_reason == EXPORT_REFUSE_DISPATCH_FOREIGN


def test_a_running_export_still_needs_causation_proven() -> None:
    """Owned but unproven still fails closed, so the relaxation is confined."""
    verdict = authorize_export(
        authorised(dispatch_active=True, owned=True, causation_proven=False)
    )

    assert not verdict.safe
    assert verdict.inhibit_reason == EXPORT_REFUSE_RECORD_MISMATCH


def test_the_checklist_is_still_complete_on_a_start() -> None:
    """Every clause is still reached and reported, not skipped on a start.

    A shorter checklist on a start than on a sustain would make the two
    incomparable in a download.
    """
    start = authorize_export(startable())
    sustain = authorize_export(authorised())

    assert [name for name, _ in start.checks] == [name for name, _ in sustain.checks]
    assert start.checks_evaluated == sustain.checks_evaluated


def test_the_reserve_floor_still_refuses_a_start() -> None:
    """The relaxation is about ownership and nothing else.

    Every physical bound still fails closed on a start exactly as on a sustain.
    """
    assert (
        authorize_export(startable(reserve_headroom_kwh=0.0)).inhibit_reason
        == EXPORT_REFUSE_RESERVE_FLOOR
    )
    assert (
        authorize_export(startable(soc_percent=10.0)).inhibit_reason
        == "configured_min_soc"
    )
    assert (
        authorize_export(startable(grid_export_remaining_kwh=0.0)).inhibit_reason
        == "no_meter_export_target"
    )


def test_serve_load_is_still_refused_on_a_start() -> None:
    """Widening the start case did not widen which intents may start."""
    verdict = authorize_export(startable(intent="serve_load"))

    assert not verdict.safe
    assert verdict.inhibit_reason == "not_an_export_intent"


def test_a_start_still_needs_an_open_quarter() -> None:
    """No quarter, no export -- unchanged, and checked before ownership."""
    assert not authorize_export(startable(quarter_admitted=False)).safe
    assert not authorize_export(startable(quarter_open=False)).safe


# ===========================================================================
# 5b. a START must not require a coherence verdict that only a tick can make
# ===========================================================================


async def test_an_export_starts_with_no_coherence_verdict_yet(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Found while implementing beta.29, and release-blocking on its own.**

    The control-grade coherence state is produced only by the sixty-second tick, and
    every stop sets it to ``None``. The tick cannot run before a START because it
    requires an active dispatch -- so requiring the verdict to *exist* made an
    export unstartable on the first refresh after any stop, the previous quarter's
    own expiry included. Refused ``sensor_incoherence``, next chance a quarter away.

    Absence of a verdict is not evidence of incoherence. With no verdict the
    question asked is the one ``control_coherence`` seeds itself from: are the
    sources readable at all.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await after_dark(hass)
    await at_rest(hass, coordinator)
    orphan_the_quarter(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.5),
    )
    withdraw_publications(coordinator, monkeypatch)
    # Exactly the post-stop state: no verdict yet.
    coordinator._coherence = None
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    assert coordinator._live_kw() is not None, "the readings are usable"
    assert (report.get("authorization") or {}).get("authorized") is True, report.get(
        "authorization"
    )
    assert hass.states.get(DISPATCH_ENABLE).state == "on"


def test_a_known_incoherent_verdict_still_refuses_a_start() -> None:
    """The fallback applies only to *absence*. A real verdict still governs.

    Asserted on the pure function rather than end to end, deliberately: a runtime
    test cannot hold a fabricated coherence state still, because settling the event
    loop lets a real sixty-second tick run and recompute it. Pinning the rule where
    the rule lives is both unambiguous and stable.
    """
    verdict = authorize_export(startable(coherent=False))

    assert not verdict.safe
    assert verdict.inhibit_reason == EXPORT_REFUSE_INCOHERENT


async def test_unreadable_sources_still_refuse_a_start_with_no_verdict(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The fallback is not a bypass: unreadable sources still fail closed."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await at_rest(hass, coordinator)
    orphan_the_quarter(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.5),
    )
    withdraw_publications(coordinator, monkeypatch)
    coordinator._coherence = None
    monkeypatch.setattr(type(coordinator), "_live_kw", lambda self: None)
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    assert (report.get("authorization") or {}).get("authorized") is not True
    assert live_surface.calls == []


def test_the_coherence_grace_bound_is_still_counted_in_ticks_alone() -> None:
    """**Why the fix is caller-side and not a second cadence feeding the machine.**

    ``control_coherence``'s grace is ``bad_ticks >= CONTROL_COHERENCE_GRACE_TICKS``
    -- a *count*, not a duration. Advancing it on the quarter-refresh cadence as
    well as the sixty-second tick would have shortened a documented 180-second
    safety bound to something nobody had chosen. So the machine is untouched and
    ``_update_coherence`` still has exactly one caller: the tick.
    """
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)

    assert source.count("self._update_coherence(now)") == 1
    assert "coherence = self._update_coherence(now)" in source


# ===========================================================================
# 6. UPDATE and STOP with CarriedRun null
# ===========================================================================


async def test_the_tick_updates_the_setpoint_with_no_carried_run(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """UPDATE, proven separately: the quarter alone drives the correction."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    orphan_the_quarter(coordinator, quarter_at(10, 45, battery=3.0, authorised=3.0))
    coordinator._applied_setpoint_kw = 0.0
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._carried is None
    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written == [DISPATCH_POWER], written


async def test_target_reached_stops_the_quarter_with_no_carried_run(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """STOP, proven separately -- and it must be *authorised*, not merely planned.

    A start gate and a stop gate that disagree about a direction is the worst of the
    two: it strands a running dispatch on the device dead-man.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    orphan_the_quarter(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.5),
    )
    coordinator._quarter_grid_export_kwh = 0.5
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._last_tick_reason == TICK_STOPPED_TARGET_REACHED
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"


async def test_the_quarter_still_ends_at_its_own_end_with_no_run(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The exposure bound.** An orphaned quarter is not an open-ended licence.

    At most one quarter and at most fifteen minutes, whatever Stage A has stopped
    publishing -- which is what makes "a parent run ending does not stop it" safe.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    orphan_the_quarter(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))

    await coordinator._async_physical_tick(local(NORMAL, 11, 0))

    assert coordinator._quarter is None
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    row = list(coordinator._completed_quarters)[-1]
    assert row["completion_reason"] == QUARTER_END_EXPIRED


async def test_the_execution_identity_survives_the_run_ending(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The causal record still matches, because the quarter names the same run."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    orphan_the_quarter(coordinator, quarter_at(10, 45, run_id="run-1"))

    assert coordinator._execution_identity() == "run-1"
    assert coordinator._executing_intent() == EXECUTION_INTENT_GRID_CHARGE


# ===========================================================================
# 7. the run-level tolerance is not a blocker under quarter authority
# ===========================================================================


@pytest.mark.parametrize(
    ("intent", "expected_action"),
    [
        (EXECUTION_INTENT_GRID_CHARGE, ACTION_CHARGE),
        (EXECUTION_INTENT_NET_EXPORT, ACTION_DISCHARGE),
    ],
)
async def test_a_quarter_at_the_run_level_tolerance_still_executes(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
    expected_action: str,
) -> None:
    """**The test that decides whether ``TARGET_TOLERANCE_KWH`` must change.**

    The 19:45 run's battery target was 0.25 kWh and ``TARGET_TOLERANCE_KWH`` is
    ``0.25``, so ``demand_for`` declared ``TARGET_MET`` on the very first refresh --
    with zero progress. That gated the *run-level* command through
    ``Decision.wants_command``, and it is where the phantom ``target_reached`` came
    from.

    Under quarter authority the command no longer comes from that path, so the
    tolerance cannot block execution. This asserts exactly that, for both intents,
    at precisely the boundary value -- and it is the evidence for leaving
    ``demand_for`` untouched rather than an assumption that it is safe to.
    """
    assert TARGET_TOLERANCE_KWH == 0.25

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await after_dark(hass)
    orphan_the_quarter(
        coordinator,
        quarter_at(
            10,
            45,
            intent=intent,
            battery=TARGET_TOLERANCE_KWH,
            authorised=TARGET_TOLERANCE_KWH,
            export=TARGET_TOLERANCE_KWH
            if intent == EXECUTION_INTENT_NET_EXPORT
            else 0.0,
        ),
    )
    withdraw_publications(coordinator, monkeypatch)

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    intent_block = report.get("intent") or {}
    assert intent_block.get("action") == expected_action, intent_block
    assert intent_block.get("average_power_kw", 0.0) > 0.0
    assert (report.get("authorization") or {}).get("authorized") is True


def test_the_run_level_tolerance_was_not_changed() -> None:
    """Pinned, because the approved scope forbids changing it without proof.

    The test above is that proof: execution no longer depends on the run-level
    target-met verdict, so the tolerance stays exactly where beta.24 put it.
    """
    assert TARGET_TOLERANCE_KWH == 0.25


# ===========================================================================
# 8. the quarter's own ceiling binds once the run is gone
# ===========================================================================


async def test_the_quarters_grid_ceiling_binds_with_no_run(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The obvious worry, answered.** No run means no run-level grid cap.

    ``_charge_limits`` reads its grid cap from the last Stage-B demand, which is
    absent once the run has gone -- so the only thing bounding the purchase is the
    **quarter's own** ``grid_authorised_kwh``, through ``progress.grid_rate_kw``.
    A charge is not unbounded as a result, and this pins the figure.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    # A large battery objective against a small grid authorisation, with no
    # production: the ceiling is the only thing that can hold it back.
    orphan_the_quarter(coordinator, quarter_at(10, 45, battery=9.0, authorised=0.25))

    progress = coordinator._quarter_progress(local(NORMAL, 10, 46))
    assert progress is not None
    decision = coordinator._dispatch_setpoint(local(NORMAL, 10, 46))
    assert decision is not None

    # Bounded by the quarter's own remaining authorisation plus any production
    # surplus -- never by the 9.0 kWh battery objective alone.
    live = coordinator._live_kw()
    assert live is not None
    surplus = max(0.0, live[1] - live[0])
    assert abs(decision.applied_kw) <= progress.grid_rate_kw + surplus + 1e-6


async def test_an_orphaned_quarter_never_writes_a_helper_family(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The actuator surface is unchanged by any of this."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    orphan_the_quarter(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.5),
    )
    withdraw_publications(coordinator, monkeypatch)
    live_surface.calls.clear()

    await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    written = {call.data["entity_id"] for call in live_surface.calls}
    assert not written & set(DISCHARGE_FAMILY.entities), written


# ===========================================================================
# 9 + 10. the refresh outcome names what actually decided the refresh
# ===========================================================================


async def test_the_refresh_outcome_reports_the_authorisation_refusal(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The diagnostic defect that cost this investigation its time.**

    A refused START must name the condition that refused it, not a run-level stop
    reason describing a different question.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    orphan_the_quarter(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.5),
    )
    withdraw_publications(coordinator, monkeypatch)
    # Spoil one export condition, so authorization refuses a well-formed START.
    monkeypatch.setattr(
        type(coordinator),
        "_coherence",
        property(lambda self: None),
        raising=False,
    )

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    outcome = coordinator._refresh_outcome
    assert outcome is not None
    authorization = report.get("authorization") or {}
    if not authorization.get("authorized"):
        assert outcome.reason != "target_reached", outcome.as_dict()
        assert outcome.wrote is False


async def test_a_planned_but_refused_command_is_not_recorded_as_written(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """``wrote`` means permitted, not merely planned."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    orphan_the_quarter(coordinator, quarter_at(10, 45, battery=1.0, authorised=1.0))

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    outcome = coordinator._refresh_outcome
    authorization = report.get("authorization") or {}
    assert outcome is not None
    if not authorization.get("authorized"):
        assert outcome.wrote is False


def test_the_refresh_outcome_is_recorded_after_authorisation() -> None:
    """**Structural, because the ordering *was* the defect.**

    ``_refresh_outcome`` used to be assigned at the write boundary, before
    ``authorize_start`` ran -- so it could not see the decision even in principle,
    and fell back to a stop reason. Asserted on source order so a later refactor
    cannot quietly move it back in front.
    """
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)

    assign = source.index("self._refresh_outcome = TickOutcome(")
    authorise = source.index("decision = authorize_start(")

    assert authorise < assign, "the outcome must be recorded after authorization"
    # And exactly once, so a second assignment cannot reintroduce the early one.
    assert source.count("self._refresh_outcome = TickOutcome(") == 1


def test_the_outcome_reason_precedence_is_the_approved_one() -> None:
    """Write-boundary refusal, then authorization, then stop only while stopping."""
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)
    block = source[source.index("outcome_reason = refusal") :][:900]

    assert "not decision.authorized" in block
    assert "decision.unsafe_reason or decision.refusal" in block
    # The stop reason is admissible only while actually stopping.
    assert "if outcome_reason is None and (resetting or releasing):" in block


# ===========================================================================
# the builder: both intents, and nothing else
# ===========================================================================


def test_the_quarter_builder_maps_each_intent_to_one_direction() -> None:
    """An intent carries the surface; the action carries only the direction."""
    base = {
        "battery_power_kw": 2.0,
        "floor_soc_percent": 20.0,
        "ceiling_soc_percent": 90.0,
        "horizon_minutes": 20,
        "target_day": local(NORMAL, 10, 45).date(),
        "start_index": 43,
        "built_at": local(NORMAL, 10, 45),
    }

    charge = quarter_intent_for(quarter_at(10, 45), **base)
    assert charge is not None
    assert charge.action == ACTION_CHARGE
    # A charge cutoff is an *upper* state of charge, so the ceiling is carried.
    assert charge.ceiling_soc_percent == pytest.approx(90.0)

    export = quarter_intent_for(
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, export=0.5), **base
    )
    assert export is not None
    assert export.action == ACTION_DISCHARGE
    # A discharge's backstop is the floor; a ceiling would be the wrong bound.
    assert export.ceiling_soc_percent is None
    assert export.floor_soc_percent == pytest.approx(20.0)


def test_the_quarter_builder_refuses_every_other_intent() -> None:
    """``None`` rather than a guess, for anything not validated for Live."""
    base = {
        "battery_power_kw": 2.0,
        "floor_soc_percent": 20.0,
        "ceiling_soc_percent": 90.0,
        "horizon_minutes": 20,
        "target_day": local(NORMAL, 10, 45).date(),
        "start_index": 43,
        "built_at": local(NORMAL, 10, 45),
    }

    for intent in ("serve_load", "curtail", "anything_at_all"):
        assert quarter_intent_for(quarter_at(10, 45, intent=intent), **base) is None


def test_a_zero_power_quarter_builds_no_command() -> None:
    """Nothing to move is not an arm, for either direction."""
    base = {
        "battery_power_kw": 0.0,
        "floor_soc_percent": 20.0,
        "ceiling_soc_percent": 90.0,
        "horizon_minutes": 20,
        "target_day": local(NORMAL, 10, 45).date(),
        "start_index": 43,
        "built_at": local(NORMAL, 10, 45),
    }

    assert quarter_intent_for(quarter_at(10, 45), **base) is None
    assert (
        quarter_intent_for(
            quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, export=0.5), **base
        )
        is None
    )


def test_control_intent_for_is_still_charge_only() -> None:
    """The guarantee that makes the direction interlock structural, intact.

    beta.29 generalised a *different* function rather than widening this one.
    """
    import inspect

    from custom_components.alpha_ems_manager import execution

    source = inspect.getsource(execution.control_intent_for)

    assert "ACTION_CHARGE" in source
    assert "ACTION_DISCHARGE" not in source


def test_carry_forward_and_carry_quarter_were_not_changed_for_this() -> None:
    """The 19:45 run ending is expected, so neither carrier needed touching.

    Pinned because the tempting fix was to stop the run ending -- which would have
    fought Stage A's horizon head instead of letting the quarter do its job.
    """
    import inspect

    from custom_components.alpha_ems_manager import execution

    carry_quarter = inspect.getsource(execution.carry_quarter)

    # The open-quarter branch is still unconditional on the horizon.
    assert "if current is not None and current.open_at(now):" in carry_quarter
    assert "return current" in carry_quarter
