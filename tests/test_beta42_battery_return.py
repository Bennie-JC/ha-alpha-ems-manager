"""What the battery has recovered of what it cost, and what that figure rests on.

**The numerator is the correction, not an addition.** ``realized_net_value_eur``
equals ``TRUE - sum(p*min(I,N)) + sum(s*X)`` -- the household's whole position. It
subtracts an import bill no battery could have avoided and credits PV export that
needed no battery, so ``sum(p*min(I,N))`` dominates and the figure is structurally
negative for any household that imports anything. Shown to an operator as "battery
savings" it would have reported that the battery destroys value. Its own docstring is
accurate; the *name* was the problem. So the return is built on
``realized_battery_benefit_eur`` -- no-battery net cash less actual net cash -- and
these tests pin that nothing else can reach it.

**And the basis is published rather than smoothed over.** The import leg is genuinely
all-in cash: wholesale, market tax, sourcing markup and energy tax, VAT inclusive. The
export leg is, on a stock configuration, a wholesale reconstruction -- the source
publishes no feed-in price, the adjustment defaults to zero and the VAT flag to off.
The battery's benefit is mostly avoided import, so the error is small on this
installation and could be large on an export-heavy one. That is a reason to publish
the size of the reconstructed leg, not a reason to call it bounded.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from custom_components.alpha_ems_manager.const import (
    CALCULATION_BASIS_IMPORT_CASH_EXPORT_RECONSTRUCTED,
    CONF_BATTERY_INVESTMENT_DATE,
    CONF_BATTERY_INVESTMENT_EUR,
    CONF_BATTERY_SUBSIDY_EUR,
    CONF_OTHER_ONE_TIME_CREDIT_EUR,
    PRICE_LEG_ALL_IN_CASH,
    REALIZED_BENEFIT_BASIS_VERSION,
    ROI_MIN_SAMPLE_DAYS,
    ROI_PAYBACK_UNAVAILABLE_INSUFFICIENT_HISTORY,
    ROI_PAYBACK_UNAVAILABLE_NO_BENEFIT,
    ROI_UNAVAILABLE_NO_HISTORY,
    ROI_UNAVAILABLE_NO_INVESTMENT,
)

TODAY = date(2026, 9, 1)


def _sealed(coordinator: Any, days: int, benefit_eur: float, *, end: date = TODAY):
    """Seal ``days`` consecutive days ending the day before ``end``."""
    from custom_components.alpha_ems_manager.storage import DayRecord

    for offset in range(1, days + 1):
        day = end - timedelta(days=offset)
        record = coordinator.store.days.get(day) or DayRecord(
            day=day, tz_key="Europe/Amsterdam"
        )
        record.note_final_benefit(
            finalized_at=f"{day.isoformat()}T00:20:00+00:00",
            benefit_eur=benefit_eur,
            basis_version=REALIZED_BENEFIT_BASIS_VERSION,
        )
        coordinator.store.days[day] = record


@pytest.fixture
async def invested(hass, setup_integration, source_entities, frank):
    """Return a coordinator that has been told what the battery cost.

    The resolved configuration is substituted directly rather than written through
    the options flow, because updating the entry reloads the integration and hands
    back a *different* coordinator -- the tests below would then be exercising an
    object the fixture never set up.
    ``test_the_option_keys_reach_the_resolved_configuration`` covers the wiring the
    substitution skips, so neither half is assumed.
    """
    from dataclasses import replace

    from .test_beta33_campaign_wiring import planning_coordinator

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    coordinator.config = replace(
        coordinator.config,
        battery_investment_eur=11000.0,
        battery_subsidy_eur=1000.0,
        other_one_time_credit_eur=0.0,
        battery_investment_date="2026-05-01",
    )
    coordinator.store.days.clear()
    coordinator.store.sealed_benefit_eur = 0.0
    coordinator.store.sealed_through = None
    return coordinator


async def test_the_option_keys_reach_the_resolved_configuration(
    hass, setup_integration
) -> None:
    """**The wiring the other fixture substitutes past.**

    beta.21's lesson, in one test: the grid-charge margin was accepted by ``solve``,
    present in the config and published in diagnostics for a whole release while this
    reader was the missing link -- and stock installs never noticed, because the
    default was zero. These four default to *absent*, which fails the same way.
    """
    from custom_components.alpha_ems_manager.coordinator import SourceConfig

    hass.config_entries.async_update_entry(
        setup_integration,
        options={
            **setup_integration.options,
            CONF_BATTERY_INVESTMENT_EUR: 11000.0,
            CONF_BATTERY_SUBSIDY_EUR: 1000.0,
            CONF_OTHER_ONE_TIME_CREDIT_EUR: 250.0,
            CONF_BATTERY_INVESTMENT_DATE: "2026-05-01",
        },
    )
    await hass.async_block_till_done()

    config = SourceConfig.from_entry(setup_integration)

    assert config.battery_investment_eur == 11000.0
    assert config.battery_subsidy_eur == 1000.0
    assert config.other_one_time_credit_eur == 250.0
    assert config.battery_investment_date == "2026-05-01"


async def test_an_unset_investment_stays_none_rather_than_becoming_zero(
    setup_integration,
) -> None:
    """Absence has to survive all the way to the sensor.

    If it were coerced here, the reason the sensor publishes would be a lie about a
    figure that had already been turned into a number.
    """
    from custom_components.alpha_ems_manager.coordinator import SourceConfig

    config = SourceConfig.from_entry(setup_integration)

    assert config.battery_investment_eur is None
    assert config.battery_investment_date is None


# ===========================================================================
# absence is a different answer from zero
# ===========================================================================


async def test_no_investment_configured_is_unavailable_with_a_reason(
    hass, setup_integration, source_entities, frank
) -> None:
    """**Not entered and entered as zero are different facts.**

    Only the second is a measurement. Collapsing them would let an installation that
    has said nothing publish a recovery percentage against a battery that cost
    nothing -- an infinite return, presented as a fact.
    """
    from .test_beta33_campaign_wiring import planning_coordinator

    coordinator = await planning_coordinator(hass, setup_integration, frank)

    payload = coordinator.battery_return(TODAY)

    assert payload["available"] is False
    assert payload["unavailable_reason"] == ROI_UNAVAILABLE_NO_INVESTMENT
    assert "recovered_percent" not in payload


async def test_an_investment_with_no_sealed_days_is_unavailable_with_a_reason(
    invested,
) -> None:
    """A configured price and no measured days is not a recovery of zero.

    It is a figure that cannot be computed yet, and it says so -- while still
    publishing the investment arithmetic, which *is* known.
    """
    payload = invested.battery_return(TODAY)

    assert payload["available"] is False
    assert payload["unavailable_reason"] == ROI_UNAVAILABLE_NO_HISTORY
    assert payload["net_investment_eur"] == 10000.0


# ===========================================================================
# the arithmetic
# ===========================================================================


async def test_the_net_investment_subtracts_the_subsidy_and_the_credit(
    invested,
) -> None:
    """11000 gross, 1000 subsidy, 0 other credit -- 10000 net, and 1 % recovered
    for every 100 EUR the battery has actually saved."""
    _sealed(invested, days=10, benefit_eur=10.0)

    payload = invested.battery_return(TODAY)

    assert payload["gross_investment_eur"] == 11000.0
    assert payload["subsidy_eur"] == 1000.0
    assert payload["net_investment_eur"] == 10000.0
    assert payload["cumulative_realised_benefit_eur"] == pytest.approx(100.0)
    assert payload["recovered_percent"] == pytest.approx(1.0)
    assert payload["remaining_to_recover_eur"] == pytest.approx(9900.0)


async def test_the_cumulative_total_survives_the_days_being_evicted(
    invested,
) -> None:
    """**The reason the seal exists at all.**

    A day record is dropped at 365 days. Its benefit was computed while the evidence
    was on disk and folded forward as it went, so the lifetime figure is the same
    before and after -- which a re-derivation could not be, because the evidence it
    would re-derive from is gone.
    """
    from custom_components.alpha_ems_manager.storage import DayRecord

    _sealed(invested, days=5, benefit_eur=2.0)
    before = invested.battery_return(TODAY)["cumulative_realised_benefit_eur"]

    # **A recorded future day, not merely a future reference.** ``prune`` clamps its
    # reference to one day past the newest day stored, deliberately, so that a Pi
    # whose clock is years ahead until NTP corrects it cannot discard the whole
    # window. Passing a far date alone would therefore prune nothing, and this test
    # would pass by measuring the clamp rather than the fold.
    far = TODAY + timedelta(days=400)
    invested.store.days[far] = DayRecord(day=far, tz_key="Europe/Amsterdam")

    invested.store.prune(far)
    invested.store.days.pop(far)

    after = invested.battery_return(TODAY)
    assert invested.store.days == {}
    assert after["cumulative_realised_benefit_eur"] == pytest.approx(before)
    assert after["sealed_evicted_eur"] == pytest.approx(10.0)


# ===========================================================================
# the payback estimate, and its two refusals
# ===========================================================================


async def test_payback_is_withheld_below_the_sample_threshold(invested) -> None:
    """**Below this the estimate is not conservative, it is arbitrary.**

    A trailing mean over a fortnight of one season, extrapolated to a decade, says
    more about the weather than about the battery. A named reason is a better answer
    than a confident wrong number.
    """
    _sealed(invested, days=ROI_MIN_SAMPLE_DAYS - 1, benefit_eur=1.0)

    payload = invested.battery_return(TODAY)

    assert payload["estimated_payback_date"] is None
    assert payload["estimated_payback_years"] is None
    assert (
        payload["payback_unavailable_reason"]
        == ROI_PAYBACK_UNAVAILABLE_INSUFFICIENT_HISTORY
    )


async def test_payback_is_withheld_rather_than_infinite_when_nothing_was_earned(
    invested,
) -> None:
    """A trailing mean at or below zero is a real measurement and is published as
    one.

    Dividing by it would give either an error or a date in the past, and a date in
    the past would read as a fact -- "you paid this off last year" on a battery that
    has earned nothing.
    """
    _sealed(invested, days=ROI_MIN_SAMPLE_DAYS + 5, benefit_eur=-0.5)

    payload = invested.battery_return(TODAY)

    assert payload["cumulative_realised_benefit_eur"] < 0.0
    assert payload["estimated_payback_date"] is None
    assert payload["payback_unavailable_reason"] == ROI_PAYBACK_UNAVAILABLE_NO_BENEFIT


async def test_a_payback_estimate_is_published_once_there_is_enough_history(
    invested,
) -> None:
    """One published estimate, from the trailing 90-day mean, with the 30-day figure
    beside it so a reader can see the spread rather than being handed two answers.

    40 days at 1.00 EUR is 40 EUR earned against 10000 net, so 9960 remains at
    1.00/day: 9960 days, which is 27.27 years.
    """
    _sealed(invested, days=40, benefit_eur=1.0)

    payload = invested.battery_return(TODAY)

    assert payload["trailing_90d_days"] == 40
    assert payload["trailing_30d_days"] == 30
    assert payload["estimated_payback_years"] == pytest.approx(27.27, abs=0.01)
    assert (
        payload["estimated_payback_date"] == (TODAY + timedelta(days=9960)).isoformat()
    )
    assert payload["payback_unavailable_reason"] is None


async def test_the_trailing_windows_read_sealed_values_and_never_a_re_derivation(
    invested,
) -> None:
    """A trailing mean assembled by re-pricing would move whenever a day's prices
    were re-issued, and a payback estimate built on a mean that moves is not an
    estimate.

    Proved by leaving an unsealed day inside the window: it contributes nothing and
    is not counted, because a day with no sealed figure is a day the total does not
    cover.
    """
    from custom_components.alpha_ems_manager.storage import DayRecord

    _sealed(invested, days=10, benefit_eur=1.0)
    unsealed = TODAY - timedelta(days=11)
    invested.store.days[unsealed] = DayRecord(day=unsealed, tz_key="Europe/Amsterdam")

    payload = invested.battery_return(TODAY)

    assert payload["trailing_30d_days"] == 10
    assert payload["trailing_30d_eur"] == pytest.approx(10.0)
    assert payload["unsealed_past_days"] == 1


# ===========================================================================
# provenance -- never imply benefit was measured before it was
# ===========================================================================


async def test_a_purchase_date_before_the_evidence_is_reported_not_estimated(
    invested,
) -> None:
    """**The gap is published, and the total is still a true measurement of what it
    covers.**

    An operator may enter a date earlier than the first day this integration has
    authoritative accounting for. The figure must not read as "benefit since
    purchase" when the evidence starts later, and the missing months must not be
    filled in with an average -- that would be manufacturing history.
    """
    _sealed(invested, days=10, benefit_eur=1.0)

    payload = invested.battery_return(TODAY)

    assert payload["investment_date"] == "2026-05-01"
    assert (
        payload["history_available_since"] == (TODAY - timedelta(days=10)).isoformat()
    )
    assert payload["accounting_start_date"] == payload["history_available_since"]
    assert payload["lifetime_history_complete"] is False


async def test_history_is_complete_when_the_evidence_reaches_back_to_the_purchase(
    invested,
) -> None:
    """The positive case, so the negative one above is not the only reachable state.

    A test file that only ever proves the incomplete branch would pass on an
    implementation that hard-coded ``False``.
    """
    from dataclasses import replace

    invested.config = replace(
        invested.config,
        battery_investment_date=(TODAY - timedelta(days=5)).isoformat(),
    )
    _sealed(invested, days=10, benefit_eur=1.0)

    payload = invested.battery_return(TODAY)

    assert payload["lifetime_history_complete"] is True


# ===========================================================================
# the basis, published beside the figure
# ===========================================================================


async def test_the_two_price_legs_are_named_separately(invested) -> None:
    """**Half of it is cash and half is a reconstruction, so one word will not do.**

    Publishing a single basis for both would average over exactly the difference an
    operator needs to know about. ``export_leg_is_cash`` sits beside the euro figures
    rather than in a nested map, following the ``model_terms.is_cash`` precedent: a
    caveat reachable only through the diagnostics download is a caveat nobody reads.
    """
    _sealed(invested, days=10, benefit_eur=1.0)

    payload = invested.battery_return(TODAY)

    assert payload["import_leg_basis"] == PRICE_LEG_ALL_IN_CASH
    assert isinstance(payload["export_leg_basis"], list)
    assert payload["export_leg_is_cash"] is False
    assert (
        payload["calculation_basis"]
        == CALCULATION_BASIS_IMPORT_CASH_EXPORT_RECONSTRUCTED
    )


async def test_the_caveat_is_in_the_basis_name_itself(invested) -> None:
    """Not only in a boolean two keys away.

    A dashboard that shows one string shows this one, and it has to carry the
    qualification on its own.
    """
    _sealed(invested, days=10, benefit_eur=1.0)

    payload = invested.battery_return(TODAY)

    assert "reconstructed" in payload["calculation_basis"]
    assert "cash" in payload["calculation_basis"]


# ===========================================================================
# the boundary -- nothing here may decide anything
# ===========================================================================


def test_the_investment_keys_are_absent_from_the_settings_fingerprint() -> None:
    """**Capital cost is not a marginal cost, and most of an installed battery is
    sunk.**

    That is the reasoning the three economic levers are already built on. A purchase
    price that could move a dispatch would be a category error, so the fingerprint is
    asserted to take named arguments and none of them is one of these four --
    structural, rather than a convention someone has to remember.
    """
    import inspect

    from custom_components.alpha_ems_manager.economic import fingerprint_settings

    accepted = set(inspect.signature(fingerprint_settings).parameters)

    assert accepted.isdisjoint(
        {
            CONF_BATTERY_INVESTMENT_EUR,
            CONF_BATTERY_SUBSIDY_EUR,
            CONF_OTHER_ONE_TIME_CREDIT_EUR,
            CONF_BATTERY_INVESTMENT_DATE,
        }
    )
    assert not [name for name in accepted if "investment" in name or "subsidy" in name]


def test_no_forecast_or_planner_term_reaches_the_return_figure() -> None:
    """**The boundary the whole release turns on, asserted by reading the source.**

    Every excluded term revalues on each refresh -- opening and closing inventory
    value, remaining-expected, the revaluation, the day total and all three
    ``model_terms`` -- so any one of them in the numerator would make a *lifetime*
    figure move without a day having passed. A behavioural test cannot catch the
    addition of a term that happens to be small today; reading what the method may
    mention can.
    """
    import inspect

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    # **Docstrings stripped.** Two of these methods explain at length *why* those
    # terms are excluded, and scanning the prose would find the words it is looking
    # for in the sentence that forbids them -- a test that fails on its own
    # documentation, and passes the moment somebody deletes the explanation.
    source = "".join(
        _code_only(inspect.getsource(getattr(AlphaEmsCoordinator, name)))
        for name in ("battery_return", "_trailing_benefit", "_payback_from")
    )

    for forbidden in (
        "opening_inventory_value_eur",
        "closing_inventory_value_eur",
        "realised_plus_remaining_value_eur",
        "remaining_expected_today_eur",
        "forecast_revaluation_eur",
        "total_economic_value_today_eur",
        "model_terms",
        "decision_advantage_eur",
        "realized_net_value_eur",
    ):
        assert forbidden not in source, forbidden


def _code_only(source: str) -> str:
    """Return the source with every docstring removed."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))
