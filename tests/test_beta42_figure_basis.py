"""Cash, attribution, model value and forecast are distinguishable at the figure.

**The basis map existed for four releases and the entity could not see it.** It
lives in the ledger block, which reaches the diagnostics download and nothing else.
An operator reading the Economic Value sensor saw a dozen adjacent euro attributes
spanning four different kinds of number -- cash from a meter, energy split by an
attribution rule, a planner valuation of energy that exists, and a forecast of energy
that has not moved -- distinguished only by their names, on an entity Home Assistant
labels ``MONETARY``. Adding a hurdle rate to a cash total was then a matter of
reading two attribute names and assuming.

So the basis is projected up to the entity, from the same map, and an unclassified
euro figure says so rather than being quietly left out: an attribute with *no* entry
and one whose entry is "nobody classified this" look identical to a reader who only
sees the ones that are present, and only the second is worth noticing.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    LEDGER_BASES,
    LEDGER_BASIS_ATTRIBUTED,
    LEDGER_BASIS_FORECAST,
    LEDGER_BASIS_MEASURED,
    LEDGER_BASIS_PLANNER_DERIVED,
    LEDGER_BASIS_UNCLASSIFIED,
)
from custom_components.alpha_ems_manager.realized import _basis_map


def test_the_seventh_word_separates_a_forecast_from_a_valuation() -> None:
    """**``planner_derived`` was carrying two different claims.**

    A closing inventory value is the optimiser's valuation of energy that *exists*;
    the remaining expected value is its estimate of energy that has not moved yet,
    over prices and a load forecast that will both be wrong to some degree. Both come
    from the planner, and only one can still be falsified by the weather. A reader
    told the same word about both cannot tell which figure a cloudy afternoon moves.
    """
    basis = _basis_map()

    assert LEDGER_BASIS_FORECAST in LEDGER_BASES
    assert (
        basis["today_accounting.remaining_expected_today_eur"] == LEDGER_BASIS_FORECAST
    )
    assert basis["closing_inventory_value_eur"] == LEDGER_BASIS_PLANNER_DERIVED


def test_a_total_is_no_stronger_than_its_weakest_addend() -> None:
    """Two corrections, one rule.

    ``realised_net_value_eur`` was labelled ``measured`` while one of its addends is
    ``attributed``; the day total was labelled ``planner_derived`` while one of its
    addends is a forecast. A basis map that upgrades a figure's honesty by summing it
    is worse than no basis map at all.
    """
    basis = _basis_map()

    assert basis["avoided_import_value_eur"] == LEDGER_BASIS_ATTRIBUTED
    assert basis["realised_net_value_eur"] == LEDGER_BASIS_ATTRIBUTED
    assert (
        basis["today_accounting.total_economic_value_today_eur"]
        == LEDGER_BASIS_FORECAST
    )


def test_the_corrected_comparator_and_its_legs_are_measured() -> None:
    """Four cash legs and the two differences built from them.

    No attribution rule, no model constant, no planner valuation -- which is exactly
    what makes them the only figures in this module an investment return may be
    built on.
    """
    basis = _basis_map()

    for name in (
        "no_battery_import_kwh",
        "no_battery_export_kwh",
        "no_battery_cost_eur",
        "no_battery_export_revenue_eur",
        "no_battery_net_cash_eur",
        "battery_benefit_eur",
    ):
        assert basis[name] == LEDGER_BASIS_MEASURED, name


def test_unclassified_is_not_one_of_the_kinds_of_number() -> None:
    """It is the *absence* of a basis, not a seventh kind.

    Admitting it to the vocabulary would make "we did not classify this" a valid
    answer to "what kind of number is this", which is the opposite of what the
    vocabulary is for.
    """
    assert LEDGER_BASIS_UNCLASSIFIED not in LEDGER_BASES


@pytest.fixture
async def economic_entity(hass, setup_integration, source_entities, frank):
    """Return the Economic Value attributes as an operator would read them."""
    from custom_components.alpha_ems_manager.sensor import (
        _economic_value_attributes,
    )

    from .test_beta33_campaign_wiring import planning_coordinator

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    return _economic_value_attributes(coordinator)


async def test_every_euro_figure_on_the_entity_carries_a_basis(
    economic_entity,
) -> None:
    """**The finding this closes**: the caveat was reachable only by downloading
    diagnostics.

    Every attribute whose name ends in euros gets a word, and the words come from the
    published vocabulary or from the explicit unclassified marker -- never from
    nothing at all.
    """
    basis = economic_entity["figure_basis"]
    euros = [
        name
        for name in economic_entity
        if name.endswith("_eur") or name.endswith("_eur_kwh")
    ]

    assert euros, "the fixture must publish at least one euro attribute"
    assert set(basis) == set(euros)
    assert set(basis.values()) <= set(LEDGER_BASES) | {LEDGER_BASIS_UNCLASSIFIED}


async def test_the_entity_and_the_download_cannot_disagree(economic_entity) -> None:
    """Projected from the same map rather than restated.

    Two hand-maintained copies of a classification is how one figure comes to be
    called cash in one payload and attributed in another -- and the payload a support
    engineer reads would not be the one the operator was looking at.
    """
    published = _basis_map()
    basis = economic_entity["figure_basis"]

    for name, word in basis.items():
        if word == LEDGER_BASIS_UNCLASSIFIED:
            assert name not in published
            assert f"today_accounting.{name}" not in published
            continue
        assert word in (published.get(name), published.get(f"today_accounting.{name}"))


async def test_the_flattened_day_figures_keep_the_basis_of_their_nested_names(
    economic_entity,
) -> None:
    """The five day figures are flattened to the top level here and keyed under
    ``today_accounting.`` in the map.

    Stripping the prefix is a projection, not a reclassification -- so a flattened
    forecast is still labelled a forecast.
    """
    basis = economic_entity["figure_basis"]

    if "remaining_expected_today_eur" in basis:
        assert basis["remaining_expected_today_eur"] == LEDGER_BASIS_FORECAST
    if "in_progress_interval_eur" in basis:
        assert basis["in_progress_interval_eur"] == LEDGER_BASIS_MEASURED

    # **Both lookups, because there are two kinds of key and dropping either one
    # silently unclassifies a figure.** The mutation table caught this: a projection
    # that consults only the prefixed names still resolves the five flattened day
    # figures, so every assertion above passed while every *directly* keyed euro
    # attribute lost its basis. At least one of those exists on any live entity.
    direct = {
        name
        for name, word in basis.items()
        if word != LEDGER_BASIS_UNCLASSIFIED
        and name in _basis_map()
        and f"today_accounting.{name}" not in _basis_map()
    }
    assert direct, (
        "no directly-keyed euro figure carries a basis: the projection is only "
        "resolving the prefixed names, and this test would prove nothing"
    )


async def test_a_euro_figure_the_map_does_not_cover_is_reported_not_dropped(
    economic_entity,
) -> None:
    """**The fallback branch, exercised on purpose.**

    Every euro attribute the entity publishes today *is* classified -- ten of them
    were added to the map in beta.42 for exactly that reason -- so on a live payload
    the unclassified branch never fires, and a test that only reads a live payload
    cannot tell whether it exists.

    It has to exist, because the next release will add a euro attribute and nobody
    will remember this map. Omitting it silently would read as "no caveat applies";
    naming it reads as "nobody classified this", which is a question a reader can
    act on. So the branch is reached with a figure the map does not cover.
    """
    from custom_components.alpha_ems_manager.sensor import _figure_basis

    basis = _figure_basis({**economic_entity, "a_future_release_added_this_eur": 1.0})

    assert basis["a_future_release_added_this_eur"] == LEDGER_BASIS_UNCLASSIFIED
    assert set(basis) >= set(economic_entity["figure_basis"])
