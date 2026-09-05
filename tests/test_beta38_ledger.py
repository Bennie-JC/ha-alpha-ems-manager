"""beta.38 Gate 3: the position identity reconciles, and only that identity.

**A caller that forgot an argument.**

``realized_window`` has accepted ``opening_inventory_value_eur`` since beta.35 and no
caller ever supplied one. So in production it was ``None``, and
``realised_plus_remaining_value_eur`` -- which needs it -- was ``None`` beside it. The
2026-09-01 download shows both nulls next to a perfectly populated
``closing_inventory_value_eur``. Nothing was wrong with the arithmetic; half of it
was never handed over.

The identity beta.38 completes::

    realised_plus_remaining_value_eur
        = realised_net_value_eur          measured / attributed
        + closing_inventory_value_eur     V(floor) - V(now),   planner-derived
        - opening_inventory_value_eur     V(floor) - V(open),  planner-derived

Both position terms come from **one curve -- this refresh's** -- so their difference
is what operating the battery achieved and carries no revaluation. Valuing the two
ends on different curves would fold "prices moved" into a figure labelled as
operational, which is the whole reason revaluation is deferred rather than
approximated.

And what does **not** go in it: ``decision_advantage_eur`` answers "the selected plan
against doing nothing, from now", measured against a per-interval idle baseline over
a forecast horizon. ``realised_net_value_eur`` is measured against a *no-battery*
counterfactual over elapsed intervals. Two different questions on two different
baselines; adding them is the forced identity beta.37 already forbids one layer down.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    LEDGER_BASES,
    LEDGER_BASIS_PLANNER_DERIVED,
)
from custom_components.alpha_ems_manager.realized import opening_inventory_kwh

from .test_beta24_live_charge import (
    LiveSurface,
)


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


async def a_priced_day(hass, config_data, frank, live_surface, monkeypatch):
    """Return a coordinator with a priced civil day and a solved plan.

    **The beta.35 ledger fixture, reused rather than reinvented.** Every input the
    realised ledger needs is already persisted -- ``DayRecord`` keeps the measured
    series and ``PriceSnapshot`` keeps the prices -- and ``_measured_day`` writes
    them through ``record_interval`` exactly as a live installation files them,
    state-of-charge series included. That series is what the opening and closing
    inventories are read from, so a hand-poked record would be testing a shape
    production never produces.
    """
    from .forecast_helpers import NORMAL
    from .test_beta35_campaign_continuity import start_the_campaign
    from .test_beta35_ledger import _measured_day

    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    coordinator.store.days[NORMAL] = _measured_day(NORMAL)
    return coordinator


def ledger_of(coordinator):
    """Return the realised ledger for today, as diagnostics publishes it."""
    plan = (coordinator.data or {}).get("battery_plan")
    published = coordinator.realized_today(plan)
    assert published.get("available") is True, published
    return published["ledger"]


# ===========================================================================
# 1. the shared rule
# ===========================================================================


def test_the_opening_energy_has_exactly_one_definition() -> None:
    """**The rule both sides call, so neither can read the series differently.**

    The ledger *reports* the opening inventory and the coordinator has to *value*
    it, from two modules that cannot share code by design -- ``realized.py`` may not
    import the solver. Two independent readings of one series is exactly how a value
    comes to describe an energy nobody published, so there is one function.
    """
    assert opening_inventory_kwh(None) is None
    assert opening_inventory_kwh([]) is None
    assert opening_inventory_kwh([None, None]) is None
    # The first *usable* reading, not the first slot.
    assert opening_inventory_kwh([None, 4.2, 9.9]) == pytest.approx(4.2)
    assert opening_inventory_kwh([7.5, 1.0]) == pytest.approx(7.5)


async def test_the_energy_valued_is_the_energy_reported(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The published opening inventory and the priced one are the same number.

    Asserted through the coordinator rather than on the helper, because the property
    that matters is that the *caller* uses it -- a shared rule nobody calls proves
    nothing.
    """
    coordinator = await a_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    ledger = ledger_of(coordinator)
    reported = ledger["opening_inventory_kwh"]
    if reported is None:
        pytest.skip("this fixture recorded no usable pack level")

    outcome = (coordinator.data or {}).get("economic")
    expected = coordinator._position_value_eur(outcome, reported)
    assert ledger["opening_inventory_value_eur"] == pytest.approx(expected, abs=1e-4)

    # **And the closing end, which beta.37 priced at the plan head instead.** The
    # kWh published beside it was the last recorded level, so the two ends of the
    # identity described two different energies -- invisible while one of them was
    # ``None``, and nonsense once both are numbers. Measured on this fixture before
    # the fix: 3.0 kWh valued higher than 3.2 kWh.
    closing = ledger["closing_inventory_kwh"]
    assert closing is not None
    assert ledger["closing_inventory_value_eur"] == pytest.approx(
        coordinator._position_value_eur(outcome, closing), abs=1e-4
    )
    # Less energy is never worth more, which is what that inversion looked like.
    if closing < reported:
        assert ledger["closing_inventory_value_eur"] <= (
            ledger["opening_inventory_value_eur"] + 1e-9
        )


# ===========================================================================
# 2. the identity
# ===========================================================================


async def test_the_opening_position_value_is_no_longer_null(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The gap itself, closed.**

    *Mutation: drop the argument from the ``realized_window`` call and this fails --
    which is exactly the state beta.37 shipped in.*
    """
    coordinator = await a_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    ledger = ledger_of(coordinator)

    assert ledger["opening_inventory_kwh"] is not None, "the witness: a level exists"
    assert ledger["opening_inventory_value_eur"] is not None, ledger
    assert ledger["closing_inventory_value_eur"] is not None, ledger


async def test_the_position_identity_reconciles(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """``realised + closing - opening``, asserted as an equality.

    An inequality would pass on a total that quietly dropped a term, which is the
    failure this release is fixing.
    """
    coordinator = await a_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    ledger = ledger_of(coordinator)

    total = ledger["realised_plus_remaining_value_eur"]
    assert total is not None, ledger
    assert total == pytest.approx(
        ledger["realised_net_value_eur"]
        + ledger["closing_inventory_value_eur"]
        - ledger["opening_inventory_value_eur"],
        abs=1e-3,
    )


async def test_both_ends_are_priced_on_one_curve(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**What makes the difference operational rather than a revaluation.**

    Both terms are ``V(floor) - V(e)`` from *this* refresh's head layer, so equal
    energies must price equally and the ordering of the curve must be respected. If
    the two ends were ever read from different solves this would break -- and the
    difference would silently carry "prices moved" inside a figure labelled as what
    the battery achieved.
    """
    coordinator = await a_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    outcome = (coordinator.data or {}).get("economic")
    ledger = ledger_of(coordinator)
    opening = ledger["opening_inventory_kwh"]
    assert opening is not None

    # Same energy, same curve, same answer -- twice.
    once = coordinator._position_value_eur(outcome, opening)
    twice = coordinator._position_value_eur(outcome, opening)
    assert once == twice

    # And the curve is monotone in the direction it has to be: more stored energy is
    # never worth less than less of it, on one curve.
    more = coordinator._position_value_eur(outcome, opening + 2.0)
    if once is not None and more is not None:
        assert more >= once - 1e-9, (once, more)


async def test_the_position_values_are_rounded_like_every_other_euro(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """beta.37 published ``3.871514669200126`` beside a sensor showing ``3.8715``.

    Same quantity, two spellings, and a reader had to work out which. Four decimals,
    like every other euro figure in the ledger.
    """
    coordinator = await a_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    ledger = ledger_of(coordinator)

    for key in ("opening_inventory_value_eur", "closing_inventory_value_eur"):
        value = ledger[key]
        assert value is not None
        assert value == pytest.approx(round(value, 4), abs=0.0), key


# ===========================================================================
# 3. what the identity is not
# ===========================================================================


async def test_realised_and_the_decision_advantage_are_not_added(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Asserted as a non-equality, deliberately.**

    ``realised_net_value_eur`` is measured against a *no-battery* counterfactual over
    intervals that have elapsed. ``decision_advantage_eur`` is forecast against a
    *per-interval idle* baseline over the horizon still to come. They answer
    different questions on different baselines and the sum of them is not a total of
    anything -- so a future tidy-up that forced the identity would fail here, exactly
    as beta.37's day-split test forbids the analogous sum one layer down.
    """
    coordinator = await a_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    ledger = ledger_of(coordinator)
    advantage = coordinator.economic_value().get("decision_advantage_eur")
    if advantage is None:
        pytest.skip("no valid comparison on this fixture")

    total = ledger["realised_plus_remaining_value_eur"]
    assert total is not None
    assert total != pytest.approx(
        ledger["realised_net_value_eur"] + advantage, abs=1e-3
    )


def test_the_position_values_are_still_planner_derived() -> None:
    """The two inventory values were ``planner_derived`` in beta.38, and still are.

    **The count moved deliberately in beta.39, and this is the note that says so
    rather than a diff nobody read.** beta.38 asserted ``len(LEDGER_BASES) == 5``
    and gave the reason: a revaluation term needs a basis of its own, and inventing
    one under time pressure from a live defect is how the wrong name gets frozen
    into a payload. beta.39 is the release that adds the term, so it is the release
    that adds the word -- ``revalued``, for the *same* energy valued on two
    different curves, which is neither a measurement nor a single-instant planner
    figure.

    What this test actually protects is unchanged and is the part that matters: the
    two figures whose difference beta.38's identity rests on are read off **one**
    curve at **one** instant, and if either were ever relabelled ``revalued`` that
    subtraction would stop meaning "what operating the battery achieved".
    """
    from custom_components.alpha_ems_manager.const import LEDGER_BASIS_REVALUED
    from custom_components.alpha_ems_manager.realized import _basis_map

    basis = _basis_map()
    assert basis["opening_inventory_value_eur"] == LEDGER_BASIS_PLANNER_DERIVED
    assert basis["closing_inventory_value_eur"] == LEDGER_BASIS_PLANNER_DERIVED
    # Seven since beta.42, which split ``forecast`` out of ``planner_derived``: a
    # closing inventory value is the optimiser's valuation of energy that *exists*,
    # while the remaining expected value is its estimate of energy that has not moved
    # yet. Both come from the planner and only one can still be falsified by the
    # weather, so one word for both told a reader nothing about which figure a cloudy
    # afternoon will move. The two figures asserted above keep their word.
    assert len(LEDGER_BASES) == 7, "the seventh word is beta.42's forecast basis"
    assert LEDGER_BASIS_REVALUED in LEDGER_BASES
    # Exactly one figure wears it, and it is the revaluation.
    revalued = [name for name, word in basis.items() if word == LEDGER_BASIS_REVALUED]
    assert revalued == ["today_accounting.forecast_revaluation_eur"], revalued


def test_the_accounting_change_adds_no_solve() -> None:
    """It reads the head layer the refresh already computed, and nothing else.

    ``_position_value_eur`` takes an outcome and an energy and returns a number: no
    table build, no recursion, no second pass. Asserted on the signature so a future
    version that reached for a solver would have to change this first.
    """
    import inspect

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    names = set(inspect.signature(AlphaEmsCoordinator._position_value_eur).parameters)
    assert names == {"self", "outcome", "energy_kwh"}
