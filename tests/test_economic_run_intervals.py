"""The per-quarter allocation breakdown, and saying peak apart from mean.

**Why this exists.** A window and a total cannot distinguish "the campaign spans
thirteen quarters" from "energy is spread across thirteen quarters", and those are
different plans with different economics. An investigation into an apparently
diluted 14.5 kWh export spent its whole length on that ambiguity, and the answer
turned out to be visible in one field per quarter.

Nothing here changes an allocation. Every figure published is read off the solved
plan -- the marginal quantities are existing per-interval properties, exact
against each interval's own idle counterfactual -- so these tests assert that the
breakdown *describes* the plan and that the plan is unchanged by describing it.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.activity import next_activity
from custom_components.alpha_ems_manager.battery import build_limits
from custom_components.alpha_ems_manager.const import (
    BATTERY_KWH_PRECISION,
    ECONOMIC_ACTION_CHARGE,
    MAX_ECONOMIC_RUN_INTERVALS_REPORTED,
    MAX_ECONOMIC_RUNS_REPORTED,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    build_horizon,
    build_outcome,
    build_physics_table,
    economic_as_dict,
    select_bucket_kwh,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_activity_announcements import NOW, make_run

# A midday production bell with two clearly cheapest quarters, then a dear tail.
# This is the shape that produced the misleading figure: real buying at full power
# in two quarters, free absorption in eleven more, reported as one campaign
# averaging 3.50 kW.
PV = [
    0.0,
    0.1,
    0.3,
    0.6,
    0.9,
    1.1,
    1.3,
    1.4,
    1.5,
    1.5,
    1.4,
    1.3,
    1.1,
    0.9,
    0.6,
    0.3,
    0.1,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
]
CHEAPEST = (2, 3)


def outcome_for(*, start_kwh: float | None = None, count: int = 24):
    """Return a solved outcome on the reference installation."""
    limits, reason = build_limits(
        capacity_kwh=22.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        round_trip_efficiency_percent=90.0,
    )
    assert reason is None
    floor = limits.energy_for_soc(20.0)
    bucket, rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    table = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)
    imports = [0.30] * count
    exports = [0.24] * count
    for index in CHEAPEST:
        imports[index], exports[index] = 0.05, 0.02
    for index in range(20, count):
        imports[index], exports[index] = 0.70, 0.62
    horizon = build_horizon(
        demands=tuple(
            IntervalDemand(index=i, baseline_kwh=0.25, pv_kwh=PV[i])
            for i in range(count)
        ),
        prices=tuple(
            IntervalPrice(import_eur_kwh=imports[i], export_eur_kwh=exports[i])
            for i in range(count)
        ),
        required_reserve_kwh=tuple([floor] * count),
        table=table,
    )
    return build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=floor + 1.0 if start_kwh is None else start_kwh,
        terminal_floor_kwh=floor,
        floor_energy_kwh=floor,
        minimum_trade_gain_eur=0.10,
        allow_grid_charging=True,
        allow_battery_export=True,
        reserve_above_capacity_kwh=0.0,
        table_ms=0.0,
        bucket_rule=rule,
    )


def payload_for(**kwargs):
    """Return the published economic payload."""
    return economic_as_dict(outcome_for(**kwargs), execution_blocked_reason="barrier")


# ===========================================================================
# The breakdown resolves the ambiguity it exists for
# ===========================================================================


def test_a_wide_charge_window_is_two_quarters_of_buying_and_eleven_of_sun() -> None:
    """**The case the breakdown was added for, asserted end to end.**

    From the run alone this reads as a thirteen-quarter campaign averaging
    3.50 kW. Per quarter it is two quarters at full power that bought, and eleven
    that stored production and bought nothing. Both descriptions are of the same
    plan; only one of them can be acted on.
    """
    payload = payload_for()
    run = next(r for r in payload["runs"] if r["action"] == ECONOMIC_ACTION_CHARGE)

    assert run["interval_count"] == len(run["intervals"])
    assert run["average_power_kw"] < run["peak_power_kw"]

    buying = [row for row in run["intervals"] if not row["absorbing"]]
    absorbing = [row for row in run["intervals"] if row["absorbing"]]

    # The buying is concentrated in exactly the cheapest quarters, at full power.
    assert tuple(row["interval"] for row in buying) == CHEAPEST
    for row in buying:
        assert row["battery_power_kw"] == pytest.approx(10.0, abs=5e-3)
        assert row["marginal_grid_import_kwh"] > 1.0
        assert row["import_price_eur_kwh"] == 0.05

    # And every other quarter of the reported window bought nothing at all.
    assert absorbing
    for row in absorbing:
        assert row["marginal_grid_import_kwh"] == pytest.approx(0.0, abs=1e-9)
        assert row["battery_charge_ac_kwh"] > 0.0


def test_every_quarter_of_a_run_is_published_in_order() -> None:
    """Contiguous and complete, or the window cannot be audited against it."""
    payload = payload_for()
    for run in payload["runs"]:
        indices = [row["interval"] for row in run["intervals"]]
        assert indices == list(range(run["start_interval"], run["end_interval"] + 1))
        assert run["intervals_omitted"] == 0


def test_the_rows_reconcile_with_the_run_totals() -> None:
    """The breakdown is a reading of the run, so it must add up to it.

    A breakdown that did not reconcile would be a second source of truth, and the
    published totals are what every other consumer reads.

    **The underlying figures reconcile exactly** -- measured, to the last bit --
    so the only slack allowed here is publication rounding. Energies publish at
    ``BATTERY_KWH_PRECISION``, which puts each row within half a unit of the last
    published decimal, so a sum of ``n`` rows is within ``n`` halves of the total.
    The tolerance is derived from the constant rather than written as a number, so
    changing the precision cannot silently loosen this test, and a genuine
    reconciliation failure cannot hide inside a generous epsilon.
    """
    step = 10.0**-BATTERY_KWH_PRECISION
    payload = payload_for()
    for run in payload["runs"]:
        rows = run["intervals"]
        drift = 0.5 * step * len(rows) + 1e-9
        assert sum(r["battery_charge_ac_kwh"] for r in rows) == pytest.approx(
            run["battery_charge_ac_kwh"], abs=drift
        )
        assert sum(r["battery_discharge_ac_kwh"] for r in rows) == pytest.approx(
            run["battery_discharge_ac_kwh"], abs=drift
        )
        assert sum(r["marginal_grid_import_kwh"] for r in rows) == pytest.approx(
            run["marginal_grid_import_kwh"], abs=drift
        )
        # The peak is a max of the same rows, so it needs no such allowance.
        peak = max(r["battery_power_kw"] for r in rows)
        assert peak == pytest.approx(run["peak_power_kw"], abs=5e-3)


def test_each_quarter_carries_its_own_reserve_requirement() -> None:
    """So a low-power tail can be attributed to the reserve rather than guessed.

    The reserve requirement is pointwise and non-monotone; without it beside the
    allocation, a reserve-driven trickle and a diluted campaign look identical.
    """
    payload = payload_for()
    for run in payload["runs"]:
        for row in run["intervals"]:
            assert row["reserve_requirement_kwh"] is not None
            assert row["reserve_requirement_kwh"] > 0.0


def test_direction_is_the_action_and_power_is_unsigned() -> None:
    """The same convention the control surface uses, and for the same reason.

    A signed power would be a second way to express direction, and the two would
    eventually disagree.
    """
    payload = payload_for()
    for run in payload["runs"]:
        for row in run["intervals"]:
            assert row["battery_power_kw"] >= 0.0
            assert row["battery_charge_ac_kwh"] >= 0.0
            assert row["battery_discharge_ac_kwh"] >= 0.0
            assert row["action"]


# ===========================================================================
# Bounded, and it says so when it truncates
# ===========================================================================


def test_the_payload_never_serialises_the_whole_trajectory() -> None:
    """Bounded by construction, and the bound is the point.

    A hundred and ninety-two rows was rightly refused; the fix is a budget, not
    an exemption.
    """
    payload = payload_for()
    rows = sum(len(run["intervals"]) for run in payload["runs"])
    assert rows <= MAX_ECONOMIC_RUN_INTERVALS_REPORTED
    assert len(payload["runs"]) <= MAX_ECONOMIC_RUNS_REPORTED


def test_truncation_is_reported_rather_than_silent() -> None:
    """A short list must not read as a short campaign.

    Driven by shrinking the budget rather than by constructing a pathological
    plan, because the behaviour under test is the disclosure.
    """
    import custom_components.alpha_ems_manager.economic as economic_module

    original = economic_module.MAX_ECONOMIC_RUN_INTERVALS_REPORTED
    try:
        economic_module.MAX_ECONOMIC_RUN_INTERVALS_REPORTED = 4
        payload = payload_for()
    finally:
        economic_module.MAX_ECONOMIC_RUN_INTERVALS_REPORTED = original

    rows = sum(len(run["intervals"]) for run in payload["runs"])
    assert rows == 4
    omitted = sum(run["intervals_omitted"] for run in payload["runs"])
    assert omitted > 0
    # The first run is complete or explicitly short; either way the count is
    # stated, so nothing reads as an allocation that was not made.
    for run in payload["runs"]:
        assert len(run["intervals"]) + run["intervals_omitted"] >= 0
        if run["intervals_omitted"]:
            assert len(run["intervals"]) < run["interval_count"]


def test_publishing_the_breakdown_changes_no_allocation() -> None:
    """Observability only. Serialising twice must not perturb the plan."""
    outcome = outcome_for()
    before = [
        (e.action, e.battery_charge_ac_kwh, e.battery_discharge_ac_kwh)
        for e in outcome.desired.intervals
    ]
    economic_as_dict(outcome, execution_blocked_reason="barrier")
    economic_as_dict(outcome, execution_blocked_reason="barrier")
    after = [
        (e.action, e.battery_charge_ac_kwh, e.battery_discharge_ac_kwh)
        for e in outcome.desired.intervals
    ]
    assert before == after


# ===========================================================================
# The peak and the mean are different figures -- and neither is Activity's
# ===========================================================================
#
# **Rewritten for beta.31.** These cases used to assert that the Activity line
# read "peak 8.00 kW, campaign average 4.46 kW", which fixed a real fault: "4.46
# kW average" read as the dispatch intensity for a campaign that ran at 8 kW
# through the peak and 1 kW in a reserve tail.
#
# The fix was right and its *location* was wrong. Activity is a plan lifecycle in
# one line, and a reader asking how a campaign was shaped is asking something a
# table answers better than a sentence can. So the two figures are asserted where
# a reader now finds them, and Activity is asserted to carry neither -- which it
# cannot, because ``RunContent`` has no power field to print.


def test_the_published_run_names_the_peak_beside_the_mean() -> None:
    """**The misleading figure, corrected -- on the surface that can carry it.**

    This is the same thirteen-quarter campaign the top of this file dissects: two
    quarters at full power that bought, and eleven that stored production. A single
    mean describes it badly, so all three figures are published per run and each is
    named for what it is.
    """
    runs = payload_for()["runs"]
    assert runs, "the campaign must publish a run for the figures to be read from"
    run = runs[0]

    assert run["peak_power_kw"] is not None
    assert run["average_power_kw"] is not None
    assert run["first_power_kw"] is not None
    # Genuinely different on a varying campaign, or the distinction would be
    # untested whatever the keys say.
    assert run["peak_power_kw"] > run["average_power_kw"]


def test_the_activity_line_carries_no_power_at_all() -> None:
    """Detail belongs in diagnostics. Activity is a timeline, not a table.

    Asserted structurally as well as on the string: the input carries no power, so
    there is no path from a solved run's power shape to an Activity sentence.
    """
    from custom_components.alpha_ems_manager.activity import RunContent

    entry = next_activity(previous=None, runs=(make_run(start_minutes=10),), now=NOW)
    assert entry is not None

    assert "kW" not in entry.message.replace("kWh", "")
    assert "peak" not in entry.message
    assert "average" not in entry.message
    assert "interval" not in entry.message
    for field in ("power_kw", "average_power_kw", "peak_power_kw"):
        assert field not in RunContent.__dataclass_fields__, field
