"""The energy-balance residual is evidence, not permission.

An earlier design gated control on ``gross_fault_suspected``, reasoning that a
residual far past its allowance meant a source could not be trusted. Live data
disproved it, and this file is the record of why.

On this installation the house-load figure is derived from the *inverter's own*
grid register, while the balance check reads a separate meter. Substitute the one
into the other and every term cancels:

    residual  ==  meter_grid - inverter_grid   (+ a filter lag on the PV term)

The battery power cancels identically. The state of charge never appears at all.
So the residual is a comparison of two grid meters -- and however far past its
allowance it goes, it says nothing about either reading a battery command
actually depends on.

Two live samples make the point concretely, and both are reproduced here from
their real flow values rather than from their headline figures:

* **+1394 W**, 2026-08-20, mode ``grid->house+battery``. Labelled a gross fault.
  Reconstructs exactly as ``2139 - 745``: a load drawing through the meter that
  the inverter's own register does not see.
* **-10149 W**, 2026-08-19, during a 10.18 kW charge ramp. Also labelled gross.
  The same two-meter difference, this time from latency between two sources that
  both timestamp promptly -- which the coherence gate structurally cannot catch.

Neither is a broken sensor. Both would have blocked control.

**No tolerance is widened here, and no threshold is tuned.** The allowance
formula is asserted to still produce exactly the figures it always did, and both
samples are asserted to still be labelled gross faults. What changed is only
that control no longer consults them.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    BALANCE_BASE_ALLOWANCE_W,
    BALANCE_CONVERSION_LOSS_FRACTION,
    BALANCE_GROSS_FAULT_FLOOR_W,
    BALANCE_GROSS_FAULT_MULTIPLE,
    BALANCE_METERING_TOLERANCE,
    BALANCE_SUSTAINED_FAILURES,
)
from custom_components.alpha_ems_manager.energy_balance import (
    BalanceMonitor,
    evaluate_balance,
    power_band,
)
from custom_components.alpha_ems_manager.normalization import PowerFlows
from custom_components.alpha_ems_manager.safety import ControlContext, evaluate

from .test_control_pipeline import make_context, make_intent


def snapshot(
    *,
    house: float,
    pv: float,
    charge: float = 0.0,
    discharge: float = 0.0,
    grid_import: float = 0.0,
    grid_export: float = 0.0,
):
    """Evaluate a snapshot given already-normalised flows.

    Built from the canonical fields rather than from raw signed readings, because
    these are transcribed from a diagnostics download where they had already been
    normalised.
    """
    result = evaluate_balance(
        PowerFlows(
            house_load_w=house,
            pv_w=pv,
            battery_charge_w=charge,
            battery_discharge_w=discharge,
            grid_import_w=grid_import,
            grid_export_w=grid_export,
        )
    )
    assert result is not None
    return result


#: The live gross-labelled sample, 2026-08-20, from its real flow values.
LIVE_GROSS = {
    "house": 578.0,
    "pv": 3.0,
    "charge": 170.0,
    "grid_import": 2139.0,
}

#: The live opposite-sign sample, 2026-08-19, during a fast charge ramp.
LIVE_GROSS_NEGATIVE = {
    "house": 1023.0,
    "pv": 564.0,
    "charge": 10180.0,
    "grid_import": 490.0,
}

#: The small boundary residual the project has documented from the start.
LIVE_BOUNDARY = {
    "house": 586.0,
    "pv": 200.0,
    "discharge": 540.0,
}


# ===========================================================================
# the samples still read exactly as they did
# ===========================================================================


def test_the_live_gross_sample_reconstructs_from_its_real_flows() -> None:
    """Every reported figure, from the flows rather than from the headline.

    Fabricating a snapshot that merely produced a 1394 W residual would have
    proved nothing about this installation. These are the values it actually
    reported.
    """
    result = snapshot(**LIVE_GROSS)

    assert result.supply_w == pytest.approx(2142.0)
    assert result.demand_w == pytest.approx(748.0)
    assert result.residual_w == pytest.approx(1394.0)
    assert result.dc_power_w == pytest.approx(173.0)
    assert result.ac_power_w == pytest.approx(2142.0)
    assert result.allowed_residual_w == pytest.approx(112.9, abs=0.05)
    assert abs(result.residual_w) / result.allowed_residual_w == pytest.approx(
        12.35, abs=0.02
    )
    assert result.gross_fault_suspected is True
    assert result.mode == "grid->house+battery"


def test_the_gross_sample_is_the_difference_between_two_grid_meters() -> None:
    """The whole argument, in arithmetic.

    House load is ``pv + battery_power + inverter_grid``, so substituting it into
    the identity leaves ``meter_grid - inverter_grid`` and nothing else. Here the
    inverter's own register must have read 745 W against the meter's 2139 W.
    """
    implied_inverter_grid = (
        LIVE_GROSS["house"] - LIVE_GROSS["pv"] + LIVE_GROSS["charge"]
    )

    assert implied_inverter_grid == pytest.approx(745.0)
    assert LIVE_GROSS["grid_import"] - implied_inverter_grid == pytest.approx(1394.0)


def test_the_negative_sample_reconstructs_the_same_way() -> None:
    """Opposite sign, same mechanism, ten kilowatts of it."""
    result = snapshot(**LIVE_GROSS_NEGATIVE)

    assert result.residual_w == pytest.approx(-10149.0)
    assert result.allowed_residual_w == pytest.approx(913.3, abs=0.05)
    assert result.gross_fault_suspected is True

    implied_inverter_grid = (
        LIVE_GROSS_NEGATIVE["house"]
        - LIVE_GROSS_NEGATIVE["pv"]
        + LIVE_GROSS_NEGATIVE["charge"]
    )
    assert LIVE_GROSS_NEGATIVE["grid_import"] - implied_inverter_grid == (
        pytest.approx(-10149.0)
    )


def test_the_small_boundary_residual_still_reads_as_moderate() -> None:
    """The known limitation, unchanged: a fault, but not a gross one."""
    result = snapshot(**LIVE_BOUNDARY)

    assert result.residual_w == pytest.approx(154.0)
    assert result.allowed_residual_w == pytest.approx(99.2, abs=0.05)
    assert result.gross_fault_suspected is False


def test_no_tolerance_was_widened() -> None:
    """The allowance is still the same three terms with the same coefficients.

    The point of this test is what it forbids. Making the two live samples stop
    inhibiting control by loosening this formula would have hidden a real
    measurement difference instead of understanding it.
    """
    result = snapshot(**LIVE_GROSS)

    assert result.allowed_residual_w == pytest.approx(
        BALANCE_BASE_ALLOWANCE_W
        + BALANCE_CONVERSION_LOSS_FRACTION * result.dc_power_w
        + BALANCE_METERING_TOLERANCE * result.ac_power_w
    )
    assert BALANCE_BASE_ALLOWANCE_W == 40.0
    assert BALANCE_CONVERSION_LOSS_FRACTION == 0.05
    assert BALANCE_METERING_TOLERANCE == 0.03
    assert BALANCE_GROSS_FAULT_MULTIPLE == 3.0
    assert BALANCE_GROSS_FAULT_FLOOR_W == 500.0
    assert BALANCE_SUSTAINED_FAILURES == 3


def test_the_warnings_are_not_hidden() -> None:
    """Both samples still fail, still warn, and still say so.

    Control no longer consults them; the user still hears about them.
    """
    for values in (LIVE_GROSS, LIVE_GROSS_NEGATIVE):
        monitor = BalanceMonitor()
        for _ in range(BALANCE_SUSTAINED_FAILURES):
            monitor.record(snapshot(**values))

        assert monitor.failed_samples == BALANCE_SUSTAINED_FAILURES
        assert monitor.sustained_failure is True
        assert monitor.should_warn() is True


# ===========================================================================
# and none of it reaches the gate
# ===========================================================================


def test_the_gate_has_no_balance_input_at_all() -> None:
    """Structural: there is no field for a residual to arrive through.

    The tokens are chosen to be unambiguous. A bare "excess" would have matched
    ``excess_export_active``, which is a feature of the control surface and has
    nothing to do with the balance identity -- and a check that fires on an
    unrelated field gets loosened rather than fixed.
    """
    fields = set(ControlContext.__dataclass_fields__)
    forbidden = ("residual", "balance", "gross_fault", "tolerance", "excess_w")

    matched = {field for field in fields for token in forbidden if token in field}

    assert matched == set()


def test_a_gross_balance_fault_does_not_inhibit_control() -> None:
    """The behavioural half, and the reason for the whole redesign.

    A residual twelve times its allowance is not evidence about the state of
    charge -- the state of charge is not a term in the identity. Gating on it
    would have blocked control during exactly the conditions that produced these
    samples: an unmetered load, and a fast charge ramp.
    """
    result = snapshot(**LIVE_GROSS)
    assert result.gross_fault_suspected is True

    verdict = evaluate(make_intent(energy_ac_kwh=0.5), make_context())

    assert verdict.safe is True
    assert verdict.inhibit_reason is None


def test_a_sustained_balance_failure_does_not_inhibit_control() -> None:
    """Nor does a run of them, which the known residual produces overnight.

    Gating on sustained failure would have made control inert exactly when a
    discharge decision matters most.
    """
    monitor = BalanceMonitor()
    for _ in range(BALANCE_SUSTAINED_FAILURES + 5):
        monitor.record(snapshot(**LIVE_BOUNDARY))
    assert monitor.sustained_failure is True

    verdict = evaluate(make_intent(energy_ac_kwh=0.5), make_context())

    assert verdict.safe is True


# ===========================================================================
# what was added instead: evidence, bounded and diagnostics-only
# ===========================================================================


def test_the_sign_of_a_failure_is_now_counted() -> None:
    """The feature the architecture notes called informative, and which every
    existing statistic discarded by taking an absolute value first."""
    monitor = BalanceMonitor()
    monitor.record(snapshot(**LIVE_GROSS))
    monitor.record(snapshot(**LIVE_GROSS_NEGATIVE))

    assert monitor.positive_failures == 1
    assert monitor.negative_failures == 1
    assert monitor.mean_signed_residual_w is not None
    assert monitor.mean_signed_residual_w < 0.0


def test_a_systematic_offset_shows_as_a_non_zero_mean() -> None:
    """Symmetric noise averages away; a measurement boundary does not."""
    monitor = BalanceMonitor()
    for _ in range(10):
        monitor.record(snapshot(**LIVE_BOUNDARY))

    assert monitor.mean_signed_residual_w == pytest.approx(154.0, abs=1.0)
    assert monitor.positive_failures == 10
    assert monitor.negative_failures == 0


def test_failures_are_attributed_to_a_power_band() -> None:
    """The feature that would separate a fixed offset from a scaling fault.

    A constant difference between two meters shrinks against the allowance as
    power rises; a mis-selected or mis-signed source grows with it.
    """
    monitor = BalanceMonitor()
    monitor.record(snapshot(**LIVE_GROSS))
    monitor.record(snapshot(**LIVE_GROSS_NEGATIVE))

    assert monitor.failed_by_power_band == {"2000-5000W": 1, "5000W+": 1}
    assert set(monitor.excess_sum_by_power_band) == {"2000-5000W", "5000W+"}


def test_the_power_band_key_space_cannot_grow_with_runtime() -> None:
    """Bounded, like the mode labels, so the tally stays small forever."""
    bands = {power_band(float(watts)) for watts in range(0, 60_000, 7)}

    assert len(bands) == 4


def test_an_alternating_fault_is_visible_even_though_it_never_warns() -> None:
    """The known blind spot in the consecutive counter, now measurable.

    A fault that alternates with a pass never reaches three in a row, so it
    never warns -- while halving the pass rate.
    """
    monitor = BalanceMonitor()
    for _ in range(6):
        monitor.record(snapshot(**LIVE_BOUNDARY))
        monitor.record(snapshot(house=600.0, pv=0.0, grid_import=600.0))

    assert monitor.sustained_failure is False
    assert monitor.should_warn() is False
    assert monitor.windowed_failures == 6


def test_the_instrumentation_is_bounded() -> None:
    """Nothing added here can outgrow the diagnostics list cap."""
    monitor = BalanceMonitor()
    for index in range(200):
        monitor.record(
            snapshot(house=float(index * 37), pv=0.0, grid_import=float(index * 41))
        )
    payload = monitor.as_dict()

    assert len(payload["failed_samples_by_power_band"]) <= 4
    assert len(payload["excess_sum_by_power_band"]) <= 4
    assert len(monitor.recent_outcomes) <= 20
    for value in payload.values():
        if isinstance(value, (list, tuple)):
            assert len(value) <= 16


def test_the_payload_says_why_it_is_not_a_gate() -> None:
    """A reader of diagnostics should not have to infer this."""
    payload = BalanceMonitor().as_dict()

    assert "residual_shape_basis" in payload
    assert "measurement" in payload["residual_shape_basis"]
