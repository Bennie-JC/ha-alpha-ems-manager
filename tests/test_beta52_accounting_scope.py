"""The investment return covers the investment, and nothing before it.

``accounting_start_date`` has been published since beta.42 and applied to nothing.
The cumulative figure summed every sealed day the store held, the trailing windows
looked only at their own width, and the sample count counted everything -- so
benefit earned before the battery was bought was subtracted from what remained to
recover and drove the recovered percentage.

That is not a conservative approximation. Money saved in a week the battery did not
exist cannot recover its cost, and including it moves the headline number in the
flattering direction. The published pair said so plainly on the installation that
prompted this work: ``sealed_through`` sat three days *before*
``accounting_start_date`` -- an accounting period ending before it began.

The days themselves are not discarded. They are still measured, still sealed, still
on disk, and now published under their own names.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from .test_beta42_battery_return import TODAY, _sealed, invested  # noqa: F401


def _invested_on(coordinator, day: date):
    """Point the configured purchase date at ``day``."""
    coordinator.config = replace(
        coordinator.config, battery_investment_date=day.isoformat()
    )


# ===========================================================================
# the bound
# ===========================================================================


async def test_days_before_the_purchase_do_not_reach_the_recovery_figure(
    invested,  # noqa: F811
) -> None:
    """**The shape of the installation that prompted this, reproduced exactly.**

    Every sealed day predates the purchase, so the return has measured nothing about
    this investment yet -- and must say so, rather than reporting a percentage
    recovered out of days the battery was not there for.
    """
    _sealed(invested, days=2, benefit_eur=3.2445, end=TODAY - timedelta(days=6))
    _invested_on(invested, TODAY - timedelta(days=4))

    payload = invested.battery_return(TODAY)

    # Nothing inside the period has been measured yet, so there is no return to
    # report -- and the refusal names *why*, because "no history" would send an
    # operator looking for a fault in a measurement that is working perfectly.
    assert payload["available"] is False
    assert payload["unavailable_reason"] == "no_finalised_days_in_accounting_period"
    assert payload["sealed_days_before_accounting_start"] == 2
    assert payload["benefit_before_accounting_start_eur"] == pytest.approx(6.489)
    # The investment is untouched: nothing before the purchase reduces it.
    assert payload["net_investment_eur"] == pytest.approx(10000.0)


async def test_the_period_never_ends_before_it_begins(invested) -> None:  # noqa: F811
    """The invariant behind the previous test, stated so it cannot regress quietly.

    Two published dates that contradict each other are worse than one missing date,
    because a reader has no way to tell which of them is the lie.
    """
    _sealed(invested, days=2, benefit_eur=3.2445, end=TODAY - timedelta(days=6))
    _invested_on(invested, TODAY - timedelta(days=4))

    payload = invested.battery_return(TODAY)

    # Previously this published sealed_through 2026-08-25 against an
    # accounting_start_date of 2026-08-28: a period that ended before it began.
    if payload.get("sealed_through") is not None:
        assert payload["sealed_through"] >= payload["accounting_start_date"]
    assert payload["accounting_start_date"] == (TODAY - timedelta(days=4)).isoformat()


async def test_the_sample_count_and_mean_cover_only_the_accounting_period(
    invested,  # noqa: F811
) -> None:
    """A mean is its total over its count, and both halves must span one period.

    Ten sealed days of which four predate the purchase is a six-day measurement, and
    dividing a six-day total by ten would understate the daily benefit as surely as
    the reverse would overstate it.
    """
    _sealed(invested, days=10, benefit_eur=2.0)
    _invested_on(invested, TODAY - timedelta(days=6))

    payload = invested.battery_return(TODAY)

    assert payload["sample_days"] == 6
    assert payload["cumulative_realised_benefit_eur"] == pytest.approx(12.0)
    assert payload["average_realised_benefit_per_day_eur"] == pytest.approx(2.0)


async def test_the_trailing_windows_are_clamped_to_the_accounting_period(
    invested,  # noqa: F811
) -> None:
    """A thirty-day window over a battery owned for six days is a six-day window.

    Published beside its own day count for exactly this reason: a mean over six days
    and a mean over thirty are different figures, and only the count says which.
    """
    _sealed(invested, days=40, benefit_eur=1.5)
    _invested_on(invested, TODAY - timedelta(days=6))

    payload = invested.battery_return(TODAY)

    assert payload["trailing_30d_days"] == 6
    assert payload["trailing_30d_eur"] == pytest.approx(9.0)
    assert payload["trailing_90d_days"] == 6
    assert payload["trailing_90d_eur"] == pytest.approx(9.0)


async def test_what_was_earned_before_the_purchase_is_published_not_discarded(
    invested,  # noqa: F811
) -> None:
    """**It is a real measurement of real days, and it does not simply vanish.**

    The figure an operator has been reading is still there under a name that says
    what it is, so upgrading does not look like data loss.
    """
    _sealed(invested, days=10, benefit_eur=2.0)
    _invested_on(invested, TODAY - timedelta(days=6))

    payload = invested.battery_return(TODAY)

    assert payload["sealed_days_before_accounting_start"] == 4
    assert payload["benefit_before_accounting_start_eur"] == pytest.approx(8.0)


async def test_no_investment_date_leaves_every_sealed_day_in_scope(
    invested,  # noqa: F811
) -> None:
    """Without a purchase date there is no period to be outside of.

    The bound is the operator's statement about when the battery started earning; in
    its absence the figure covers what it has always covered rather than nothing.
    """
    _sealed(invested, days=10, benefit_eur=2.0)
    invested.config = replace(invested.config, battery_investment_date=None)

    payload = invested.battery_return(TODAY)

    assert payload["sample_days"] == 10
    assert payload["cumulative_realised_benefit_eur"] == pytest.approx(20.0)
    assert payload["sealed_days_before_accounting_start"] == 0


# ===========================================================================
# the evicted running total, which carries no dates
# ===========================================================================


async def test_an_evicted_total_wholly_inside_the_period_is_added_whole(
    invested,  # noqa: F811
) -> None:
    """Evicted days are a scalar with no dates, so they are placed, never split.

    When the cursor they are complete through falls on or after the purchase date,
    every day behind it is inside the period and the total is added as it stands.
    """
    _sealed(invested, days=2, benefit_eur=1.0)
    _invested_on(invested, TODAY - timedelta(days=40))
    invested.store.sealed_benefit_eur = 15.0
    invested.store.sealed_day_count = 5
    invested.store.sealed_through = TODAY - timedelta(days=30)

    payload = invested.battery_return(TODAY)

    assert payload["sample_days"] == 7
    assert payload["cumulative_realised_benefit_eur"] == pytest.approx(17.0)


async def test_an_evicted_total_wholly_before_the_period_is_dropped_whole(
    invested,  # noqa: F811
) -> None:
    """**And never pro-rated.**

    An estimated share of a scalar would be a model term inside a figure whose whole
    value is that no model can reach it. When the cursor falls before the purchase
    date every day it covers is outside, so it is dropped in full.
    """
    _sealed(invested, days=2, benefit_eur=1.0)
    _invested_on(invested, TODAY - timedelta(days=5))
    invested.store.sealed_benefit_eur = 15.0
    invested.store.sealed_day_count = 5
    invested.store.sealed_through = TODAY - timedelta(days=30)

    payload = invested.battery_return(TODAY)

    assert payload["sample_days"] == 2
    assert payload["cumulative_realised_benefit_eur"] == pytest.approx(2.0)
    assert payload["pre_investment_days_included"] is False
