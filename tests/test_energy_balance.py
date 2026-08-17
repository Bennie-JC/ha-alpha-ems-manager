"""The optional energy-balance sanity check.

The identity is a data-quality signal, not a metering settlement. These tests
pin down that it is generous about real-world noise, refuses to judge a partial
snapshot, and catches the failure it exists for: an inverted sign convention.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    BALANCE_SAMPLE_WINDOW,
    SIGN_BATTERY_POSITIVE_IS_CHARGE,
)
from custom_components.alpha_ems_manager.energy_balance import evaluate_balance
from custom_components.alpha_ems_manager.normalization import (
    PowerFlows,
    split_battery_power,
)
from custom_components.alpha_ems_manager.storage import BalanceStats


def flows(**overrides: float | None) -> PowerFlows:
    """Return a balanced snapshot, with overrides applied."""
    base = {
        "house_load_w": 2000.0,
        "pv_w": 5000.0,
        "battery_charge_w": 3000.0,
        "battery_discharge_w": 0.0,
        "grid_import_w": 0.0,
        "grid_export_w": 0.0,
    }
    base.update(overrides)
    return PowerFlows(**base)  # type: ignore[arg-type]


def test_a_balanced_system_passes() -> None:
    """PV 5 kW = house 2 kW + battery charge 3 kW."""
    sample = evaluate_balance(flows())

    assert sample is not None
    assert sample.residual_w == pytest.approx(0.0)
    assert sample.within_tolerance


def test_small_conversion_losses_are_tolerated() -> None:
    """A few percent of inverter loss is normal and must not be flagged."""
    sample = evaluate_balance(flows(pv_w=5150.0))

    assert sample is not None
    assert sample.relative_error < 0.05
    assert sample.within_tolerance


def test_a_large_mismatch_is_flagged() -> None:
    """A residual far outside the tolerance is reported."""
    sample = evaluate_balance(flows(pv_w=200.0))

    assert sample is not None
    assert not sample.within_tolerance


def test_an_inverted_battery_sign_is_caught() -> None:
    """The check exists mainly to surface a mis-set sign convention.

    Reading -3000 W (charging) under the wrong convention turns 3 kW of demand
    into 3 kW of supply, a 6 kW error that the identity cannot miss.
    """
    charge, discharge = split_battery_power(-3000.0, SIGN_BATTERY_POSITIVE_IS_CHARGE)
    sample = evaluate_balance(
        flows(battery_charge_w=charge, battery_discharge_w=discharge)
    )

    assert sample is not None
    assert not sample.within_tolerance


def test_near_zero_flows_do_not_produce_meaningless_percentages() -> None:
    """A 20 W residual at night is not a 100 % error.

    The absolute floor in the denominator keeps the quiet hours from dominating
    the quality score.
    """
    sample = evaluate_balance(
        flows(
            house_load_w=20.0,
            pv_w=0.0,
            battery_charge_w=0.0,
            battery_discharge_w=0.0,
            grid_import_w=0.0,
        )
    )

    assert sample is not None
    assert sample.relative_error <= 0.2
    assert sample.within_tolerance


@pytest.mark.parametrize(
    "missing",
    [
        "house_load_w",
        "pv_w",
        "battery_charge_w",
        "battery_discharge_w",
        "grid_import_w",
        "grid_export_w",
    ],
)
def test_a_partial_snapshot_is_not_judged(missing: str) -> None:
    """Any missing component means no verdict, rather than a false alarm."""
    assert evaluate_balance(flows(**{missing: None})) is None


def test_the_sample_serialises_for_diagnostics() -> None:
    """Diagnostics reports the residual in plain values."""
    sample = evaluate_balance(flows())
    assert sample is not None

    payload = sample.as_dict()
    assert set(payload) == {
        "supply_w",
        "demand_w",
        "residual_w",
        "allowed_residual_w",
        "relative_error",
        "within_tolerance",
        "outcome",
        "mode",
        "tolerance_reason",
        "dc_power_w",
        "ac_power_w",
        "gross_fault_suspected",
        "flows_w",
    }
    # With no timing information attached the sample is judged on its numbers
    # alone, so it reports a pass or a failure rather than being skipped.
    assert payload["outcome"] == "passed"


# -- rolling statistics ------------------------------------------------------


def test_the_pass_rate_starts_undefined() -> None:
    """With nothing sampled there is no score to report."""
    assert BalanceStats().score is None


def test_the_pass_rate_reflects_recent_samples() -> None:
    """Three good samples out of four give a 0.75 pass rate."""
    stats = BalanceStats()
    for outcome in (True, True, True, False):
        stats.record(outcome)

    assert stats.score == pytest.approx(0.75)


def test_the_window_decays_so_old_problems_stop_counting() -> None:
    """A fixed wiring fault should not weigh the score down forever."""
    stats = BalanceStats()
    for _ in range(BALANCE_SAMPLE_WINDOW):
        stats.record(False)
    assert stats.score == pytest.approx(0.0)

    for _ in range(BALANCE_SAMPLE_WINDOW * 2):
        stats.record(True)

    assert stats.score > 0.9
    assert stats.total_samples <= BALANCE_SAMPLE_WINDOW + 1


def test_the_tally_survives_a_round_trip() -> None:
    """Balance statistics persist with the rest of the learning history."""
    stats = BalanceStats(ok_samples=7, total_samples=10)
    restored = BalanceStats.from_dict(stats.to_dict())

    assert restored.ok_samples == 7
    assert restored.total_samples == 10
    assert restored.score == pytest.approx(0.7)


@pytest.mark.parametrize(
    "payload", [None, "nonsense", [], {"ok": "x"}, {"total": None}, {}]
)
def test_a_damaged_tally_degrades_to_empty(payload) -> None:
    """Corrupt statistics never raise on load."""
    stats = BalanceStats.from_dict(payload)

    assert stats.total_samples >= 0
    assert stats.ok_samples >= 0
