"""beta.40 Gate 5: Safety Buy stays physical, and stays price-blind.

**The rule that does not move.** Only physical reachability may *initiate* a grid
purchase. The retention gate is unambiguously economic -- it compares the
optimiser's dual against an export price -- so the question this file settles is
whether adding it changed anything about a compulsory buy.

It did not, and the reason is structural rather than careful: the verdict authorises
raising the battery **up to the measured production surplus**, which is energy
nobody bought. It cannot change how much is compelled, how much is bought, or why.
So the envelope may sit on a Safety Buy row -- absorbing free production during a
compulsory buy strictly reduces what must be bought later -- without the buy itself
acquiring a price.

Asserted by differencing whole publications rather than by inspecting fields: the
target published with a retention gate must equal the one published without it,
everywhere except the two row keys the gate adds.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_MIXED_BUY,
    ECONOMIC_ACTION_SAFETY_BUY,
    EXECUTION_INTENT_GRID_CHARGE,
    RETENTION_GATE_AUTHORISED,
    RETENTION_GATE_NOT_A_CHARGE,
)
from custom_components.alpha_ems_manager.economic import (
    RetentionGate,
    purchase_purpose,
    quarter_schedule_for,
)

from .beta40_trace import (
    EXPORT_PRICE_EUR_KWH,
    MARGINAL_VALUE_EUR_KWH,
    ROUND_TRIP_EFFICIENCY,
)
from .forecast_helpers import NORMAL, local

BASE = local(NORMAL, 12, 0)

#: The capture's own gate: 0.90 * 0.2237 = 0.2013 against an export price of
#: 0.1013, so it authorises with 0.0999 EUR/kWh to spare.
LIVE_GATE = RetentionGate(
    marginal_value_eur_kwh=MARGINAL_VALUE_EUR_KWH,
    round_trip_efficiency=ROUND_TRIP_EFFICIENCY,
)

#: The two keys the gate adds to a row, and the only difference it may make.
GATE_KEYS = frozenset({"retention_authorised", "retention_gate"})


class Interval:
    """A stand-in carrying only the fields the row builder reads."""

    def __init__(self, index: int, *, export_price: float = EXPORT_PRICE_EUR_KWH):
        self.index = index
        self.battery_charge_ac_kwh = 0.28
        self.battery_discharge_ac_kwh = 0.0
        self.marginal_grid_import_kwh = 0.06
        self.grid_export_kwh = 0.0
        self.marginal_grid_export_kwh = 0.0
        self.grid_import_kwh = 0.06
        self.start_energy_dc_kwh = 8.9856
        self.export_price_eur_kwh = export_price
        self.import_price_eur_kwh = 0.19588


def rows(
    *, retention: RetentionGate | None, intent: str = EXECUTION_INTENT_GRID_CHARGE
):
    """Return the published rows for two intervals, with or without the gate."""
    return quarter_schedule_for(
        (Interval(0), Interval(1)),
        start_index=0,
        end_index=1,
        intent=intent,
        moment=lambda i: BASE + timedelta(minutes=15 * i),
        retention=retention,
    )


# == 1. the gate changes nothing but itself ===============================


def test_the_gate_adds_two_keys_and_moves_no_other_figure() -> None:
    """**The neutrality claim, by difference rather than by inspection.**

    Every quantity a Safety Buy is decided from -- the battery objective, the grid
    ceiling, the meter figures, the executability verdict -- is byte-identical with
    the gate present and absent.

    *Mutation: let the gate alter ``battery_kwh`` or ``grid_authorised_kwh`` and
    this fails.*
    """
    without = rows(retention=None)
    with_gate = rows(retention=LIVE_GATE)

    assert len(without) == len(with_gate) == 2
    for plain, gated in zip(without, with_gate, strict=True):
        assert set(plain) == set(gated) - GATE_KEYS
        for key, value in plain.items():
            assert gated[key] == value, key


def test_a_publication_without_a_gate_carries_no_gate_keys_at_all() -> None:
    """Absent means the pre-beta.40 shape, not a published refusal.

    This is what lets the whole beta.39 assertion surface keep reading these rows
    unchanged, and what makes the neutrality replay a comparison of equals.
    """
    for row in rows(retention=None):
        assert GATE_KEYS.isdisjoint(row)


def test_the_gate_authorises_on_the_captures_own_numbers() -> None:
    """The vacuity gate for this file: the gate must actually be granting here.

    Without this, every assertion above would also pass on an implementation whose
    gate refused everything.
    """
    for row in rows(retention=LIVE_GATE):
        assert row["retention_authorised"] is True
        assert row["retention_gate"] == RETENTION_GATE_AUTHORISED


# == 2. the compulsory decision is untouched ==============================


@pytest.mark.parametrize(
    ("safety_buy", "safety_kwh", "economic_kwh", "expected"),
    [
        (False, None, None, EXECUTION_INTENT_GRID_CHARGE),
        (True, 1.0, 0.0, ECONOMIC_ACTION_SAFETY_BUY),
        (True, 0.0, 1.0, ECONOMIC_ACTION_SAFETY_BUY),
        (True, 1.0, 2.0, ECONOMIC_ACTION_MIXED_BUY),
        (True, None, None, ECONOMIC_ACTION_SAFETY_BUY),
    ],
)
def test_the_purchase_purpose_is_decided_without_any_price(
    safety_buy: bool, safety_kwh, economic_kwh, expected: str
) -> None:
    """``purchase_purpose`` takes no price and beta.40 gave it none.

    The whole Safety-Buy-versus-Economic-Buy attribution is a function of two
    energies and one boolean. A gate that had leaked into it would have had to
    appear in this signature.
    """
    assert (
        purchase_purpose(
            EXECUTION_INTENT_GRID_CHARGE,
            safety_buy=safety_buy,
            safety_buy_kwh=safety_kwh,
            economic_buy_kwh=economic_kwh,
        )
        == expected
    )


def test_the_gate_never_reaches_a_row_that_is_not_a_charge() -> None:
    """An export or a load-serving row is refused by construction, not by price.

    The verdict is about keeping production the battery would otherwise export;
    there is no such question on a row that is not charging.
    """
    from custom_components.alpha_ems_manager.const import EXECUTION_INTENT_NET_EXPORT

    for row in rows(retention=LIVE_GATE, intent=EXECUTION_INTENT_NET_EXPORT):
        assert row["retention_authorised"] is False
        assert row["retention_gate"] == RETENTION_GATE_NOT_A_CHARGE


# == 3. the gate is a comparison, and it does refuse =====================


@pytest.mark.parametrize(
    ("export_price", "authorised"),
    [
        # The capture: keeping wins comfortably.
        (EXPORT_PRICE_EUR_KWH, True),
        # A zero-price interval: keeping wins by everything.
        (0.0, True),
        # A negative export price -- keeping wins by more still.
        (-0.05, True),
        # An export price above the round-tripped value of holding: selling wins,
        # and beta.40 says so. This is why it is not a zero-export rule.
        (0.30, False),
        # Exactly at the boundary: the comparison is strict, so it refuses.
        (ROUND_TRIP_EFFICIENCY * MARGINAL_VALUE_EUR_KWH, False),
    ],
)
def test_the_tariff_decides_and_sometimes_decides_against_storing(
    export_price: float, authorised: bool
) -> None:
    """**The product rule, and its refusals are the load-bearing half.**

    ``eta_rt * V > export_price``. Where exporting genuinely pays more than keeping,
    exporting is correct and nothing suppresses it.
    """
    published = quarter_schedule_for(
        (Interval(0, export_price=export_price),),
        start_index=0,
        end_index=0,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        moment=lambda i: BASE + timedelta(minutes=15 * i),
        retention=LIVE_GATE,
    )

    assert published[0]["retention_authorised"] is authorised


def test_an_undefined_dual_refuses_rather_than_granting() -> None:
    """``None`` is a defined answer and zero is not.

    The lattice cannot always define the marginal value -- a violation boundary, an
    unreachable state, the top bucket. Publishing a grant there would be authorising
    on a number that does not exist.
    """
    blind = RetentionGate(
        marginal_value_eur_kwh=None, round_trip_efficiency=ROUND_TRIP_EFFICIENCY
    )

    published = quarter_schedule_for(
        (Interval(0),),
        start_index=0,
        end_index=0,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        moment=lambda i: BASE + timedelta(minutes=15 * i),
        retention=blind,
    )

    assert published[0]["retention_authorised"] is False


def test_a_missing_export_price_refuses_too() -> None:
    """No price, no comparison, no authorisation. Total rather than optimistic."""
    published = quarter_schedule_for(
        (Interval(0, export_price=None),),
        start_index=0,
        end_index=0,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        moment=lambda i: BASE + timedelta(minutes=15 * i),
        retention=LIVE_GATE,
    )

    assert published[0]["retention_authorised"] is False
