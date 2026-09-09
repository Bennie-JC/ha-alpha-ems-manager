"""A compelled objective may buy the production its forecast promised.

**The third correction in one family, and the arithmetic names it exactly.**

A charge row's objective is fed from two places: production, and purchase bounded by
the row's frozen ``grid_authorised_kwh``. That authorisation is
``marginal_grid_import_kwh`` -- the import the charge was *predicted* to cause,
computed inside the solve from forecast production. Since the plan sized the
objective as ``forecast_surplus + forecast_grid_share``, the cap the two bounds
jointly impose is ``measured_surplus + forecast_grid_share``, so

    cap - objective_rate  =  measured_surplus - forecast_surplus

and the battery is throttled by **exactly the production forecast error**.

Reconstructed against the live 2026-09-09 campaign: a 13.63 kWh objective over
thirteen rows is 4.194 kW, and a 2.2 kW production expectation that did not arrive
commands 1.90 kW. The campaign delivered 6.334 kWh at a 1.95 kW mean, with the pack
at 74 % and 5.5 kWh of headroom unused. Predicted 6.175 kWh against 6.334 observed
-- 2.5 %, the same agreement the beta.40 proof rested on.

beta.36 stopped the row's authorisation capping battery power directly; beta.40
stopped the run's budget doing it as a flat pace. This is the same coupling surviving
as *forecast* production, and beta.54 removes it **only where the objective was never
a choice**: energy physical reachability compelled. Discretionary purchase keeps the
forecast bound, because buying past what the optimiser priced is a decision and the
fifteen-minute replan is what makes it.

The two rival explanations are excluded by their own signatures rather than by
argument -- a collapsed headroom ceiling commands 0.00 kW and the deadband commands
4.00 kW, so neither can produce 6.334 kWh over the window.
"""

from __future__ import annotations

import inspect

import pytest

from custom_components.alpha_ems_manager.dispatch import (
    ChargeLimits,
    QuarterProgress,
    decide_charge,
)

#: The production row, reconstructed. 13.63 kWh over thirteen quarters.
OBJECTIVE_KWH = 13.63 / 13
HOURS = 0.25
#: What the plan authorised from the grid, having expected 2.2 kW of production.
FORECAST_SURPLUS_KW = 2.2
GRID_AUTHORISED_KWH = OBJECTIVE_KWH - FORECAST_SURPLUS_KW * HOURS


def decide(
    *,
    compelled_kwh: float = 0.0,
    actual_surplus_kw: float = 0.0,
    objective_kwh: float = OBJECTIVE_KWH,
    grid_authorised_kwh: float = GRID_AUTHORISED_KWH,
    inverter_kw: float | None = 10.0,
    headroom_kw: float | None = 22.0,
    remaining_grid_kw: float | None = None,
    last_applied_kw: float | None = None,
    seconds_remaining: float = 900.0,
):
    """Run one charge decision over the reconstructed row."""
    return decide_charge(
        progress=QuarterProgress(
            seconds_remaining=seconds_remaining,
            battery_remaining_kwh=objective_kwh,
            grid_remaining_kwh=grid_authorised_kwh,
            retention_authorised=False,
            compelled_remaining_kwh=compelled_kwh,
        ),
        house_load_kw=0.5,
        pv_kw=0.5 + actual_surplus_kw,
        limits=ChargeLimits(
            inverter_kw=inverter_kw,
            headroom_kw=headroom_kw,
            remaining_grid_kw=remaining_grid_kw,
        ),
        last_applied_kw=last_applied_kw,
    )


# ===========================================================================
# the defect, and the fix
# ===========================================================================


def test_the_throttle_is_exactly_the_production_forecast_error() -> None:
    """**The proof, as arithmetic rather than as a story.**

    Across a range of forecasts, the commanded rate with no production is the
    forecast grid share alone -- so the shortfall against the objective rate equals
    the production the forecast promised and the sky did not deliver.
    """
    for forecast_kw in (1.0, 2.0, 2.2, 3.0):
        authorised = max(0.0, OBJECTIVE_KWH - forecast_kw * HOURS)
        decision = decide(actual_surplus_kw=0.0, grid_authorised_kwh=authorised)

        commanded = abs(decision.applied_kw)
        # Quantised to the 0.1 kW actuator step, hence the tolerance.
        assert commanded == pytest.approx(authorised / HOURS, abs=0.11)
        assert decision.limited_by == "remaining_grid_energy"


def test_a_compelled_row_delivers_its_objective_when_production_fails() -> None:
    """**The fix, on the row that prompted it.**

    Same forecast, same absent production, and the compulsory objective is bought
    instead of abandoned -- 4.10 kW against a 4.194 kW objective, the difference
    being the actuator step and nothing else.
    """
    decision = decide(compelled_kwh=OBJECTIVE_KWH, actual_surplus_kw=0.0)

    assert abs(decision.applied_kw) == pytest.approx(4.10, abs=0.01)
    assert decision.limited_by == "compelled_objective"


def test_an_economic_row_is_unchanged_when_production_fails() -> None:
    """**The non-regression that matters most.**

    A discretionary purchase keeps its forecast bound exactly. Buying past what the
    optimiser priced is a decision, and the rolling replan is what makes it.
    """
    decision = decide(compelled_kwh=0.0, actual_surplus_kw=0.0)

    assert abs(decision.applied_kw) == pytest.approx(1.90, abs=0.01)
    assert decision.limited_by == "remaining_grid_energy"


def test_exact_delivery_commands_what_it_always_commanded() -> None:
    """With production arriving as forecast, nothing moves -- for either kind of row.

    And the compulsory authority is not even *named* there: it was available, it was
    inert, and labelling it would claim a purchase that never happened.
    """
    economic = decide(compelled_kwh=0.0, actual_surplus_kw=FORECAST_SURPLUS_KW)
    compelled = decide(
        compelled_kwh=OBJECTIVE_KWH, actual_surplus_kw=FORECAST_SURPLUS_KW
    )

    assert economic.applied_kw == compelled.applied_kw
    assert economic.limited_by == compelled.limited_by == "quantisation"


def test_a_mixed_buy_gets_its_compelled_share_and_no_more() -> None:
    """Reachability compelled part of the run; the optimiser chose the rest on price.

    Promoting the discretionary half would be exactly the error ``purchase_purpose``
    refuses to make, so the raise stops at the compulsory share.
    """
    half = OBJECTIVE_KWH * 0.5
    decision = decide(compelled_kwh=half, actual_surplus_kw=0.0)

    commanded = abs(decision.applied_kw)
    assert commanded < abs(decide(compelled_kwh=OBJECTIVE_KWH).applied_kw)
    assert commanded <= OBJECTIVE_KWH / HOURS


# ===========================================================================
# the rival explanations, excluded by signature
# ===========================================================================


def test_a_collapsed_headroom_ceiling_commands_nothing_at_all() -> None:
    """**Which is why it cannot be what happened.**

    ``headroom_ceiling_kw`` returns ``0.0`` whenever the plan's landing energy is at
    or below the pack's current energy, and that zeroes the command outright. The
    campaign delivered 6.334 kWh, so this was not the binding constraint.
    """
    decision = decide(compelled_kwh=0.0, actual_surplus_kw=0.0, headroom_kw=0.0)

    assert decision.applied_kw == 0.0
    assert decision.limited_by == "headroom"


def test_the_deadband_would_have_nearly_met_the_objective() -> None:
    """**Also excluded.** It holds the previous setpoint, which was already near.

    A row climbing in 0.1 kW steps is refused and holds 4.00 kW -- 13 kWh over the
    window, essentially the whole promise. That is not a 46 % delivery either.
    """
    decision = decide(
        compelled_kwh=0.0,
        actual_surplus_kw=0.0,
        grid_authorised_kwh=OBJECTIVE_KWH,
        last_applied_kw=-4.05,
    )

    assert abs(decision.applied_kw) == pytest.approx(4.00, abs=0.01)
    assert decision.limited_by == "deadband"


# ===========================================================================
# bounds the raise may not cross
# ===========================================================================


def test_the_raise_never_exceeds_the_rows_own_objective() -> None:
    """A compulsory share is a *part* of the promise, never an addition to it."""
    decision = decide(compelled_kwh=OBJECTIVE_KWH * 4, actual_surplus_kw=0.0)

    assert abs(decision.applied_kw) <= OBJECTIVE_KWH / HOURS + 1e-9


def test_a_delivered_compulsory_share_stops_asking() -> None:
    """Nothing remains owed, so the term is inert and the forecast bound returns."""
    decision = decide(compelled_kwh=0.0, actual_surplus_kw=0.0)
    assert decision.limited_by == "remaining_grid_energy"


def test_the_inverter_limit_still_binds_first() -> None:
    """Every clamp still applies, in the same order, to a compelled row."""
    decision = decide(
        compelled_kwh=OBJECTIVE_KWH, actual_surplus_kw=0.0, inverter_kw=2.0
    )

    assert abs(decision.applied_kw) == pytest.approx(2.0, abs=0.01)
    assert decision.limited_by == "inverter_power"


def test_the_pack_headroom_still_binds_on_a_compelled_row() -> None:
    """A compulsory objective is not permission to overfill the pack."""
    decision = decide(
        compelled_kwh=OBJECTIVE_KWH, actual_surplus_kw=0.0, headroom_kw=1.0
    )

    assert abs(decision.applied_kw) == pytest.approx(1.0, abs=0.01)
    assert decision.limited_by == "headroom"


def test_the_actuator_step_still_quantises_a_compelled_command() -> None:
    """0.1 kW toward zero, so a compelled row can never overshoot by rounding."""
    decision = decide(compelled_kwh=OBJECTIVE_KWH, actual_surplus_kw=0.0)

    steps = abs(decision.applied_kw) / 0.1
    assert steps == pytest.approx(round(steps), abs=1e-6)
    assert abs(decision.applied_kw) <= OBJECTIVE_KWH / HOURS


def test_a_row_with_no_time_left_asks_for_nothing_absurd() -> None:
    """The rate denominator is floored at one tick horizon, not at zero."""
    decision = decide(
        compelled_kwh=OBJECTIVE_KWH, actual_surplus_kw=0.0, seconds_remaining=0.0
    )

    assert abs(decision.applied_kw) <= 10.0


# ===========================================================================
# structural
# ===========================================================================


def test_the_raise_reads_only_the_rows_own_frozen_share() -> None:
    """**Not catch-up, and the source says so rather than the comment alone.**

    No campaign-cumulative state is reachable from the decision. The bound is this
    row's own compulsory remainder, so an earlier row's shortfall cannot appear in a
    later row's command.
    """
    source = inspect.getsource(decide_charge)

    # Code identifiers, not prose: the docstring legitimately discusses campaigns.
    for forbidden in (
        "_campaign_realized",
        "_campaign_measured",
        "_campaign_frozen_target",
        "campaign_realized_now",
        "self.",
    ):
        assert forbidden not in source, forbidden
    assert "progress.compelled_rate_kw" in source


def test_the_compelled_rate_is_bounded_by_the_remaining_objective() -> None:
    """Stated on the property itself, so the bound cannot be edited away silently."""
    progress = QuarterProgress(
        seconds_remaining=900.0,
        battery_remaining_kwh=0.2,
        grid_remaining_kwh=0.0,
        retention_authorised=False,
        compelled_remaining_kwh=99.0,
    )

    assert progress.compelled_rate_kw == pytest.approx(0.2 / 0.25)


def test_a_discretionary_row_defaults_to_no_compulsory_authority() -> None:
    """The default is what makes this term inert everywhere it does not apply."""
    progress = QuarterProgress(
        seconds_remaining=900.0,
        battery_remaining_kwh=1.0,
        grid_remaining_kwh=0.5,
        retention_authorised=False,
    )

    assert progress.compelled_remaining_kwh == 0.0
    assert progress.compelled_rate_kw == 0.0


def test_the_new_token_is_not_a_clamp() -> None:
    """It may raise a command, so it has no place in the clamp order."""
    from custom_components.alpha_ems_manager.const import (
        DISPATCH_CLAMP_ORDER,
        DISPATCH_LIMIT_COMPELLED_OBJECTIVE,
    )

    assert DISPATCH_LIMIT_COMPELLED_OBJECTIVE not in DISPATCH_CLAMP_ORDER


def test_an_export_row_gains_no_compulsory_authority() -> None:
    """There is no such thing as a compelled sale, and the export path never asks."""
    from custom_components.alpha_ems_manager.dispatch import decide_export

    source = inspect.getsource(decide_export)

    assert "compelled" not in source


# ===========================================================================
# apportionment
# ===========================================================================


def test_the_runs_compelled_energy_is_shared_across_its_rows_not_repeated() -> None:
    """**Otherwise one run's rows could together claim it several times over.**

    The share is each row's objective as a fraction of the run's, so the rows sum to
    the run's compulsory total exactly.
    """
    from datetime import UTC, datetime, timedelta

    from custom_components.alpha_ems_manager.execution import AdmittedPlan, QuarterRow

    start = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
    rows = tuple(
        QuarterRow(
            start=start + timedelta(minutes=15 * index),
            end=start + timedelta(minutes=15 * (index + 1)),
            battery_kwh=1.0,
            grid_authorised_kwh=0.5,
            grid_export_target_kwh=0.0,
            grid_export_caused_kwh=0.0,
            desired_grid_kw=2.0,
            not_executable=None,
        )
        for index in range(4)
    )

    class _Target:
        battery_target_kwh = 4.0

    plan = AdmittedPlan(
        plan_id="p",
        revision=1,
        run_id="r",
        intent="grid_charge",
        purpose="safety_buy",
        admitted_at=start,
        rows=rows,
        compelled_kwh=2.0,
        target=_Target(),
    )

    shares = [plan._compelled_share(row) for row in rows]

    assert sum(shares) == pytest.approx(2.0)
    assert all(share == pytest.approx(0.5) for share in shares)


def test_an_absent_attribution_grants_no_compulsory_authority() -> None:
    """Absent is not zero, and both must read as "no authority" here.

    A pre-beta.54 record and a refresh with no reachability both arrive as ``None``,
    and treating either as fully compelled would buy on the strength of a figure
    nobody produced.
    """
    from datetime import UTC, datetime, timedelta

    from custom_components.alpha_ems_manager.execution import AdmittedPlan, QuarterRow

    start = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
    row = QuarterRow(
        start=start,
        end=start + timedelta(minutes=15),
        battery_kwh=1.0,
        grid_authorised_kwh=0.5,
        grid_export_target_kwh=0.0,
        grid_export_caused_kwh=0.0,
        desired_grid_kw=2.0,
        not_executable=None,
    )

    class _Target:
        battery_target_kwh = 1.0

    plan = AdmittedPlan(
        plan_id="p",
        revision=1,
        run_id="r",
        intent="grid_charge",
        purpose="charge",
        admitted_at=start,
        rows=(row,),
        compelled_kwh=None,
        target=_Target(),
    )

    assert plan._compelled_share(row) == 0.0
