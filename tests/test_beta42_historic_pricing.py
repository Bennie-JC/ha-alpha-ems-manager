"""A past day can be priced, and is priced on the basis published for it.

**The defect this closes shipped for four releases.** ``realized_days`` read prices
from ``coordinator.price_forecasts``, which is rebuilt on every refresh and holds
today and tomorrow. Any older day found no prices and was skipped, so the multi-day
ledger silently priced exactly one day -- while its own docstring said it read the
persisted issuances, and the published ``realized_window`` reported
``days_priced: 1`` on every installation.

Nothing caught it because the only test that reached two days reached it by hand,
injecting a yesterday forecast into ``price_forecasts`` -- a state production never
produces. So the fixture proved the arithmetic and not the wiring.

These tests use the real path: a day recorded in the learning store, its prices in the
forecast history and **not** in ``price_forecasts``, which is exactly the state any
day older than tomorrow is in.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    PRICE_BASIS_LIVE_FORECAST,
    PRICE_BASIS_STORED_SNAPSHOT,
)


def _measured_day(day: date):
    """Return one civil day of measured flows, filed the way production files them."""
    from .synthetic import empty_day

    record = empty_day(day)
    stored = 30.0
    for index in range(record.interval_count):
        imported = 0.5 if index < 8 else 0.0
        exported = 0.4 if 20 <= index < 28 else 0.0
        if imported:
            stored += 2.0
        elif exported:
            stored -= 2.0
        record.record_interval(
            index,
            measured_kwh=0.15,
            ev_kwh=None,
            ev_expected=False,
            pv_kwh=0.0,
            grid_import_kwh=imported,
            grid_export_kwh=exported,
            soc_percent=min(100.0, max(0.0, stored)),
        )
    return record


@pytest.fixture
async def priced_history(hass, setup_integration, source_entities, frank):
    """Return a coordinator holding two measured days, priced two different ways.

    Today's prices sit in ``price_forecasts`` where the live refresh puts them.
    **Yesterday's sit only in the forecast history**, which is precisely the state
    every day older than tomorrow is in on a real installation -- and the state no
    previous fixture reproduced, because the one multi-day test injected a
    yesterday forecast into ``price_forecasts`` by hand.
    """
    from dataclasses import replace

    from custom_components.alpha_ems_manager.price_forecast import build_price_snapshot

    from .test_beta33_campaign_wiring import planning_coordinator

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    plan = (coordinator.data or {}).get("battery_plan")
    assert plan is not None

    today = plan.target_day
    yesterday = today - timedelta(days=1)
    coordinator.store.days[today] = _measured_day(today)
    coordinator.store.days[yesterday] = _measured_day(yesterday)

    live = (coordinator.price_forecasts or {}).get(today)
    assert live is not None, "the driven refresh must have stored today's prices"

    # Yesterday, at prices of its own, persisted through the real writer and then
    # deliberately absent from the live map.
    dearer = replace(
        live,
        target_day=yesterday,
        intervals=tuple(
            replace(
                interval,
                import_price_eur_kwh=(
                    None
                    if interval.import_price_eur_kwh is None
                    else interval.import_price_eur_kwh * 2.0
                ),
            )
            for interval in live.intervals
        ),
    )
    snapshot = build_price_snapshot(
        dearer,
        issued_at=coordinator.store.days[yesterday].day and _issued_at(yesterday),
        interval_count=coordinator.store.days[yesterday].interval_count,
    )
    coordinator.history.add_price_snapshot(snapshot)
    coordinator.price_forecasts.pop(yesterday, None)

    return coordinator, plan


def _issued_at(day: date):
    """Return a fixed issuance instant for a stored day."""
    from datetime import datetime

    from homeassistant.util import dt as dt_util

    return datetime(day.year, day.month, day.day, 14, 0, tzinfo=dt_util.UTC)


def test_a_stored_snapshot_prices_a_day_the_live_forecast_has_forgotten(
    priced_history,
) -> None:
    """**The wiring, which is the part that was missing.**

    Yesterday is absent from ``price_forecasts`` and present in the forecast
    history. It must price from the history rather than be skipped.
    """
    coordinator, plan = priced_history
    yesterday = plan.target_day - timedelta(days=1)

    assert yesterday not in (coordinator.price_forecasts or {}), (
        "the fixture must reproduce the real state: a past day has no live forecast"
    )

    ledger = coordinator.realized_days(plan, days=2)

    assert ledger["available"] is True
    assert ledger["days_priced"] == 2, ledger
    assert ledger["first_day"] == yesterday.isoformat()
    assert ledger["last_day"] == plan.target_day.isoformat()


def test_the_basis_of_every_priced_day_is_published(priced_history) -> None:
    """A reader must be able to tell which days came from where.

    Today is priced from the live forecast and yesterday from the stored issuance,
    and both bases appear -- so a window built entirely from history is
    distinguishable from one built entirely from the current refresh.
    """
    coordinator, plan = priced_history

    ledger = coordinator.realized_days(plan, days=2)

    assert sorted(ledger["price_basis"]) == sorted(
        [PRICE_BASIS_LIVE_FORECAST, PRICE_BASIS_STORED_SNAPSHOT]
    )


def test_a_past_day_is_priced_at_the_rates_published_for_it(priced_history) -> None:
    """**Not at today's rates, and not under today's configuration.**

    The stored issuance carries the prices as published, so a day priced from it is
    priced at what it actually cost. Yesterday's import price is deliberately
    different from today's, and the ledger's import cost has to show it: a
    two-day window costs the sum of two different days, never twice one of them.
    """
    coordinator, plan = priced_history

    one_day = coordinator.realized_days(plan, days=1)
    two_days = coordinator.realized_days(plan, days=2)

    yesterday_cost = two_days["import_cost_eur"] - one_day["import_cost_eur"]
    assert yesterday_cost > 0.0, two_days
    # The two days used different prices, so their costs cannot be equal.
    assert yesterday_cost != pytest.approx(one_day["import_cost_eur"]), (
        one_day["import_cost_eur"],
        yesterday_cost,
    )


def test_a_day_with_no_stored_prices_is_skipped_and_not_valued_at_zero(
    priced_history,
) -> None:
    """The rule that survives: missing prices mean the day is not counted.

    Asking for more days than the history holds must lower ``days_priced`` rather
    than quietly average in days worth nothing -- which would drag a lifetime figure
    toward zero for every day the installation was switched off.
    """
    coordinator, plan = priced_history

    ledger = coordinator.realized_days(plan, days=10)

    assert ledger["days_priced"] == 2, ledger["days_priced"]
