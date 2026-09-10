"""beta.56: the meter's own export had no entity, so a tile read the counterfactual.

``realised_export_value_eur`` answers "what would a household with this array and
**no battery** have sold". It has to be a counterfactual: measured export includes
energy the battery sent to the grid, and that sale already lives inside the
load-shifting term, so pricing the meter there would count one sale twice.

But the question an owner asks looking at a dashboard is the other one -- how much
went out of my meter, and what did it fetch -- and through beta.55 no entity
published it. The figures existed: ``realized_grid_export_kwh`` and
``realized_export_revenue_eur`` have been summed in ``realized_window`` since
beta.31 and reached the diagnostics download and nothing else. So the only export
figure a card could bind to was the counterfactual, and it was the smaller number.

This suite pins the pair that closes that, and -- at least as importantly -- pins
that it did **not** close it by redefining the counterfactual or by joining the
decomposition identity.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    LEDGER_BASIS_MEASURED,
    METERED_EXPORT_RULE,
)
from custom_components.alpha_ems_manager.realized import (
    _basis_map,
    day_accounting,
    realized_window,
)


def _window(**overrides):
    """Solve one four-interval day through the production path.

    Deliberately a shape with both legs, a battery and a sale, so the metered pair
    and the counterfactual are genuinely different numbers rather than
    coincidentally equal ones.
    """
    kwargs = {
        "grid_import_kwh": [1.0, 0.0, 0.0, 0.5],
        "grid_export_kwh": [0.0, 2.0, 1.0, 0.0],
        "import_price_eur_kwh": [0.30, 0.30, 0.30, 0.30],
        "export_price_eur_kwh": [0.10, 0.10, 0.20, 0.10],
        "load_kwh": [1.0, 0.5, 0.5, 0.5],
        "production_kwh": [0.0, 2.0, 1.5, 0.0],
        "battery_charge_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_discharge_kwh": [0.0, 0.5, 0.0, 0.0],
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
    }
    kwargs.update(overrides)
    return realized_window(**kwargs)


def _accounting(window):
    return day_accounting(
        realised=window,
        in_progress_eur=0.0,
        in_progress_index=4,
        in_progress_coverage=1.0,
        remaining_expected_eur=0.0,
        forecast_revaluation_eur=0.0,
    )


# ===========================================================================
# 1. the pair is the meter, and it is the figure the window already summed
# ===========================================================================


def test_the_metered_pair_is_projected_not_recomputed() -> None:
    """**One derivation.** The window applied the pricing gate; nothing repeats it.

    Two derivations of one number is how two published figures come to disagree,
    and this module refuses it everywhere else. The accounting block must carry
    the window's own totals rather than its own arithmetic over the same inputs.

    *Mutation: recompute either field inside ``day_accounting`` and this fails as
    soon as the two implementations round differently.*
    """
    window = _window()
    accounting = _accounting(window)

    assert accounting.realised_metered_export_kwh == pytest.approx(
        window.realized_grid_export_kwh
    )
    assert accounting.realised_metered_export_revenue_eur == pytest.approx(
        window.realized_export_revenue_eur
    )
    # And it really is the meter: 2.0 + 1.0 exported, at 0.10 and 0.20.
    assert accounting.realised_metered_export_kwh == pytest.approx(3.0)
    assert accounting.realised_metered_export_revenue_eur == pytest.approx(0.4)


def test_the_metered_volume_carries_no_efficiency_factor() -> None:
    """A meter reading is a meter reading. **No round-trip term may touch it.**

    The only efficiency in that loop belongs to ``battery_to_grid_kwh``, which is
    an *attributed* split -- a different figure with a different basis. If a
    conversion factor ever leaked onto this one, the published volume would stop
    matching the P1 meter the owner can read on their own wall, which is the whole
    claim being made for it.

    *Mutation: scale the export leg by ``discharge_efficiency`` and this fails.*
    """
    lossless = _accounting(_window(charge_efficiency=1.0, discharge_efficiency=1.0))
    lossy = _accounting(_window(charge_efficiency=0.80, discharge_efficiency=0.80))

    assert lossless.realised_metered_export_kwh == pytest.approx(
        lossy.realised_metered_export_kwh
    )
    assert lossless.realised_metered_export_revenue_eur == pytest.approx(
        lossy.realised_metered_export_revenue_eur
    )


def test_battery_export_is_in_the_meter_and_not_in_the_counterfactual() -> None:
    """**The whole reason two figures exist, in one assertion.**

    The meter cannot tell where a kilowatt-hour came from, and this pair is the
    meter's. The counterfactual can and must: a bare array would never have sold
    what the battery discharged, and crediting it there would pay twice for one
    sale. So a discharge that reaches the grid moves one figure and not the other.
    """
    # An interval exporting more than a bare array would have spilled: production
    # 1.5 against load 0.5 spills 1.0, and 1.6 crossed the meter.
    window = _window(
        grid_export_kwh=[0.0, 2.0, 1.6, 0.0],
        battery_discharge_kwh=[0.0, 0.5, 0.6, 0.0],
    )
    accounting = _accounting(window)

    assert accounting.realised_metered_export_kwh == pytest.approx(3.6)
    assert accounting.realised_export_value_eur == pytest.approx(
        window.realized_no_battery_export_revenue_eur
    )
    assert (
        accounting.realised_export_value_eur
        < accounting.realised_metered_export_revenue_eur
    )


# ===========================================================================
# 2. and it changed nothing that already existed
# ===========================================================================


def test_the_counterfactual_export_component_is_untouched() -> None:
    """``realised_export_value_eur`` keeps its exact meaning and its exact value.

    Still ``realized_no_battery_export_revenue_eur`` behind its two refusal gates,
    with no arithmetic of its own. beta.56 adds a neighbour; it does not redefine
    the neighbourhood.
    """
    window = _window()
    accounting = _accounting(window)

    assert accounting.realised_export_value_eur == pytest.approx(
        window.realized_no_battery_export_revenue_eur
    )
    # The two answer different questions and must not coincide on this shape.
    assert accounting.realised_export_value_eur != pytest.approx(
        accounting.realised_metered_export_revenue_eur
    )


def test_the_metered_pair_is_outside_the_decomposition_identity() -> None:
    """**The three components still sum to the total, with no plug term.**

    Adding the metered revenue into ``realised_energy_value_eur`` would double the
    export leg: the counterfactual sale and the metered sale are the same physical
    electrons under two conventions. The identity must be exactly what it was.

    *Mutation: add ``realised_metered_export_revenue_eur`` to any of the three
    components, or into the total, and this fails.*
    """
    accounting = _accounting(_window())

    parts = (
        accounting.realised_self_consumption_value_eur,
        accounting.realised_export_value_eur,
        accounting.realised_load_shifting_value_eur,
    )
    assert all(part is not None for part in parts)
    assert accounting.realised_energy_value_eur == pytest.approx(sum(parts), abs=1e-6)


def test_a_missing_sell_price_withholds_the_counterfactual_and_not_the_meter() -> None:
    """Two different availability questions, and they must not be collapsed.

    The counterfactual is withheld when *any* interval spilled production with no
    sell price, because that is precisely the amount by which it is understated.
    The metered pair needs no such gate: the window skips an interval it cannot
    price, so the pair is understated-but-sound rather than biased, and the skip
    count is published beside it.
    """
    # Index 1 spills 1.5 kWh in the counterfactual -- production 2.0 against a
    # 0.5 load -- with no sell price to value it at, while the meter itself
    # exported nothing there. So the interval stays *priced* on its import leg and
    # the counterfactual gate is the thing that fires, which is the case this
    # separation exists for. (An interval whose own export cannot be priced is a
    # different case, and it leaves the window entirely -- see the test below.)
    window = _window(
        grid_import_kwh=[1.0, 0.2, 0.0, 0.5],
        grid_export_kwh=[0.0, None, 1.0, 0.0],
        export_price_eur_kwh=[0.10, None, 0.20, 0.10],
    )
    accounting = _accounting(window)

    assert window.counterfactual_intervals_missing_sell_price >= 1
    assert accounting.realised_export_value_eur is None
    assert accounting.decomposition_unavailable_reason is not None
    # **And the meter is published anyway.** It has no counterfactual to be
    # understated against: 1.0 kWh crossed the meter at a price that was recorded.
    assert accounting.realised_metered_export_kwh == pytest.approx(1.0)
    assert accounting.realised_metered_export_revenue_eur == pytest.approx(0.2)


def test_an_interval_that_exported_with_no_price_is_skipped_not_zeroed() -> None:
    """Zero revenue and *unknown* revenue are different answers.

    Valuing an unpriced sale at zero would understate the pair while still
    reporting full coverage -- a silent bias, which is the failure this project
    refuses everywhere else. The interval leaves the window and the count says so.
    """
    priced = _window()
    holed = _window(export_price_eur_kwh=[0.10, 0.10, None, 0.10])

    assert holed.intervals_skipped == priced.intervals_skipped + 1
    # That interval's 1.0 kWh is absent from the volume, not priced at zero.
    assert holed.realized_grid_export_kwh == pytest.approx(2.0)


# ===========================================================================
# 3. the basis word, which is the half a MONETARY entity cannot do without
# ===========================================================================


def test_both_names_and_both_spellings_carry_the_measured_basis() -> None:
    """**Without this the euro half publishes ``unclassified`` and nothing fails.**

    ``sensor._figure_basis`` maps every attribute ending ``_eur`` through
    ``_basis_map``, stripping the ``today_accounting.`` prefix because the block is
    flattened to the top level. A euro figure with no entry lands on a ``MONETARY``
    entity marked unclassified -- silently, and beside a dozen figures that are
    classified. beta.53 mapped both spellings for this reason; so does beta.56.
    """
    basis = _basis_map()
    for name in (
        "realised_metered_export_kwh",
        "realised_metered_export_revenue_eur",
        "today_accounting.realised_metered_export_kwh",
        "today_accounting.realised_metered_export_revenue_eur",
    ):
        assert basis.get(name) == LEDGER_BASIS_MEASURED, name


def test_the_published_block_states_the_reconstructed_price_caveat() -> None:
    """The volume is measured; the price may be modelled. **Both, in the payload.**

    ``export_revenue_eur`` is mapped ``measured`` while
    ``current_export_price_eur_kwh`` is mapped ``estimated`` -- a tension older
    than beta.56 that this pair makes user-visible. A tile reading "earned" over a
    reconstructed price is a claim, so the caveat travels with the figure instead
    of living in a docstring.
    """
    published = _accounting(_window()).as_dict()

    assert published["realised_metered_export_kwh"] == pytest.approx(3.0)
    assert published["realised_metered_export_revenue_eur"] == pytest.approx(0.4)
    rule = published["metered_export_rule"]
    assert rule == METERED_EXPORT_RULE
    assert "reconstructed" in rule
    assert "measured" in rule
    # And it names what it is *not*, because that is the confusion being closed.
    assert "realised_export_value_eur" in rule
    assert "counterfactual" in rule


def test_the_pair_is_absent_rather_than_zero_without_a_window() -> None:
    """No evidence is not no export. ``None`` throughout, on the beta.53 terms."""
    accounting = day_accounting(
        realised=None,
        in_progress_eur=None,
        in_progress_index=None,
        in_progress_coverage=None,
        remaining_expected_eur=None,
        forecast_revaluation_eur=None,
    )

    assert accounting.realised_metered_export_kwh is None
    assert accounting.realised_metered_export_revenue_eur is None
