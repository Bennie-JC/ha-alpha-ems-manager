"""beta.34: energy-balance failures are attributed, and the tolerance is untouched.

The reference installation's residuals are **two populations**, and neither is a
fault.

*Command transition.* The worst sample of 2026-08-29: house 681 W, PV 910 W,
charge 118 W, **import 2046 W** -- a residual of +2157 W against an allowance of
180. The meter reports two kilowatts that nothing consumes. It sits inside the
11:00-11:45Z quarters where Stage B ramped the setpoint from 2.4 kW to 10.2 kW,
and it was marked ``coherent`` because the source skew was 7.7 s. Every reading
was fresh; they were not describing the same setpoint.

*Boundary bias.* ``mean_signed_residual_w = +18.6``, 49 positive failures against
39 negative, and the excess per failed sample runs about 41 W in the 500-2000 W
band and about **412 W** in the 2000-5000 W band. It scales with power, which is
the signature of a proportional term -- the DC/AC conversion boundary, already
named in diagnostics as ``mixed_dc_strings_and_ac_meter`` -- and not of a fixed
offset or a wrong entity. A wrong entity fails *every* sample; the pass rate is
88 %.

So beta.34 adds attribution and **changes no verdict**. Reported as one number
the two populations look like one problem with a slightly tight tolerance, which
is exactly the reading that leads to widening it. The three tolerance constants
are asserted unchanged below, and balance is still not a control gate.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    BALANCE_BASE_ALLOWANCE_W,
    BALANCE_CONVERSION_LOSS_FRACTION,
    BALANCE_MAX_SOURCE_SKEW_SECONDS,
    BALANCE_METERING_TOLERANCE,
    BALANCE_REGIME_COMMAND_TRANSITION,
    BALANCE_REGIME_STEADY_STATE,
    BALANCE_TRANSITION_SECONDS,
    DISPATCH_POWER_DEADBAND_KW,
)
from custom_components.alpha_ems_manager.energy_balance import (
    BalanceMonitor,
    evaluate_balance,
)

from .test_energy_balance import flows

# ===========================================================================
# 1. the tolerance is exactly what it was
# ===========================================================================


def test_the_three_tolerance_constants_are_unchanged() -> None:
    """**The line beta.34 was told not to cross, pinned as a number.**

    Attribution is not permission. If a later change to any of these is wanted it
    must be argued on its own, with its own evidence, and it must break this test
    on the way.
    """
    assert BALANCE_BASE_ALLOWANCE_W == 40.0
    assert BALANCE_CONVERSION_LOSS_FRACTION == 0.05
    assert BALANCE_METERING_TOLERANCE == 0.03


def test_attribution_changes_no_verdict() -> None:
    """The same flows, judged identically whichever regime they land in.

    ``within_tolerance`` is computed from the three terms above and from nothing
    else. The regime is derived *after* the verdict and never feeds back into it.
    """
    quiet = evaluate_balance(flows())
    transitioning = evaluate_balance(
        flows(),
        seconds_since_dispatch_write=1.0,
        setpoint_delta_kw_since_previous=8.0,
    )
    assert quiet is not None and transitioning is not None

    assert quiet.within_tolerance == transitioning.within_tolerance
    assert quiet.allowed_residual_w == transitioning.allowed_residual_w
    assert quiet.residual_w == transitioning.residual_w
    assert quiet.outcome == transitioning.outcome
    # Only the label differs.
    assert quiet.regime == BALANCE_REGIME_STEADY_STATE
    assert transitioning.regime == BALANCE_REGIME_COMMAND_TRANSITION


# ===========================================================================
# 2. the threshold is derived, not chosen
# ===========================================================================


def test_the_transition_window_is_the_skew_allowance() -> None:
    """The two questions are the same question, so they get the same number.

    The skew bound says how far apart two source readings may be taken and still
    describe one instant. For exactly that long after the setpoint changes, a
    sample may pair a reading from before the change with one from after it.
    """
    assert BALANCE_TRANSITION_SECONDS == BALANCE_MAX_SOURCE_SKEW_SECONDS


@pytest.mark.parametrize(
    ("elapsed", "delta", "expected"),
    [
        # Freshly written: still settling, whatever the step size.
        (0.0, 0.0, BALANCE_REGIME_COMMAND_TRANSITION),
        (5.0, None, BALANCE_REGIME_COMMAND_TRANSITION),
        (BALANCE_TRANSITION_SECONDS, 0.0, BALANCE_REGIME_COMMAND_TRANSITION),
        # Past the window and the setpoint barely moved: steady state.
        (BALANCE_TRANSITION_SECONDS + 1.0, 0.0, BALANCE_REGIME_STEADY_STATE),
        (600.0, DISPATCH_POWER_DEADBAND_KW, BALANCE_REGIME_STEADY_STATE),
        # Past the window but the last step was large: the second clause catches
        # the sample immediately after a kilowatt-scale ramp.
        (600.0, 7.8, BALANCE_REGIME_COMMAND_TRANSITION),
        (600.0, -7.8, BALANCE_REGIME_COMMAND_TRANSITION),
        # Nothing has ever been written: nothing can be in flight.
        (None, None, BALANCE_REGIME_STEADY_STATE),
    ],
)
def test_the_regime_rule(
    elapsed: float | None, delta: float | None, expected: str
) -> None:
    """Both clauses, at their boundaries, on the production sample."""
    sample = evaluate_balance(
        flows(),
        seconds_since_dispatch_write=elapsed,
        setpoint_delta_kw_since_previous=delta,
    )
    assert sample is not None
    assert sample.regime == expected


def test_the_live_worst_sample_classifies_as_a_transition() -> None:
    """**The measured shape, at the measured skew.**

    House 681 W, PV 910 W, charge 118 W, import 2046 W -- an unexplained two
    kilowatts, taken while the setpoint was climbing from 2.4 kW to 10.2 kW. It
    still fails, as it should: the residual really is outside the allowance. What
    changes is that a reader can now see it is not the same *kind* of failure as
    a 41 W boundary excess at 1 kW.
    """
    sample = evaluate_balance(
        flows(
            house_load_w=681.0,
            pv_w=910.0,
            battery_charge_w=118.0,
            battery_discharge_w=0.0,
            grid_import_w=2046.0,
            grid_export_w=0.0,
        ),
        seconds_since_dispatch_write=45.0,
        setpoint_delta_kw_since_previous=7.8,
    )
    assert sample is not None
    assert sample.within_tolerance is False
    assert sample.regime == BALANCE_REGIME_COMMAND_TRANSITION
    assert sample.residual_w == pytest.approx(2157.0, abs=5.0)


# ===========================================================================
# 3. the tallies
# ===========================================================================


def test_the_steady_state_rate_is_published_beside_the_overall_one() -> None:
    """Beside, never instead of.

    The overall rate stays exactly what it was so the recorded series remains
    comparable across releases. The steady-state rate answers the different
    question of how well the identity closes when nothing is in flight, which is
    the number that says something about the installation's boundary.

    *Mutation: drop ``regime`` from ``BalanceSample`` and this fails.*
    """
    monitor = BalanceMonitor()
    assert monitor.pass_rate is None
    assert monitor.pass_rate_steady_state is None

    # Two quiet passes -- the balanced snapshot -- then one quiet failure and one
    # transitional failure, both a two-kilowatt residual.
    for elapsed, delta, house in (
        (None, None, 2000.0),
        (None, None, 2000.0),
        (None, None, 4000.0),
        (1.0, 8.0, 4000.0),
    ):
        sample = evaluate_balance(
            flows(house_load_w=house),
            seconds_since_dispatch_write=elapsed,
            setpoint_delta_kw_since_previous=delta,
        )
        assert sample is not None
        assert sample.within_tolerance is (house == 2000.0), house
        monitor.record(sample)

    payload = monitor.as_dict()
    assert payload["failed_samples_by_regime"]
    assert payload["passed_samples_by_regime"]
    assert payload["pass_rate_steady_state"] is not None
    # The overall rate counts every eligible sample, transitional ones included.
    assert monitor.eligible_samples == 4
    steady = sum(payload["passed_samples_by_regime"].values()) + sum(
        payload["failed_samples_by_regime"].values()
    )
    assert steady == monitor.eligible_samples
    # And the two rates genuinely differ on this data, which is the whole point.
    assert monitor.pass_rate != monitor.pass_rate_steady_state
    assert "regime_basis" in payload


def test_balance_is_still_not_a_control_gate() -> None:
    """Nothing in the control path may consult a balance verdict.

    beta.34 makes the failures easier to read, which is exactly the change most
    likely to tempt somebody into acting on them. It remains an observation.
    """
    import pathlib

    from custom_components.alpha_ems_manager import coordinator as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    authorize = source[source.index("def _build_control_report") :]
    authorize = authorize[: authorize.index("\n    @callback")]
    for forbidden in ("balance.pass_rate", "within_tolerance", "regime"):
        assert forbidden not in authorize, forbidden
