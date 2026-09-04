"""beta.41: the post-horizon demand window, and why it used to be empty.

**The defect this file exists for is silent, which is why the tests assert on
provenance and not only on figures.**

:class:`TerminalValue` prices what is left in the pack at the horizon edge in two
segments: the part the household will consume before the next free refill, at the
import price it displaces, and the spare, at the export price. The first segment
is what makes the curve concave, and concavity is what stops the value becoming a
licence to hoard.

That first segment had **zero width on every afternoon refresh.** The window was
``demands[horizon_intervals:]``, ``horizon_intervals`` is the prefix where a known
price *and* a demand both exist, and the price series is built one entry per
demand -- so the moment tomorrow's day-ahead publishes, the two series end at the
same instant and the slice is empty. The whole curve collapsed onto the export
rate:

    eta_discharge * export_price = 0.948683 * 0.15825 = 0.15013 EUR/kWh

which is exactly the ``stored_energy_marginal_value_eur_kwh`` the live 2026-09-03
20:45 diagnostic published, and it puts the break-even import price for any grid
charge at **0.0924** -- below the cheapest quarter the installation has ever
recorded. No Buy was reachable at any price.

Nothing announced it. A collapsed window and a genuine forecast of darkness
produced identical figures, so the published basis and stop reason are part of the
fix rather than decoration.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    TERMINAL_WINDOW_CLOCK_MATCHED,
    TERMINAL_WINDOW_EMPTY,
    TERMINAL_WINDOW_FORECAST_TAIL,
    TERMINAL_WINDOW_STOP_PV_BLIND,
    TERMINAL_WINDOW_STOP_SURPLUS,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    post_horizon_window,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .beta41_trace import (
    BREAK_EVEN_IMPORT_EUR_KWH,
    CHEAPEST_QUARTER_EVER_SEEN,
    DISCHARGE_EFFICIENCY,
    GRID_CHARGE_MARGIN_EUR_PER_KWH,
    MARGINAL_VALUE_EUR_KWH,
    TERMINAL_EXPORT_PRICE_EUR_KWH,
)

DAY = 96


def series(
    *,
    first: int,
    count: int,
    load: float = 0.24,
    pv: float | None = 0.0,
    price=lambda index: 0.30,
    surplus_at: int | None = None,
):
    """Return an aligned demand/price pair, one price per demand."""
    demands = []
    for offset in range(count):
        index = first + offset
        production = pv
        if surplus_at is not None and index == surplus_at and pv is not None:
            production = load + 1.0
        demands.append(
            IntervalDemand(index=index, baseline_kwh=load, pv_kwh=production)
        )
    prices = tuple(
        IntervalPrice(import_eur_kwh=price(d.index), export_eur_kwh=0.15825)
        for d in demands
    )
    return tuple(demands), prices


# == 1. the collapse, and that it is repaired ==============================


def test_the_window_survives_the_day_ahead_publishing() -> None:
    """**The regression test for the reported fault.**

    Prices reaching the end of the demand series is the ordinary afternoon state,
    not an edge case: it is what publishing tomorrow's day-ahead *means*. The
    window must still have width, and must say it was replayed by clock rather
    than read from a real tail.

    *Mutation: return an empty window when the tail is empty and this fails.*
    """
    demands, prices = series(first=84, count=108)

    window = post_horizon_window(
        demands, prices, horizon_intervals=len(demands), today_interval_count=DAY
    )

    assert window.demand_ac_kwh > 0.0
    assert window.displaced_price_eur_kwh > 0.0
    assert window.intervals > 0
    assert window.basis == TERMINAL_WINDOW_CLOCK_MATCHED


def test_a_real_tail_is_still_read_as_a_real_tail() -> None:
    """The pre-publication path, which already worked and must keep working.

    While the prices stop short of the forecast there *is* a tail, and it is the
    better evidence: it is this horizon's own forecast rather than a replay of an
    earlier clock position.
    """
    demands, prices = series(first=84, count=108)

    window = post_horizon_window(
        demands, prices, horizon_intervals=40, today_interval_count=DAY
    )

    assert window.basis == TERMINAL_WINDOW_FORECAST_TAIL
    assert window.demand_ac_kwh > 0.0


def test_the_collapsed_curve_is_the_number_the_installation_published() -> None:
    """The arithmetic that makes this a defect rather than a preference.

    With no served segment the whole credit is the spare segment, whose marginal
    rate is ``eta_discharge * export_price``. That product is the published
    marginal value to five decimals, and the break-even import price it implies
    sits below the cheapest quarter the site has ever seen -- so the refusal was
    structural and no tariff could have cleared it.
    """
    collapsed = DISCHARGE_EFFICIENCY * TERMINAL_EXPORT_PRICE_EUR_KWH

    assert collapsed == pytest.approx(MARGINAL_VALUE_EUR_KWH, abs=5e-6)
    break_even = DISCHARGE_EFFICIENCY * collapsed - GRID_CHARGE_MARGIN_EUR_PER_KWH
    assert break_even == pytest.approx(BREAK_EVEN_IMPORT_EUR_KWH, abs=1e-9)
    assert break_even < CHEAPEST_QUARTER_EVER_SEEN, (
        "structurally unreachable: no quarter the market has offered clears it"
    )


# == 2. the estimator may never read an unknown price ======================


def test_the_replay_reads_only_prices_the_horizon_priced() -> None:
    """**A guarantee, not a hope.** Every price in the priced prefix is known.

    ``actionable_intervals`` stops at the first interval that is not ``known``, so
    sourcing the estimator from that prefix alone means an unpublished price
    cannot influence a decision. Proved by making everything past the horizon
    hostile and showing the answer does not move.
    """
    demands, prices = series(first=84, count=108)
    head = 60
    poisoned = prices[:head] + tuple(
        IntervalPrice(import_eur_kwh=99.0, export_eur_kwh=99.0) for _ in prices[head:]
    )

    honest = post_horizon_window(
        demands, prices[:head], horizon_intervals=head, today_interval_count=DAY
    )
    hostile = post_horizon_window(
        demands, poisoned, horizon_intervals=head, today_interval_count=DAY
    )

    assert hostile.displaced_price_eur_kwh == pytest.approx(
        honest.displaced_price_eur_kwh
    )
    assert hostile.displaced_price_eur_kwh < 1.0


def test_the_clock_slot_is_read_at_the_clock_and_not_at_the_offset() -> None:
    """**A live defect of its own, and it was wrong on the path that worked.**

    The old estimator computed an *absolute* civil-day slot and then indexed the
    price array with it -- but that array's zero is the **head**, not midnight, so
    a 14:00 head priced tomorrow 02:00 at today 16:15. It is the same frame error
    already recorded for ``survival_window_end``: absolute and head-relative agree
    only while the head is zero, which is true in every fixture and in no live
    refresh.

    Constructed so the two answers cannot coincide. The horizon is a full civil
    day from a 14:00 head, so every clock slot is covered and the mean fallback
    cannot fire; exactly one slot is cheap, and it is the slot the first
    post-horizon interval lands on. With ``lookahead=1`` the window is that one
    interval, so the displaced price *is* the price of the slot the estimator
    chose: 0.05 read at the clock, 0.60 read at the offset.
    """
    head_index = 57  # 14:00 elapsed 56, so the series starts here
    cheap_slot = (head_index + 96) % DAY  # the slot the tail's first interval hits

    def one_cheap_slot(index: int) -> float:
        return 0.05 if (index % DAY) == cheap_slot else 0.60

    demands, prices = series(first=head_index, count=DAY + 8, price=one_cheap_slot)
    head = DAY  # a full day priced, so every slot is present

    window = post_horizon_window(
        demands,
        prices,
        horizon_intervals=head,
        today_interval_count=DAY,
        lookahead=1,
    )

    assert window.intervals == 1
    assert window.basis == TERMINAL_WINDOW_FORECAST_TAIL

    # The old reading, reproduced here so the discriminator is visible rather than
    # asserted: an absolute slot used as a position into a head-relative array.
    tail_index = demands[head].index
    absolute_slot = tail_index % DAY
    head_relative = prices[absolute_slot].import_eur_kwh
    assert head_relative == pytest.approx(0.60), (
        "the witness: the two readings genuinely disagree on this fixture"
    )

    assert window.displaced_price_eur_kwh == pytest.approx(0.05), (
        "read at the clock slot; the head-relative offset gives 0.60"
    )


@pytest.mark.parametrize("day_intervals", [92, 96, 100])
def test_a_daylight_saving_day_maps_its_own_slots(day_intervals: int) -> None:
    """A civil day is 92 or 100 intervals twice a year, and a modulus against 96
    silently maps tomorrow's evening onto today's afternoon.

    The arithmetic is the price alignment's own: below the count it *is* the slot,
    at or above it the count is subtracted once. Asserted by giving one clock
    position a unique price and requiring the replay to find it.
    """
    first = day_intervals - 8

    def spike(index: int) -> float:
        return 0.11 if (index % day_intervals) == 0 else 0.44

    demands, prices = series(
        first=first, count=day_intervals + 8, price=spike, load=0.20
    )

    window = post_horizon_window(
        demands,
        prices,
        horizon_intervals=len(demands),
        today_interval_count=day_intervals,
    )

    assert window.intervals > 0
    assert window.basis == TERMINAL_WINDOW_CLOCK_MATCHED
    # The spike is one slot in the replayed window, so the weighted mean must sit
    # strictly between the two prices rather than land on either.
    assert 0.11 < window.displaced_price_eur_kwh < 0.44


# == 3. the physical bound, and every stopping rule shortens ===============


def test_the_next_free_refill_still_ends_the_window() -> None:
    """Physical, price-blind, and the bound that stops a full pack being credited
    for the morning's sun.

    It now fires on the replayed window too, because the proxy carries its own
    production forecast -- which is the half the old code could not do at all,
    since an empty window has nothing to test.
    """
    demands, prices = series(first=84, count=108, surplus_at=100)

    window = post_horizon_window(
        demands, prices, horizon_intervals=len(demands), today_interval_count=DAY
    )

    assert window.stopped_by == TERMINAL_WINDOW_STOP_SURPLUS
    assert window.intervals < len(demands)


def test_a_pv_blind_forecast_earns_no_window_at_all() -> None:
    """``None`` production is not a forecast of darkness.

    Without a production forecast the next free refill cannot be located, so a
    segment whose entire definition is "before the next free refill" has no
    defensible width. Refusing is the conservative answer: it narrows the served
    segment and *lowers* the worth of stored energy, and overstating that worth is
    what authorises real spending.
    """
    demands, prices = series(first=84, count=108, pv=None)

    window = post_horizon_window(
        demands, prices, horizon_intervals=len(demands), today_interval_count=DAY
    )

    assert window.basis == TERMINAL_WINDOW_EMPTY
    assert window.stopped_by == TERMINAL_WINDOW_STOP_PV_BLIND
    assert window.demand_ac_kwh == 0.0
    assert window.displaced_price_eur_kwh == 0.0


def test_an_empty_horizon_is_refused_rather_than_guessed() -> None:
    """No priced prefix means no estimator, and therefore no window."""
    demands, prices = series(first=84, count=8)

    assert (
        post_horizon_window(
            demands, prices, horizon_intervals=0, today_interval_count=DAY
        ).basis
        == TERMINAL_WINDOW_EMPTY
    )
    assert (
        post_horizon_window((), (), horizon_intervals=4, today_interval_count=DAY).basis
        == TERMINAL_WINDOW_EMPTY
    )


def test_a_missing_interval_count_degrades_instead_of_raising() -> None:
    """The slot helper returns an unmatchable sentinel rather than dividing."""
    demands, prices = series(first=84, count=108)

    window = post_horizon_window(
        demands, prices, horizon_intervals=len(demands), today_interval_count=0
    )

    assert window.basis == TERMINAL_WINDOW_EMPTY


def test_the_replay_is_never_wider_than_the_lookahead_allows() -> None:
    """The cap still binds, and the window reports how many intervals it counted."""
    demands, prices = series(first=84, count=108)

    window = post_horizon_window(
        demands,
        prices,
        horizon_intervals=len(demands),
        today_interval_count=DAY,
        lookahead=6,
    )

    assert window.intervals <= 6
