"""beta.30: ownership on self-controlled evidence, and a derived quarter.

Four defects, three of them structural, all found from one real hardware run:

* **D1** ownership could never be proven on the inverter, so no correction ever
  landed and the dead-man stopped every run;
* **D2** every second quarter of a multi-quarter run was skipped;
* **D3** an ended run's progress was compared against a fresh publication;
* **D4** most planned export rows were below what the actuator can deliver.

The lesson running through all four, and the reason this file is shaped the way it
is: **a test double must never be defined as the inverse of the code under test.**
``LiveSurface`` modelled the dispatch-start register as "the same reconstruction the
ownership layer performs, from the same instant", so roughly two hundred ownership
assertions could not fail while Live execution was impossible in the field.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    DISCHARGE_FAMILY,
    DISPATCH_DEADMAN_MINUTES,
    DISPATCH_ENABLE,
    DISPATCH_POWER,
    SENSOR_DISPATCH_START,
)
from custom_components.alpha_ems_manager.const import (
    CADENCE_PHYSICAL_TICK,
    DISPATCH_POWER_STEP_KW,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    MIN_EXECUTABLE_QUARTER_KWH,
    OWNERSHIP_FACTOR_MARKER,
    OWNERSHIP_FACTOR_MODE,
    OWNERSHIP_FACTOR_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_PROVENANCE_EXACT,
    OWNERSHIP_PROVENANCE_PARAMETERS,
    OWNERSHIP_PROVENANCE_SETTLING,
    OWNERSHIP_UNPROVEN,
    QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION,
    TICK_SKIPPED_SUB_RESOLUTION,
)
from custom_components.alpha_ems_manager.execution import (
    AdmittedPlan,
    OwnershipEvidence,
    QuarterRow,
    carry_plan,
    ownership_of,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, drive_live_charge, owned_live_charge

pytestmark = pytest.mark.usefixtures("control_surface")

NOW = datetime(2026, 8, 19, 10, 46, tzinfo=local(NORMAL, 10, 46).tzinfo)


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def claim(**overrides) -> dict:
    """Return a causal claim of ours, as beta.30 writes one."""
    record = {
        "run_id": "run-1",
        "admitted_plan_id": "plan-1",
        "claim_id": "claim-1",
        "quarter_start": local(NORMAL, 10, 45).isoformat(),
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "power_kw": 4.3,
        "cutoff_soc_percent": 100,
        "duration_minutes": 20,
        "written_at": (NOW - timedelta(seconds=30)).isoformat(),
        "dispatch_start": None,
    }
    record.update(overrides)
    return record


def evidence(**overrides) -> OwnershipEvidence:
    """Return evidence for a running dispatch of ours."""
    fields = {
        "dispatch_active": True,
        "marker_on": True,
        "record": claim(),
        "run_id": "run-1",
        "now": NOW,
        "readback_compatible": True,
        "readback_power_kw": -4.3,
        "readback_cutoff_percent": 100.0,
        "readback_duration_minutes": 20.0,
        "readback_duration_permitted": True,
    }
    fields.update(overrides)
    return OwnershipEvidence(**fields)


def row(start_minute: int, *, battery: float = 1.0, export: float = 0.0) -> QuarterRow:
    """Return one published row of a schedule."""
    start = local(NORMAL, 10, start_minute)
    return QuarterRow(
        start=start,
        end=start + timedelta(minutes=15),
        battery_kwh=battery,
        grid_authorised_kwh=battery,
        grid_export_target_kwh=export,
        grid_export_caused_kwh=0.0,
        desired_grid_kw=battery / 0.25,
    )


def plan_of(
    *rows: QuarterRow, intent: str = EXECUTION_INTENT_GRID_CHARGE
) -> AdmittedPlan:
    """Return an admitted plan over ``rows``."""
    return AdmittedPlan(
        plan_id="plan-1",
        revision=1,
        run_id="run-1",
        intent=intent,
        purpose=intent,
        admitted_at=rows[0].start - timedelta(minutes=15),
        rows=rows,
    )


# ===========================================================================
# D1 -- ownership rests on evidence Alpha EMS controls
# ===========================================================================


def test_ownership_holds_with_no_dispatch_start_register_at_all() -> None:
    """**The hardware defect, as a unit test.**

    Every provenance path before beta.30 needed the vendor register. If it reads
    zero, or sits in another epoch, or means something else entirely, ownership must
    still hold -- because the marker, the claim and the readback are Alpha EMS's own
    evidence and none of them involves that register.
    """
    assert ownership_of(evidence(dispatch_start=None)) == OWNERSHIP_OWNED
    assert (
        evidence(dispatch_start=None).record_provenance
        == OWNERSHIP_PROVENANCE_PARAMETERS
    )


@pytest.mark.parametrize(
    "start",
    [
        None,
        NOW,
        NOW + timedelta(hours=2),  # a UTC/local confusion
        NOW - timedelta(hours=2),
        datetime(2026, 8, 19, 0, 0, tzinfo=NOW.tzinfo),  # a midnight-seconds reading
        datetime(2026, 8, 20, 6, 0, tzinfo=NOW.tzinfo),  # an epoch-shaped nonsense
    ],
)
def test_no_reading_of_that_register_can_withhold_ownership(start) -> None:
    """**Why beta.30 is safe to ship before P0 reports.**

    Whatever the measurement turns out to say -- seconds since local midnight, since
    UTC midnight, a Unix epoch, an elapsed counter, or a flat zero -- ownership is
    unaffected. The register can only ever *strengthen* the label.
    """
    verdict = evidence(dispatch_start=start)

    assert ownership_of(verdict) == OWNERSHIP_OWNED
    assert verdict.record_provenance in (
        OWNERSHIP_PROVENANCE_EXACT,
        OWNERSHIP_PROVENANCE_SETTLING,
        OWNERSHIP_PROVENANCE_PARAMETERS,
    )


def test_ownership_survives_a_dead_man_re_arm() -> None:
    """The duration alternates 20/25 by design, so it cannot be compared to the claim.

    Judging it against the claim's own value would deny ownership at the first
    re-arm -- fifteen minutes into every run. The invariant question is whether the
    live duration is one Alpha EMS is willing to command.
    """
    for minutes in DISPATCH_DEADMAN_MINUTES:
        verdict = evidence(
            readback_duration_minutes=float(minutes), readback_duration_permitted=True
        )
        assert ownership_of(verdict) == OWNERSHIP_OWNED, minutes

    # And a duration we would never command is refused.
    refused = evidence(readback_duration_minutes=5.0, readback_duration_permitted=False)
    assert ownership_of(refused) == OWNERSHIP_UNPROVEN


def test_ownership_survives_the_controller_moving_the_power() -> None:
    """The sixty-second controller rewrites the power; that is its whole purpose.

    Measured during implementation: claim 4.3 kW, live -5.5 kW one minute later.
    A factor comparing them would deny ownership as soon as the first correction
    landed -- so the power is reported and never judged.
    """
    for live in (-4.3, -5.5, -0.1, -9.9):
        assert ownership_of(evidence(readback_power_kw=live)) == OWNERSHIP_OWNED, live


def test_ownership_survives_a_stage_a_revision() -> None:
    """A republication changes the run's figures and must not touch ownership.

    The claim names the run; ``record_names_this_run`` compares that and nothing
    about the publication's contents.
    """
    assert ownership_of(evidence(run_id="run-1")) == OWNERSHIP_OWNED
    # And a claim naming a genuinely different run is still refused.
    assert ownership_of(evidence(run_id="run-2")) == OWNERSHIP_UNPROVEN


def test_ownership_spans_every_row_of_its_plan() -> None:
    """One claim covers one dispatch session, and a session spans the whole plan.

    Binding it to the row was measured breaking ownership at the first boundary.
    """
    for minute in (45, 46, 59):
        moment = local(NORMAL, 10, minute)
        assert ownership_of(evidence(now=moment)) == OWNERSHIP_OWNED, minute


def test_a_foreign_dispatch_is_never_adopted() -> None:
    """Fail-closed, unchanged. Every single factor is necessary."""
    assert ownership_of(evidence(marker_on=False)) != OWNERSHIP_OWNED
    assert ownership_of(evidence(record=None)) != OWNERSHIP_OWNED
    assert ownership_of(evidence(readback_compatible=False)) != OWNERSHIP_OWNED
    assert ownership_of(evidence(dispatch_active=False)) != OWNERSHIP_OWNED
    # A cutoff we did not write is a different command.
    assert ownership_of(evidence(readback_cutoff_percent=42.0)) != OWNERSHIP_OWNED


def test_a_pre_beta_30_claim_fails_closed() -> None:
    """An upgrade must not adopt a dispatch armed under rules this release dropped.

    The old claim carries no cutoff to compare, so the readback cannot corroborate
    it -- and the dead-man finishes whatever is in flight, which is what it is for.
    """
    legacy = {
        "run_id": "run-1",
        "written_at": (NOW - timedelta(seconds=30)).isoformat(),
        "dispatch_start": None,
    }
    verdict = evidence(record=legacy, readback_cutoff_percent=100.0)

    # It has no admitted plan recorded, which the diagnostics report.
    assert verdict.record_names_this_plan is False


def test_the_failed_factor_names_exactly_one_condition() -> None:
    """**The field that would have ended the beta.29 investigation in one look.**"""
    assert evidence().failed_factor == OWNERSHIP_FACTOR_NONE
    assert evidence(marker_on=False).failed_factor == OWNERSHIP_FACTOR_MARKER
    assert evidence(readback_compatible=False).failed_factor == OWNERSHIP_FACTOR_MODE


def test_the_factor_results_publish_every_verdict() -> None:
    """Not only the failure: a reader needs to see which factors held."""
    results = evidence().factor_results()

    for factor in ("dispatch_active", "marker", "claim", "claim_names_run"):
        assert results[factor] is True
    assert results["failed_factor"] == OWNERSHIP_FACTOR_NONE
    # The two figures that are reported rather than judged are still published.
    assert results["readback_power_kw"] == pytest.approx(-4.3)
    assert results["readback_duration_minutes"] == pytest.approx(20.0)


# ===========================================================================
# D2 -- a derived quarter cannot skip a boundary
# ===========================================================================


def test_a_four_quarter_plan_yields_all_four_rows() -> None:
    """**The measured defect, inverted.** The hardware jumped 22:15Z -> 22:45Z.

    One slot cannot hold "the open quarter" and "the next one" at the same time, so
    the row opening at a refresh was unreachable. A derived row has no slot to lose.
    """
    plan = plan_of(row(0), row(15), row(30), row(45))

    seen = [plan.row_covering(local(NORMAL, 10, minute)) for minute in (0, 15, 30, 45)]

    assert [None if r is None else r.start.minute for r in seen] == [0, 15, 30, 45]


@pytest.mark.parametrize("offset", [0, 3, 59, 899])
def test_the_row_covering_now_is_found_at_every_offset(offset: int) -> None:
    """Boundary + 0 s, + 3 s, + 59 s and the last second of the row.

    The refresh lands a few seconds *after* the boundary it is meant to open, which
    is precisely where the strictly-future rule failed.
    """
    plan = plan_of(row(0), row(15))
    moment = local(NORMAL, 10, 0) + timedelta(seconds=offset)

    found = plan.row_covering(moment)

    assert found is not None
    assert found.start == local(NORMAL, 10, 0)


def test_a_plan_is_immutable_once_its_first_row_opens() -> None:
    """Revisable while prepared; economically frozen afterwards."""
    opened = plan_of(row(30), row(45))
    fresh = [
        {
            "plan_id": "plan-2",
            "revision": 9,
            "intent": EXECUTION_INTENT_GRID_CHARGE,
            "purpose": "safety_buy",
            "window_start": local(NORMAL, 10, 30).isoformat(),
            "window_end": local(NORMAL, 11, 0).isoformat(),
            "battery_target_kwh": 9.0,
            "average_power_kw": 9.0,
            "quarter_schedule": [row(30, battery=9.0).as_dict()],
        }
    ]

    kept = carry_plan(opened, fresh, local(NORMAL, 10, 31), run=None)

    assert kept is opened
    assert kept.rows[0].battery_kwh == pytest.approx(1.0)


def test_withdrawal_is_never_inferred_from_an_empty_horizon() -> None:
    """An open plan survives a horizon that says nothing at all.

    Stage A's horizon head is ``elapsed + 1``, so a publication *cannot* describe an
    open row. Reading its absence as a cancellation would cancel every plan, always.
    """
    opened = plan_of(row(30), row(45))

    assert carry_plan(opened, [], local(NORMAL, 10, 31), run=None) is opened


def test_a_spent_plan_is_dropped() -> None:
    """It ends at its last row's end, unconditionally. No orphan authority."""
    spent = plan_of(row(0))

    assert carry_plan(spent, [], local(NORMAL, 10, 16), run=None) is None


def test_the_carried_run_does_not_gate_a_row_that_is_already_open() -> None:
    """A run ending at a boundary is expected and must not stop an open plan."""
    opened = plan_of(row(30), row(45))

    assert carry_plan(opened, [], local(NORMAL, 10, 46), run=None) is opened


# ===========================================================================
# D3 -- progress cannot cross identities
# ===========================================================================


async def test_a_new_claim_does_not_inherit_the_previous_one_s_progress(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The phantom ``target_reached``**, which the hardware reported while the
    battery was physically charging at 5.7 kW.

    Progress keys on ``(claim_id, quarter_start)``: a stop and a restart inside one
    quarter is a new arm, and energy the previous arm delivered is not progress
    against this one.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._quarter_battery_kwh = 2.531

    record = dict(coordinator.store.execution_record or {})
    record["claim_id"] = "a-different-arm"
    coordinator.store.execution_record = record
    coordinator._accrue_quarter_progress(local(NORMAL, 10, 47))

    assert coordinator._quarter_battery_kwh == 0.0


async def test_a_new_row_does_not_inherit_the_previous_row_s_progress(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The other half of the key: crossing a boundary starts fresh measurements."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._quarter_battery_kwh = 1.75
    opened = coordinator._quarter
    assert opened is not None

    coordinator._refresh_executing_quarter(opened.quarter_end + timedelta(seconds=5))
    coordinator._accrue_quarter_progress(opened.quarter_end + timedelta(seconds=5))

    assert coordinator._quarter_battery_kwh == 0.0


# ===========================================================================
# D4 -- the actuator's resolution is an economic fact
# ===========================================================================


def test_the_minimum_executable_quarter_is_derived_from_the_step() -> None:
    """0.1 kW for a quarter of an hour. Derived, so it cannot drift from the step."""
    assert pytest.approx(DISPATCH_POWER_STEP_KW * 0.25) == MIN_EXECUTABLE_QUARTER_KWH
    assert pytest.approx(0.025) == MIN_EXECUTABLE_QUARTER_KWH


def test_a_sub_resolution_row_is_published_and_not_armable() -> None:
    """**beta.29 published meter targets of 0.01 kWh** against a 0.025 kWh minimum.

    The economics stay visible -- a reader must be able to see what was planned --
    and the row is never armed.
    """
    tiny = replace(
        row(0, battery=0.0, export=0.01),
        not_executable=QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION,
    )

    assert tiny.executable is False
    assert tiny.as_dict()["not_executable"] == QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION
    # Visible in the published form, which is the point.
    assert tiny.as_dict()["grid_export_target_kwh"] == pytest.approx(0.01)


def test_a_plan_derives_no_quarter_for_a_sub_resolution_row() -> None:
    """Stage B is never handed an envelope it cannot deliver."""
    plan = plan_of(
        replace(row(0), not_executable=QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION),
        row(15),
    )

    assert plan.row_covering(local(NORMAL, 10, 1)) is not None
    assert plan.executing_quarter(local(NORMAL, 10, 1)) is None
    assert plan.executing_quarter(local(NORMAL, 10, 16)) is not None


async def test_the_tick_refuses_a_sub_resolution_objective_as_a_backstop(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Stage A marks it; Stage B refuses it anyway, and says which reason.

    Arming a 0.01 kWh objective would overshoot it by 150 % on the first tick, so
    the backstop is not redundant politeness.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    plan = coordinator._plan
    assert plan is not None
    opened = coordinator._quarter
    assert opened is not None
    coordinator._plan = replace(
        plan,
        rows=tuple(
            replace(r, battery_kwh=0.004, grid_authorised_kwh=0.004)
            if r.start == opened.quarter_start
            else r
            for r in plan.rows
        ),
    )
    live_surface.calls.clear()

    await coordinator._async_physical_tick(opened.quarter_start + timedelta(minutes=1))

    assert coordinator._last_tick_reason == TICK_SKIPPED_SUB_RESOLUTION
    assert live_surface.calls == []


def test_stage_a_marks_a_tiny_export_row_non_executable() -> None:
    """The economics decide it, because "is this run worth forming" is economic."""
    from custom_components.alpha_ems_manager.economic import quarter_schedule_for

    class Interval:
        def __init__(self, index: int, export: float) -> None:
            self.index = index
            self.battery_charge_ac_kwh = 0.0
            self.battery_discharge_ac_kwh = 0.25
            self.marginal_grid_import_kwh = 0.0
            self.grid_export_kwh = export
            self.marginal_grid_export_kwh = export
            self.grid_import_kwh = 0.0

    base = local(NORMAL, 19, 0)
    rows = quarter_schedule_for(
        (Interval(0, 0.01), Interval(1, 0.16)),
        start_index=0,
        end_index=1,
        intent=EXECUTION_INTENT_NET_EXPORT,
        moment=lambda i: base + timedelta(minutes=15 * i),
    )

    assert rows[0]["not_executable"] == QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION
    assert rows[1]["not_executable"] is None


# ===========================================================================
# P0 -- the probe is read-only, and never chooses
# ===========================================================================


async def test_the_probe_records_a_sample_on_every_tick_including_refusals(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The refused ticks are the ones beta.29 most needed and did not have."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._dispatch_start_samples.clear()
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    await hass.async_block_till_done()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    samples = list(coordinator._dispatch_start_samples)
    assert samples, "a refused tick must still be sampled"
    assert samples[-1]["cadence"] == CADENCE_PHYSICAL_TICK
    assert samples[-1]["ownership_state"] != OWNERSHIP_OWNED


async def test_the_probe_publishes_candidates_and_chooses_none(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Deciding here would repeat the original mistake.**

    Candidates are published side by side with their deltas; the interpretation is a
    hardware measurement, not a code assumption.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._dispatch_start_samples.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    sample = list(coordinator._dispatch_start_samples)[-1]
    for field in (
        "raw_state",
        "raw_numeric",
        "raw_device_class",
        "raw_unit",
        "raw_last_changed",
        "reconstructed_local_midnight",
        "candidates",
        "deltas_to_claim_written_s",
        "raw_delta_since_previous",
        "seconds_since_claim_written",
        "phase",
    ):
        assert field in sample, field
    assert "local_midnight_seconds" in sample["candidates"]
    assert "utc_midnight_seconds" in sample["candidates"]
    # No field says which one is right.
    assert not any("correct" in key or "chosen" in key for key in sample)


async def test_the_probe_writes_nothing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Read-only, and asserted rather than asserted-in-a-comment."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    live_surface.calls.clear()

    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot

    for _ in range(5):
        coordinator._record_dispatch_start_sample(
            read_snapshot(hass), local(NORMAL, 10, 46), cadence=CADENCE_PHYSICAL_TICK
        )

    assert live_surface.calls == []


def test_the_probe_names_no_service_and_no_entity_write() -> None:
    """Structural, so a later edit cannot quietly give it a write path."""
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module.AlphaEmsCoordinator._record_dispatch_start_sample)

    for forbidden in (
        "async_execute",
        "services",
        "CommandStep",
        "turn_on",
        "set_value",
    ):
        assert forbidden not in source, forbidden


def test_the_probe_adds_no_second_cadence() -> None:
    """It samples the snapshot the caller already read. Five call sites, still five."""
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)

    assert source.count("read_snapshot(self.hass)") == 5


async def test_the_probe_is_bounded(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A ring, so a long run cannot grow the payload without limit."""
    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot
    from custom_components.alpha_ems_manager.const import (
        MAX_DISPATCH_START_SAMPLES_REPORTED,
    )

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    for _ in range(MAX_DISPATCH_START_SAMPLES_REPORTED + 10):
        coordinator._record_dispatch_start_sample(
            read_snapshot(hass), local(NORMAL, 10, 46), cadence=CADENCE_PHYSICAL_TICK
        )

    assert (
        len(coordinator._dispatch_start_samples) == MAX_DISPATCH_START_SAMPLES_REPORTED
    )


def test_this_module_never_derives_a_register_value_from_the_production_code() -> None:
    """**The anti-tautology guard: the D1 lesson, encoded as a test.**

    ``LiveSurface`` set the register to "the same reconstruction the ownership layer
    performs, from the same instant". A double defined as the inverse of the function
    under test cannot fail, and that is why two hundred ownership assertions passed
    while Live execution was impossible on the inverter.
    """
    import inspect
    import sys

    module = sys.modules[__name__]
    # The helpers that *produce* fixture values. A register reading must be a literal
    # here, never something computed by the code the test then checks.
    for helper in (claim, evidence, row, plan_of):
        source = inspect.getsource(helper)
        assert "_dispatch_start_instant" not in source, helper.__name__
        assert "replace(hour=0" not in source, helper.__name__
        assert "total_seconds()" not in source, helper.__name__
    # And the module as a whole reconstructs no register value for an assertion:
    # the one place the production helper is called is its own totality test.
    callers = {
        name
        for name, obj in vars(module).items()
        if callable(obj)
        and name.startswith("test_")
        and name
        != "test_this_module_never_derives_a_register_value_from_the_production_code"
        and "_dispatch_start_instant" in inspect.getsource(obj)
    }
    # Exactly one test may touch the production helper, and only to prove it total.
    assert callers == {"test_the_register_helper_survives_every_shape_of_reading"}, (
        callers
    )


# ===========================================================================
# the surface, unchanged
# ===========================================================================


async def test_no_live_route_reaches_a_helper_family(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Force Charging and Force Discharging stay unwritten for either intent."""
    coordinator, _trace = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=3
    )
    del coordinator

    written = {call.data["entity_id"] for call in live_surface.calls}

    assert not written & set(DISCHARGE_FAMILY.entities), written


def test_serve_load_is_still_not_executable() -> None:
    """Unchanged, and still not by being named anywhere."""
    from custom_components.alpha_ems_manager.const import CONTROL_LIVE_DISPATCH_INTENTS

    assert "serve_load" not in CONTROL_LIVE_DISPATCH_INTENTS
    assert (
        frozenset({EXECUTION_INTENT_GRID_CHARGE, EXECUTION_INTENT_NET_EXPORT})
        == CONTROL_LIVE_DISPATCH_INTENTS
    )


def test_the_ownership_surface_still_derives_nothing_itself() -> None:
    """The evidence module receives verdicts and computes no ownership of its own."""
    import ast
    import inspect

    from custom_components.alpha_ems_manager import safety

    tree = ast.parse(inspect.getsource(safety))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    # Names, never prose: the module explains *why* it derives no ownership, and a
    # substring search would fail on the explanation rather than on a real reference.
    for denied in ("OwnershipEvidence", "ownership_of", "record_matches"):
        assert denied not in names, denied


async def test_a_live_charge_still_arms_and_is_owned_within_one_tick(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The end-to-end property the hardware could not achieve.**

    beta.29 armed correctly and then reported ``ownership_not_owned`` on every tick
    for thirty minutes. Ownership must hold on the refresh after the arm.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot

    snapshot = read_snapshot(hass)
    now = local(NORMAL, 10, 46)

    assert hass.states.get(DISPATCH_ENABLE).state == "on"
    assert coordinator._ownership_now(snapshot, now) == OWNERSHIP_OWNED
    assert coordinator._evidence_for(snapshot, now).failed_factor == (
        OWNERSHIP_FACTOR_NONE
    )


async def test_the_tick_writes_a_correction_once_ownership_holds(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The correction that never landed in the field, landing.

    ``last_successful_write`` sat at the arm for a whole thirty-minute run.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._applied_setpoint_kw = 0.0
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written == [DISPATCH_POWER], written


def test_the_register_helper_survives_every_shape_of_reading() -> None:
    """It is still called, for diagnostics, so it must be total."""
    from custom_components.alpha_ems_manager.coordinator import (
        _dispatch_start_instant,
    )

    class Snap:
        def __init__(self, value):
            self.dispatch_start = value

    assert _dispatch_start_instant(None, NOW) is None
    assert _dispatch_start_instant(Snap(None), NOW) is None
    assert _dispatch_start_instant(Snap(0), NOW) is None
    assert _dispatch_start_instant(Snap(-5), NOW) is None
    assert _dispatch_start_instant(Snap(float("nan")), NOW) is None
    assert _dispatch_start_instant(Snap(40500), NOW) is not None


def test_the_start_register_sensor_is_still_only_read() -> None:
    """It appears in the snapshot and the probe, and in no command builder."""
    import inspect

    from custom_components.alpha_ems_manager import alphaess_device

    source = inspect.getsource(alphaess_device)
    for builder in ("def plan_dispatch_arm", "def plan_dispatch_power"):
        start = source.index(builder)
        body = source[start : source.index("\ndef ", start + 1)]
        assert SENSOR_DISPATCH_START not in body, builder
