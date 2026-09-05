"""beta.43: representable is not the same as controllable.

``MIN_EXECUTABLE_QUARTER_KWH`` answers *can the actuator express this number?* -- one
0.1 kW step held for a quarter, 0.025 kWh. It has never answered the other question,
and the live 2026-09-05 capture is what made the difference matter.

A ``net_export`` row was published with a 0.04 kWh meter objective, 0.16 kW average,
and armed. The controller's own deadband is 0.2 kW: over a quarter that is 0.05 kWh of
meter movement it deliberately does not correct, already larger than the whole
objective. The row's incremental export revenue was 0.04 x 0.20745 = 0.0083 EUR, while
the 0.21 kWh of avoided import beside it -- the great majority of its published value
-- needed no dispatch at all.

So the floor added here is derived from the two constants that bound the control loop
and from nothing else, and it is asked of ``net_export`` only. The charge side is a
campaign-level question with authorised PV absorption on the other side of it, and is
deliberately left alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.alpha_ems_manager.const import (
    DISPATCH_POWER_DEADBAND_KW,
    DISPATCH_POWER_STEP_KW,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_EXPORT,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    MIN_CONTROLLABLE_QUARTER_KWH,
    MIN_EXECUTABLE_QUARTER_KWH,
    QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE,
    QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION,
)
from custom_components.alpha_ems_manager.economic import (
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    EconomicInterval,
    EconomicRun,
    execution_target,
)

START = datetime(2026, 9, 5, 20, 30, tzinfo=UTC)


def _export_run(*, export_kwh: float, intervals: int = 1) -> EconomicRun:
    """Return a real discharge run whose meter objective is ``export_kwh``."""
    return EconomicRun(
        action=ECONOMIC_ACTION_EXPORT,
        start_index=82,
        end_index=82 + intervals - 1,
        interval_count=intervals,
        battery_charge_ac_kwh=0.0,
        battery_discharge_ac_kwh=0.25 * intervals,
        grid_import_kwh=0.0,
        grid_export_kwh=export_kwh,
        pv_curtailed_kwh=0.0,
        first_power_kw=1.0,
        net_cash_flow_eur=0.01,
        min_price_eur_kwh=0.20,
        max_price_eur_kwh=0.21,
        average_price_eur_kwh=0.207,
        marginal_grid_import_kwh=-0.21,
        marginal_cost_eur=-0.08,
        direction=ECONOMIC_DIRECTION_DISCHARGE,
    )


def _charge_run(*, battery_kwh: float) -> EconomicRun:
    """Return a real charge run whose battery objective is ``battery_kwh``."""
    return EconomicRun(
        action=ECONOMIC_ACTION_CHARGE,
        start_index=82,
        end_index=82,
        interval_count=1,
        battery_charge_ac_kwh=battery_kwh,
        battery_discharge_ac_kwh=0.0,
        grid_import_kwh=battery_kwh,
        grid_export_kwh=0.0,
        pv_curtailed_kwh=0.0,
        first_power_kw=battery_kwh / 0.25,
        net_cash_flow_eur=-0.01,
        min_price_eur_kwh=0.12,
        max_price_eur_kwh=0.13,
        average_price_eur_kwh=0.125,
        marginal_grid_import_kwh=battery_kwh,
        marginal_cost_eur=0.01,
        direction=ECONOMIC_DIRECTION_CHARGE,
    )


def _interval(index: int, *, export_kwh: float, battery_kwh: float) -> EconomicInterval:
    """Return one solved interval, as the optimizer produces them."""
    charging = battery_kwh > 0.0
    return EconomicInterval(
        index=index,
        action=ECONOMIC_ACTION_CHARGE if charging else ECONOMIC_ACTION_EXPORT,
        start_energy_dc_kwh=14.0,
        battery_delta_dc_kwh=battery_kwh if charging else -0.25,
        battery_charge_ac_kwh=battery_kwh if charging else 0.0,
        battery_discharge_ac_kwh=0.0 if charging else 0.25,
        grid_import_kwh=battery_kwh if charging else 0.0,
        grid_export_kwh=export_kwh,
        pv_curtailed_kwh=0.0,
        cost_eur=0.01,
        import_price_eur_kwh=0.383,
        export_price_eur_kwh=0.207,
        run_start=index == 82,
        idle_import_kwh=0.0 if charging else 0.21,
    )


def _moment(index: int):
    return START + timedelta(minutes=15 * (index - 82))


def _target(run: EconomicRun, *, intervals: int = 1) -> dict:
    export_each = run.grid_export_kwh / intervals
    battery_each = run.battery_charge_ac_kwh / intervals
    solved = tuple(
        _interval(82 + offset, export_kwh=export_each, battery_kwh=battery_each)
        for offset in range(intervals)
    )
    return execution_target(
        run,
        window_start=START,
        window_end=START + timedelta(minutes=15 * intervals),
        reserve_floor_kwh=5.27,
        issued_at=START,
        stale_after=START + timedelta(minutes=30),
        intervals=solved,
        moment=_moment,
    )


def test_the_two_floors_are_separate_and_the_controllable_one_is_larger() -> None:
    """**The resolution constant is not renamed, repurposed or moved.**

    It keeps answering the question it has always answered. The new one is derived
    from the deadband the controller will not correct inside plus one step it cannot
    express, so neither can drift from the hardware figure it comes from.
    """
    assert pytest.approx(DISPATCH_POWER_STEP_KW * 0.25) == MIN_EXECUTABLE_QUARTER_KWH
    assert (
        pytest.approx((DISPATCH_POWER_DEADBAND_KW + DISPATCH_POWER_STEP_KW) * 0.25)
        == MIN_CONTROLLABLE_QUARTER_KWH
    )
    assert MIN_CONTROLLABLE_QUARTER_KWH > MIN_EXECUTABLE_QUARTER_KWH
    # The deadband alone already exceeds the resolution floor, which is the whole
    # reason one constant could not answer both questions.
    assert DISPATCH_POWER_DEADBAND_KW * 0.25 > MIN_EXECUTABLE_QUARTER_KWH


def test_the_live_tiny_export_row_is_published_and_not_armable() -> None:
    """The 2026-09-05 row: 0.04 kWh at the meter, 0.16 kW average.

    *Mutation: drop the controllability clause and this row arms again.*
    """
    target = _target(_export_run(export_kwh=0.04))
    assert target["intent"] == EXECUTION_INTENT_NET_EXPORT
    rows = target["quarter_schedule"]
    assert len(rows) == 1
    assert rows[0]["grid_export_target_kwh"] == pytest.approx(0.04), (
        "the witness: the economics stay visible, which is the point"
    )
    assert rows[0]["not_executable"] == QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE
    # And it is *not* reported as the older, different refusal.
    assert rows[0]["not_executable"] != QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION


def test_a_material_export_row_is_unchanged() -> None:
    """A row the loop can hold arms exactly as before.

    0.30 kWh over one quarter is 1.2 kW at the meter -- six deadbands -- and this is
    the case the release must not touch: Stage B tracked 0.25 and 1.02 kWh objectives
    to within 0.017 and 0.047 kWh live.
    """
    target = _target(_export_run(export_kwh=0.30))
    rows = target["quarter_schedule"]
    assert rows[0]["not_executable"] is None
    assert rows[0]["grid_export_target_kwh"] == pytest.approx(0.30)


def test_the_floor_is_asked_per_quarter_not_per_run() -> None:
    """A 0.10 kWh run over two quarters is 0.05 kWh in each, and neither is steerable.

    The deadband is a per-quarter property, so a run total that looks respectable
    while every one of its quarters sits inside the uncorrected band is exactly the
    shape that produced tracking error larger than the objective.
    """
    target = _target(_export_run(export_kwh=0.10, intervals=2), intervals=2)
    rows = target["quarter_schedule"]
    assert len(rows) == 2
    assert all(
        row["grid_export_target_kwh"] < MIN_CONTROLLABLE_QUARTER_KWH for row in rows
    ), "the witness: each quarter is below the floor"
    assert all(
        row["not_executable"] == QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE
        for row in rows
    )


def test_a_small_charge_row_is_not_gated_by_the_export_floor() -> None:
    """**The direction asymmetry, and it is deliberate.**

    A charge objective is battery-side, its rows are usually continuations inside one
    arm, and a row carrying ``retention_authorised`` is how authorised PV absorption
    reaches the pack -- 62 % of the live charge campaign's energy. Gating it per row
    on an export-shaped floor would forfeit that for no dispatch saving at all.

    *Mutation: drop the ``net_export`` condition from the clause and this fails.*
    """
    battery_kwh = 0.04
    assert battery_kwh > MIN_EXECUTABLE_QUARTER_KWH
    assert battery_kwh < MIN_CONTROLLABLE_QUARTER_KWH, (
        "the witness: this is exactly the magnitude the export side refuses"
    )
    target = _target(_charge_run(battery_kwh=battery_kwh))
    assert target["intent"] == EXECUTION_INTENT_GRID_CHARGE
    rows = target["quarter_schedule"]
    assert rows[0]["not_executable"] is None, (
        "a charge of the same size still arms; the floor is an export rule"
    )


def test_a_sub_resolution_export_row_still_reports_the_older_refusal() -> None:
    """The two refusals stay distinguishable, which is why they are two words.

    Sub-resolution means the actuator cannot say the number; below-controllable means
    the loop cannot hold it. A reader has to be able to tell them apart, and the
    ordering of the clauses is what keeps the narrower one visible.
    """
    target = _target(_export_run(export_kwh=0.01))
    assert (
        target["quarter_schedule"][0]["not_executable"]
        == QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION
    )


def test_stage_b_asks_the_same_floor_stage_a_did() -> None:
    """The backstop is a backstop, and it must not ask an easier question.

    Two copies of a floor is one too many, and the copy that drifted would be this
    one: it had to be kept in step with the resolution constant by hand.

    *Mutation: return the resolution floor for both intents and this fails.*
    """
    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    export = SimpleNamespace(intent=EXECUTION_INTENT_NET_EXPORT)
    charge = SimpleNamespace(intent=EXECUTION_INTENT_GRID_CHARGE)
    ask = AlphaEmsCoordinator._min_armable_kwh

    assert ask(None, export) == pytest.approx(MIN_CONTROLLABLE_QUARTER_KWH)
    assert ask(None, charge) == pytest.approx(MIN_EXECUTABLE_QUARTER_KWH)
    assert ask(None, export) > ask(None, charge), (
        "the asymmetry Stage A applies is the asymmetry Stage B enforces"
    )
