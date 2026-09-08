"""The realised value, split into English components that do not overlap.

The ledger already answers two questions well. ``battery_benefit_eur`` says what
operating the battery changed, and ``realised_today_eur`` says where the whole
household position stands. Neither answers the question an owner asks first: *how
much of this came from using my own solar, how much from selling it, and how much
from moving it in time?*

Three worlds and two transitions answer it exactly. World 0 is the household with no
photovoltaics and no battery; World 1 has the array and no battery; World 2 is what
happened. Self-consumption is the first transition's import leg, export revenue is
its export leg, and load shifting is the second transition entire. World 1 appears
once with each sign and cancels, so the three sum to World 0 less World 2 with **no
plug term and no residual**.

The trap the split exists to avoid is production that went into the pack and came out
after dark. It was not consumed when it was made and it was not sold, so it belongs
to neither solar component -- it is the later import it displaced, less the export it
gave up, which is the battery transition and nothing else.

The other half of this file is refusal. Two known conditions bias an individual
component, and where either holds the component is withheld with a named reason
rather than published wrong. Cancellation keeps the *total* sound; it does not make
the components honest, and the components are the entire point.
"""

from __future__ import annotations

import inspect

import pytest

from custom_components.alpha_ems_manager import realized
from custom_components.alpha_ems_manager.const import (
    DECOMPOSITION_UNAVAILABLE_COVERAGE_INCOMPLETE,
    DECOMPOSITION_UNAVAILABLE_NO_EVIDENCE,
    DECOMPOSITION_UNAVAILABLE_SELL_PRICE_MISSING,
    LEDGER_BASIS_MEASURED,
    LEDGER_BASIS_UNCLASSIFIED,
)
from custom_components.alpha_ems_manager.realized import (
    _basis_map,
    day_accounting,
    realized_window,
)

#: A clean two-interval day. The first imports under a load the array cannot cover;
#: the second spills production the house does not want. Both priced on both legs, so
#: every gate below is satisfied and the identity has to hold exactly.
CLEAN = {
    "grid_import_kwh": [1.0, 0.0],
    "grid_export_kwh": [0.0, 0.5],
    "import_price_eur_kwh": [0.40, 0.30],
    "export_price_eur_kwh": [0.10, 0.08],
    "load_kwh": [2.0, 0.5],
    "production_kwh": [0.5, 2.0],
}


def _window(**overrides):
    """Return a window over ``CLEAN``, with any series replaced."""
    return realized_window(**{**CLEAN, **overrides})


# ===========================================================================
# the identity
# ===========================================================================


def test_the_three_components_sum_exactly_to_the_published_total() -> None:
    """``==``, because ``>=`` passes on a double count.

    The total is derived from its own two ends -- gross load value less actual net
    cash -- and never by adding the three components up, so this equality is a real
    check on the decomposition rather than a restatement of how it was built.
    """
    window = _window()

    total = window.realized_energy_value_eur
    assert total is not None
    assert window.decomposition_unavailable_reason is None

    parts = (
        window.realized_self_consumption_value_eur,
        window.realized_export_value_eur,
        window.realized_load_shifting_value_eur,
    )
    assert None not in parts
    assert sum(parts) == pytest.approx(total, abs=1e-4)


def test_the_components_are_the_three_world_transitions_by_hand() -> None:
    """Every figure checked against arithmetic done outside the module.

    Interval 0: load 2.0 against 0.5 produced, so 0.5 was self-consumed at 0.40 and
    1.5 would have been imported. Interval 1: load 0.5 against 2.0 produced, so 0.5
    was self-consumed at 0.30 and 1.5 would have spilled at 0.08.
    """
    window = _window()

    # SUM p*min(L, PV) = 0.5*0.40 + 0.5*0.30
    assert window.realized_self_consumption_value_eur == pytest.approx(0.35, abs=1e-4)
    # SUM s*max(0, PV-L) = 1.5*0.08
    assert window.realized_export_value_eur == pytest.approx(0.12, abs=1e-4)
    # (SUM p*N - SUM s*X) - (SUM p*I - SUM s*E)
    #   = (1.5*0.40 - 1.5*0.08) - (1.0*0.40 - 0.5*0.08)
    assert window.realized_load_shifting_value_eur == pytest.approx(0.12, abs=1e-4)
    # SUM p*L - net cash = (2.0*0.40 + 0.5*0.30) - (0.40 - 0.04)
    assert window.realized_energy_value_eur == pytest.approx(0.59, abs=1e-4)


def test_stored_solar_is_credited_once_and_only_in_the_shifting_term() -> None:
    """**The named trap, and the reason the solar legs are counterfactual.**

    One interval produces 2.0 against a load of 0.5 and exports nothing: the surplus
    went into the pack. Self-consumption credits only the 0.5 the house used at the
    time; the export component credits what a bare array *would* have sold, because
    that is the revenue this household gave up; and the value of storing it appears
    exactly once, inside the battery transition, with the opposite sign.
    """
    stored = _window(
        grid_import_kwh=[1.0, 0.0],
        grid_export_kwh=[0.0, 0.0],
        load_kwh=[2.0, 0.5],
        production_kwh=[0.5, 2.0],
    )

    # Only what the house actually used as it was produced.
    assert stored.realized_self_consumption_value_eur == pytest.approx(0.35, abs=1e-4)
    # A bare array would have sold the surplus; this household stored it instead.
    assert stored.realized_export_value_eur == pytest.approx(0.12, abs=1e-4)
    # **Both sides of storing it, and only here.** The pack covered 0.5 kWh of the
    # first interval at 0.40 and gave up 1.5 kWh of export at 0.08 in the second:
    # 0.20 - 0.12 = 0.08. Neither half is smeared into a solar leg to hide it.
    assert stored.realized_load_shifting_value_eur == pytest.approx(0.08, abs=1e-4)
    assert stored.realized_energy_value_eur == pytest.approx(0.55, abs=1e-4)
    assert pytest.approx(0.55, abs=1e-4) == 0.35 + 0.12 + 0.08


def test_the_export_component_is_the_counterfactual_and_not_the_meter() -> None:
    """Measured meter export includes battery-to-grid, which is the shifting term.

    Using it here would count one sale twice: once as solar the array exported and
    once as energy the battery moved. The two figures differ on this fixture, which
    is the whole demonstration.
    """
    window = _window()

    assert window.realized_export_revenue_eur == pytest.approx(0.04, abs=1e-4)
    assert window.realized_export_value_eur == pytest.approx(0.12, abs=1e-4)
    assert window.realized_export_value_eur != window.realized_export_revenue_eur


def test_the_shifting_component_is_the_existing_sealed_comparator() -> None:
    """Identically ``battery_benefit_eur``, and deliberately not a new derivation.

    Two derivations of one number is how two published figures come to disagree, and
    this one is the numerator of the investment return.
    """
    window = _window()

    assert (
        window.realized_load_shifting_value_eur == window.realized_battery_benefit_eur
    )


def test_no_component_is_ever_a_plug() -> None:
    """Structural: nothing is back-solved from the total to make it balance.

    A component defined as the total less the others would balance by construction
    and could be arbitrarily wrong without any test noticing.
    """
    for name in (
        "realized_self_consumption_value_eur",
        "realized_export_value_eur",
        "realized_load_shifting_value_eur",
    ):
        source = inspect.getsource(getattr(realized.RealizedWindow, name).fget)
        assert "realized_energy_value_eur" not in source, name


def test_a_negative_component_is_published_as_measured() -> None:
    """A component can legitimately be negative, and none is clamped at zero.

    A battery that bought at the wrong hour really did destroy value that day, and a
    figure floored at zero would report a loss as a break-even.
    """
    window = _window(
        grid_import_kwh=[2.0, 0.0],
        grid_export_kwh=[0.0, 0.0],
        load_kwh=[0.5, 0.5],
        production_kwh=[0.0, 0.0],
    )

    shifting = window.realized_load_shifting_value_eur
    assert shifting is not None
    assert shifting < 0.0


# ===========================================================================
# refusal
# ===========================================================================


def test_a_missing_sell_price_withholds_the_two_components_it_biases() -> None:
    """**The known ledger defect, and the new figures decline to inherit it.**

    ``no_battery_export`` accumulates on every counterfactual interval while
    ``no_battery_revenue`` accumulates only where a sell price exists, so an interval
    that spilled production without one contributes kilowatt-hours at zero revenue.
    The export component is understated by exactly that and the shifting component
    overstated by exactly that -- errors that cancel in the total and are wrong
    individually, which is not a standard these figures accept.
    """
    holed = _window(export_price_eur_kwh=[0.10, None], grid_export_kwh=[0.0, None])

    assert holed.counterfactual_intervals_missing_sell_price == 1
    assert holed.realized_export_value_eur is None
    assert holed.realized_load_shifting_value_eur is None
    assert holed.realized_energy_value_eur is None
    assert (
        holed.decomposition_unavailable_reason
        == DECOMPOSITION_UNAVAILABLE_SELL_PRICE_MISSING
    )


def test_self_consumption_survives_a_missing_sell_price() -> None:
    """A precise split, not a convenience.

    Its two inputs are the gross load value and the counterfactual import cost. Both
    are priced on the *import* leg alone, so the export hole cannot reach them and
    withholding this figure too would refuse an answer that is provably sound.
    """
    holed = _window(export_price_eur_kwh=[0.10, None], grid_export_kwh=[0.0, None])

    assert holed.realized_self_consumption_value_eur == pytest.approx(0.35, abs=1e-4)


def test_mismatched_interval_coverage_withholds_the_shifting_component() -> None:
    """Actual cash over one interval set against a counterfactual over another.

    ``net_cash_flow_eur`` accumulates on every priced interval; the counterfactual
    legs need a load and a production reading as well. Where the two sets differ the
    comparison is not like for like, and the figures that span both are withheld.
    """
    partial = _window(load_kwh=[2.0, None], production_kwh=[0.5, None])

    assert partial.counterfactual_intervals_priced == 1
    assert partial.intervals_priced == 2
    assert partial.realized_load_shifting_value_eur is None
    assert partial.realized_energy_value_eur is None
    assert (
        partial.decomposition_unavailable_reason
        == DECOMPOSITION_UNAVAILABLE_COVERAGE_INCOMPLETE
    )
    # The two solar legs are internally consistent over the intervals they cover.
    assert partial.realized_export_value_eur is not None
    assert partial.realized_self_consumption_value_eur is not None


def test_no_counterfactual_evidence_is_its_own_reason() -> None:
    """The most fundamental refusal, and it is reported first.

    Without a load and a production series there is no counterfactual at all, which is
    a different thing from having one with a hole in it.
    """
    blind = realized_window(
        grid_import_kwh=[1.0],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[0.40],
        export_price_eur_kwh=[0.10],
    )

    assert blind.realized_self_consumption_value_eur is None
    assert blind.realized_export_value_eur is None
    assert blind.realized_energy_value_eur is None
    assert (
        blind.decomposition_unavailable_reason == DECOMPOSITION_UNAVAILABLE_NO_EVIDENCE
    )


def test_an_absent_component_is_none_and_takes_the_total_with_it() -> None:
    """Never zero, and never a smaller total.

    A total missing one of its terms is a different number wearing the same name.
    """
    holed = _window(export_price_eur_kwh=[0.10, None], grid_export_kwh=[0.0, None])

    assert holed.realized_energy_value_eur is None
    assert holed.realized_energy_value_eur != 0.0


def test_the_gates_do_not_move_a_single_existing_figure() -> None:
    """**Byte-identical, including where the known defect biases them.**

    The decomposition holds itself to a stricter standard than the ledger it sits
    beside. That asymmetry is deliberate: repairing ``no_battery_export_revenue_eur``
    changes a write-once sealed figure and the recovery percentage built on it, which
    needs its own release and a migration. What beta.53 adds is the counter that lets
    the size of the defect be measured first.
    """
    holed = _window(export_price_eur_kwh=[0.10, None], grid_export_kwh=[0.0, None])

    # Interval 1 spills 1.5 kWh and has no sell price, so the ledger prices it at
    # nothing -- exactly the bias -- and beta.53 leaves that untouched.
    assert holed.realized_no_battery_export_kwh == pytest.approx(1.5, abs=1e-3)
    assert holed.realized_no_battery_export_revenue_eur == pytest.approx(0.0, abs=1e-4)
    assert holed.realized_no_battery_net_cash_eur == pytest.approx(0.6, abs=1e-4)
    assert holed.realized_battery_benefit_eur == pytest.approx(0.2, abs=1e-4)
    assert holed.realized_net_value_eur is not None


# ===========================================================================
# publication
# ===========================================================================


def test_every_new_figure_carries_a_basis_from_the_vocabulary() -> None:
    """**Closes a real hole rather than restating a guard that exists.**

    ``_figure_basis`` publishes ``unclassified`` for any euro attribute the map does
    not name, and the beta.42 suite tolerates that by design -- it was written
    expecting the next release to add a euro attribute and forget the map. So nothing
    today would catch four unclassified euro figures on a MONETARY entity. This does.
    """
    basis = _basis_map()

    for name in (
        "self_consumption_value_eur",
        "export_value_eur",
        "load_shifting_value_eur",
        "energy_value_eur",
    ):
        key = f"today_accounting.realised_{name}"
        assert key in basis, key
        assert basis[key] == LEDGER_BASIS_MEASURED
        assert basis[key] != LEDGER_BASIS_UNCLASSIFIED


def test_the_ledger_publishes_the_components_and_its_coverage() -> None:
    """The four figures, the reason and both coverage counters, together.

    A refusal a reader cannot explain is worse than a figure they can check, so the
    counts that decide the gates are published beside the gates' verdict.
    """
    block = _window().ledger()["ledger"]

    assert block["self_consumption_value_eur"] == pytest.approx(0.35, abs=1e-4)
    assert block["export_value_eur"] == pytest.approx(0.12, abs=1e-4)
    assert block["load_shifting_value_eur"] == pytest.approx(0.12, abs=1e-4)
    assert block["energy_value_eur"] == pytest.approx(0.59, abs=1e-4)
    assert block["decomposition_unavailable_reason"] is None
    assert block["counterfactual_intervals_missing_sell_price"] == 0
    assert block["counterfactual_intervals_priced"] == 2


def test_the_day_block_carries_the_decomposition_beside_the_position() -> None:
    """Published where the entity can reach it, under the ``realised_`` names.

    ``realised_today_eur`` keeps its exact meaning and its four-term reconciliation;
    these six sit beside it and never replace it. One is measured cash against a
    stated counterfactual and the other is a position carrying a planner valuation --
    two legitimate questions that must not be mixed.
    """
    block = day_accounting(
        # Both ends of the position valued, so the headline figure beside the
        # decomposition is a number rather than a refusal.
        realised=_window(
            opening_inventory_value_eur=0.0, closing_inventory_value_eur=0.0
        ),
        in_progress_eur=0.0,
        in_progress_index=None,
        in_progress_coverage=None,
        remaining_expected_eur=0.0,
        forecast_revaluation_eur=0.0,
    ).as_dict()

    assert block["realised_self_consumption_value_eur"] == pytest.approx(0.35, abs=1e-4)
    assert block["realised_export_value_eur"] == pytest.approx(0.12, abs=1e-4)
    assert block["realised_load_shifting_value_eur"] == pytest.approx(0.12, abs=1e-4)
    assert block["realised_energy_value_eur"] == pytest.approx(0.59, abs=1e-4)
    assert block["decomposition_unavailable_reason"] is None
    assert block["counterfactual_intervals_missing_sell_price"] == 0
    # The headline is untouched and still its own derivation.
    assert block["realised_today_eur"] is not None


def test_a_day_with_no_realised_window_publishes_the_refusal_not_a_zero() -> None:
    """Absent evidence is absent, on the same terms as every other figure here."""
    block = day_accounting(
        realised=None,
        in_progress_eur=None,
        in_progress_index=None,
        in_progress_coverage=None,
        remaining_expected_eur=None,
        forecast_revaluation_eur=None,
        unavailable_reason="no_day_record",
    ).as_dict()

    assert block["realised_energy_value_eur"] is None
    assert block["realised_self_consumption_value_eur"] is None
    assert (
        block["decomposition_unavailable_reason"]
        == DECOMPOSITION_UNAVAILABLE_NO_EVIDENCE
    )
