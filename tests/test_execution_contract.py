"""The Stage-A to Stage-B execution contract.

**Nothing consumes this.** There is no Stage B, no actuator behind it, and
``CONTROL_EXECUTION_AVAILABLE`` is false. It is published now so Stage B can be
written against a contract that already exists, rather than inventing one and
discovering its ambiguities against a real inverter.

Three properties are the whole point, and each was a defect waiting to happen:

**Two boundaries, two fields.** The published run's ``energy_kwh`` changes meaning
with the action -- battery AC for a charge, *grid* export for an export. Measured on
the live installation, 1.3 kW of intended net export needs 2.2 kW of battery
against 0.9 kW of house load. A consumer handed one generic figure would command
1.3 kW and deliver 0.4.

**Absolute time.** ``start_interval`` is horizon-relative and moves every quarter
as the horizon advances. That is exactly the defect that made the beta.16 Activity
log announce the same run over and over, and Stage B would have inherited it.

**Identity that survives replanning.** A run being executed has less energy left
every quarter. That must not read as a different run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_BUCKET_KWH,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_HOLD,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_SERVE_LOAD,
    EXECUTION_INTENTS,
    EXECUTION_TARGET_STALE_MINUTES,
)
from custom_components.alpha_ems_manager.economic import (
    EconomicRun,
    execution_intent,
    execution_revision,
    execution_target,
)

OPENS = datetime(2026, 8, 22, 18, 30, tzinfo=UTC)
CLOSES = OPENS + timedelta(minutes=60)
#: When the plan was issued. Since beta.19 freshness hangs off this, not off the
#: window: a run many hours out must not carry a freshness deadline many hours out.
ISSUED = OPENS - timedelta(hours=3)
STALE = ISSUED + timedelta(minutes=EXECUTION_TARGET_STALE_MINUTES)


def run_of(
    *,
    action: str,
    charge: float = 0.0,
    discharge: float = 0.0,
    grid_import: float = 0.0,
    grid_export: float = 0.0,
    intervals: int = 4,
) -> EconomicRun:
    """Return one run with the boundaries set explicitly."""
    return EconomicRun(
        action=action,
        start_index=42,
        end_index=45,
        interval_count=intervals,
        battery_charge_ac_kwh=charge,
        battery_discharge_ac_kwh=discharge,
        grid_import_kwh=grid_import,
        grid_export_kwh=grid_export,
        pv_curtailed_kwh=0.0,
        first_power_kw=(charge + discharge) / (intervals * 0.25) if intervals else 0.0,
        net_cash_flow_eur=0.0,
        min_price_eur_kwh=0.10,
        max_price_eur_kwh=0.40,
        average_price_eur_kwh=0.25,
        marginal_grid_import_kwh=grid_import,
        marginal_grid_export_kwh=grid_export,
        marginal_cost_eur=-1.0,
        direction="charge" if charge else "discharge",
        charged_switching_fee=True,
    )


def target_of(run: EconomicRun, **overrides) -> dict:
    """Return the published target for one run.

    ``issued_at`` and ``stale_after`` are anchored to each other rather than to
    the window, which is the beta.19 correction: freshness asks how old an
    instruction is, and that cannot depend on when the instruction is for.
    """
    return execution_target(
        run,
        window_start=OPENS,
        window_end=CLOSES,
        reserve_floor_kwh=overrides.pop("reserve_floor_kwh", 4.4),
        issued_at=overrides.pop("issued_at", ISSUED),
        stale_after=overrides.pop("stale_after", ISSUED + timedelta(minutes=30)),
        **overrides,
    )


# ===========================================================================
# A. the boundary distinction -- the hard requirement
# ===========================================================================


def test_a_charge_target_is_battery_side_and_names_no_grid_target() -> None:
    """The charge setpoint is a battery figure. House load is not added to it.

    Measured live: 3.7 kW of battery charging against 1.1 kW of house and 0.63 kW
    of production drew 4.17 kW from the meter. Stage A knows all three; Stage B is
    handed the battery figure, because that is what the inverter takes.
    """
    target = target_of(
        run_of(action=ECONOMIC_ACTION_CHARGE, charge=5.0, grid_import=4.2)
    )

    assert target["intent"] == EXECUTION_INTENT_GRID_CHARGE
    assert target["battery_target_kwh"] == pytest.approx(5.0)
    # Not the grid figure, and not present at all: a consumer must not be able to
    # pick whichever field happens to be non-zero.
    assert target["grid_target_kwh"] is None
    # The expected grid consequence is still published -- for reporting, not as a
    # setpoint.
    assert target["expected_grid_import_kwh"] == pytest.approx(4.2)


def test_a_net_export_target_names_both_boundaries() -> None:
    """Export is paid at the meter, so the meter figure must be explicit.

    2.2 kWh of battery delivering 1.3 kWh to the grid: both appear, each labelled,
    and they are not equal. A Stage B that commanded the grid figure at the
    battery would deliver well under half.
    """
    target = target_of(
        run_of(action=ECONOMIC_ACTION_EXPORT, discharge=2.2, grid_export=1.3)
    )

    assert target["intent"] == EXECUTION_INTENT_NET_EXPORT
    assert target["battery_target_kwh"] == pytest.approx(2.2)
    assert target["grid_target_kwh"] == pytest.approx(1.3)
    assert target["battery_target_kwh"] > target["grid_target_kwh"]


def test_a_load_serving_discharge_is_a_different_intent_from_export() -> None:
    """Same physical direction, different target. The distinction Stage B needs."""
    serving = target_of(
        run_of(action=ECONOMIC_ACTION_DISCHARGE, discharge=1.0, grid_export=0.0)
    )

    assert serving["intent"] == EXECUTION_INTENT_SERVE_LOAD
    assert serving["battery_target_kwh"] == pytest.approx(1.0)
    assert serving["grid_target_kwh"] is None


def test_the_boundary_rule_is_stated_in_the_payload() -> None:
    """A machine-readable contract still has to be readable."""
    target = target_of(
        run_of(action=ECONOMIC_ACTION_EXPORT, discharge=2.2, grid_export=1.3)
    )

    rule = target["boundary_rule"]
    assert "NOT added" in rule
    assert "meter" in rule and "battery" in rule


def test_a_safety_buy_is_a_grid_charge_with_its_purpose_preserved() -> None:
    """Stage B needs the physical intent; a human needs to know why."""
    target = target_of(
        run_of(action=ECONOMIC_ACTION_CHARGE, charge=3.0, grid_import=3.0),
        safety_buy=True,
    )

    assert target["intent"] == EXECUTION_INTENT_GRID_CHARGE
    assert target["purpose"] == ECONOMIC_ACTION_SAFETY_BUY


def test_curtailment_never_claims_an_actuator_that_does_not_exist() -> None:
    """No primitive can decline production, so no intent may imply one."""
    assert execution_intent(run_of(action=ECONOMIC_ACTION_CURTAIL)) == (
        EXECUTION_INTENT_HOLD
    )
    assert (
        execution_intent(run_of(action=ECONOMIC_ACTION_HOLD)) == EXECUTION_INTENT_HOLD
    )
    assert "curtail" not in " ".join(EXECUTION_INTENTS)


def test_every_intent_is_in_the_documented_vocabulary() -> None:
    """No target may carry an intent the constant set does not name."""
    for action in (
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_DISCHARGE,
        ECONOMIC_ACTION_EXPORT,
        ECONOMIC_ACTION_CURTAIL,
        ECONOMIC_ACTION_HOLD,
        ECONOMIC_ACTION_SAFETY_BUY,
    ):
        assert execution_intent(run_of(action=action)) in EXECUTION_INTENTS


# ===========================================================================
# B. absolute time, and staleness
# ===========================================================================


def test_the_window_is_absolute_and_no_index_appears() -> None:
    """The beta.16 defect, refused by construction.

    ``start_index`` is 42 on this run and must be nowhere in the payload: an index
    is relative to a horizon that advances every quarter and to a civil day that
    can be 92, 96 or 100 intervals long.
    """
    target = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=1.0))

    assert target["window_start"] == OPENS.isoformat()
    assert target["window_end"] == CLOSES.isoformat()
    assert "42" not in str({k: v for k, v in target.items() if k not in ("plan_id",)})
    for forbidden in ("start_interval", "end_interval", "index"):
        assert forbidden not in target


def test_stale_after_is_anchored_to_the_issue_instant_not_the_window() -> None:
    """**The beta.19 correction, and the reason it mattered.**

    beta.18 derived ``stale_after`` from ``window_start``, which made it useless
    for the one job its name describes. This run opens three hours after the plan
    was issued, so the beta.18 rule would have called it fresh until three and a
    half hours from now -- long after any ordinary meaning of the word.

    Freshness asks how old an instruction is. That cannot depend on when the
    instruction is *for*, and the window remains a separate published fact.
    """
    target = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=1.0))

    assert target["issued_at"] == ISSUED.isoformat()
    assert target["stale_after"] == STALE.isoformat()
    assert EXECUTION_TARGET_STALE_MINUTES >= 30
    # The whole point: the deadline is near the issue instant, not near the window.
    assert target["stale_after"] < target["window_start"]
    # And the window is still published independently.
    assert target["window_start"] == OPENS.isoformat()
    assert target["window_end"] == CLOSES.isoformat()


def test_the_contract_says_what_consumes_it_and_what_executes() -> None:
    """Stage B reads this, and since beta.25 it executes part of it.

    **The claim had to change because the release did.** "Nothing executes" was
    the honest disclaimer through beta.23 and is now false: an authorised charge
    reaches the inverter. What the rule must still do is say *which* half is
    executable and which is refused -- a contract that claimed either "nothing
    executes" or simply "this executes" would mislead in opposite directions.
    """
    target = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=1.0))

    rule = target["contract_rule"]
    assert "Stage B" in rule
    # The executable half, named with the surface and the direction.
    assert "charge" in rule
    assert "mode 2" in rule
    assert "negative power" in rule
    # And the refused half, named too.
    for refused in ("discharge", "export", "curtailment"):
        assert refused in rule, refused
    assert "refused" in rule
    # Freshness is enforced from beta.19, so the old disclaimer would be false.
    assert "enforced by nothing" not in rule


# ===========================================================================
# C. identity and revision
# ===========================================================================


def test_the_same_run_keeps_its_identity_as_its_energy_shrinks() -> None:
    """**The property Stage B depends on.**

    A run being executed has less left to do every quarter. That is progress, not
    a new plan, and the identifier must not move -- otherwise a future Stage B
    would abandon and restart its own dispatch every fifteen minutes.
    """
    first = target_of(
        run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0, grid_import=6.0)
    )
    later = target_of(
        run_of(action=ECONOMIC_ACTION_CHARGE, charge=2.5, grid_import=2.5)
    )

    assert first["plan_id"] == later["plan_id"]


def test_a_different_start_instant_is_a_different_plan() -> None:
    """Identity is the intent and the instant, so a moved window is a new run."""
    original = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))
    shifted = execution_target(
        run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0),
        window_start=OPENS + timedelta(minutes=15),
        window_end=CLOSES,
        reserve_floor_kwh=4.4,
        issued_at=ISSUED,
        stale_after=STALE,
    )

    assert original["plan_id"] != shifted["plan_id"]


def test_a_different_intent_at_the_same_instant_is_a_different_plan() -> None:
    """Charging and exporting at 18:30 are not two revisions of one thing."""
    charging = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))
    exporting = target_of(
        run_of(action=ECONOMIC_ACTION_EXPORT, discharge=6.0, grid_export=4.0)
    )

    assert charging["plan_id"] != exporting["plan_id"]


def test_the_first_publication_is_revision_one() -> None:
    """Nothing to compare against means the beginning, not a continuation."""
    target = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))

    assert execution_revision(None, target) == 1


def test_noise_below_the_deadband_does_not_bump_the_revision() -> None:
    """**The control-jitter guard.**

    A revision that incremented on floating-point drift would make a future Stage
    B re-plan continuously. The deadband is one state-space bucket, the same
    quantity the Activity surface uses and for the same reason.
    """
    before = target_of(
        run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0, grid_import=6.0)
    )
    before["revision"] = 3
    nudged = target_of(
        run_of(
            action=ECONOMIC_ACTION_CHARGE,
            charge=6.0 + ECONOMIC_BUCKET_KWH / 2.0,
            grid_import=6.0,
        )
    )

    assert execution_revision(before, nudged) == 3


def test_a_material_move_bumps_the_revision() -> None:
    """Beyond the deadband is a real change, and Stage B has to be told."""
    before = target_of(
        run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0, grid_import=6.0)
    )
    before["revision"] = 3
    moved = target_of(
        run_of(
            action=ECONOMIC_ACTION_CHARGE,
            charge=6.0 + ECONOMIC_BUCKET_KWH * 4.0,
            grid_import=6.0,
        )
    )

    assert execution_revision(before, moved) == 4


def test_a_moved_window_end_bumps_the_revision() -> None:
    """The window is part of the target, so lengthening it is a revision."""
    before = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))
    before["revision"] = 2
    longer = execution_target(
        run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0),
        window_start=OPENS,
        window_end=CLOSES + timedelta(minutes=30),
        reserve_floor_kwh=4.4,
        issued_at=ISSUED,
        stale_after=STALE,
    )

    assert execution_revision(before, longer) == 3


def test_a_new_plan_id_restarts_the_numbering() -> None:
    """A different run does not continue someone else's revisions."""
    before = target_of(run_of(action=ECONOMIC_ACTION_CHARGE, charge=6.0))
    before["revision"] = 7
    other = target_of(
        run_of(action=ECONOMIC_ACTION_EXPORT, discharge=6.0, grid_export=4.0)
    )

    assert execution_revision(before, other) == 1


# ===========================================================================
# D. the reserve travels with the target
# ===========================================================================


def test_the_reserve_floor_accompanies_every_target() -> None:
    """Stage B must never have to ask what the hard floor was.

    The floor is a safety quantity and it belongs in the same message as the
    energy target, so no consumer can act on one without the other.
    """
    target = target_of(
        run_of(action=ECONOMIC_ACTION_EXPORT, discharge=8.0, grid_export=6.0),
        reserve_floor_kwh=15.5,
    )

    assert target["reserve_floor_kwh"] == pytest.approx(15.5)


def test_the_published_target_is_json_shaped() -> None:
    """A machine-readable contract has to survive serialisation."""
    import json

    target = target_of(
        run_of(action=ECONOMIC_ACTION_EXPORT, discharge=2.2, grid_export=1.3)
    )
    target["revision"] = 1

    restored = json.loads(json.dumps(target))
    assert restored == target
