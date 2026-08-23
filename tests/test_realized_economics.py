"""What actually happened, and the proof it can never change what happens next.

Two separate claims, and the second matters more than the first.

**The arithmetic.** Realised cost is measured flows multiplied by the prices
recorded for the same intervals. Every figure here is checked by hand, because a
figure nobody can check is worse than no figure.

**The isolation.** A realised number must never become an optimizer input. The
temptation is a cost basis -- "never sell below what this energy cost me" -- and it
is economically *wrong*: energy that cost 0.20 is a sunk cost, and if selling it at
0.18 makes room for production that would otherwise be curtailed, selling is
correct. A cost-basis rule would forbid the right decision. So the optimizer is
given nothing from here, and that is asserted structurally rather than trusted.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from custom_components.alpha_ems_manager import realized as realized_module
from custom_components.alpha_ems_manager.realized import (
    BATTERY_BASIS_STATE_OF_CHARGE,
    BATTERY_BASIS_UNAVAILABLE,
    PROVENANCE_UNKNOWN,
    realized_window,
    soc_series_to_energy,
)

ETA = 0.9487


# ===========================================================================
# A. the arithmetic, checked by hand
# ===========================================================================


def test_cost_and_revenue_are_flows_times_their_own_prices() -> None:
    """One import at 0.30 and one export at 0.10, and nothing else.

    Deliberately trivial: if this is wrong nothing else in the module can be
    right, and the expected values are arithmetic a reader can do in their head.
    """
    window = realized_window(
        grid_import_kwh=[1.0, 0.0],
        grid_export_kwh=[0.0, 2.0],
        import_price_eur_kwh=[0.30, 0.30],
        export_price_eur_kwh=[0.10, 0.10],
    )

    assert window.realized_grid_import_kwh == pytest.approx(1.0)
    assert window.realized_grid_export_kwh == pytest.approx(2.0)
    assert window.realized_import_cost_eur == pytest.approx(0.30)
    assert window.realized_export_revenue_eur == pytest.approx(0.20)
    # Positive means money left the household -- the same sign convention the
    # optimizer uses for cost, so the two can be read side by side.
    assert window.realized_net_cash_flow_eur == pytest.approx(0.10)
    assert window.intervals_priced == 2
    assert window.intervals_skipped == 0


def test_each_interval_is_valued_at_its_own_price() -> None:
    """Not an average. A day's prices vary by a factor of four."""
    window = realized_window(
        grid_import_kwh=[1.0, 1.0, 1.0],
        grid_export_kwh=[0.0, 0.0, 0.0],
        import_price_eur_kwh=[0.10, 0.20, 0.40],
        export_price_eur_kwh=[0.05, 0.05, 0.05],
    )

    assert window.realized_import_cost_eur == pytest.approx(0.70)


def test_an_unpriced_flow_is_skipped_and_never_valued_at_zero() -> None:
    """**Unknown is not free.** The rule Phase 6 applies to prices, applied here.

    Five kilowatt-hours were imported and there is no price for them. Valuing
    them at zero would report a free day; counting the energy but not the cost
    would report an impossible one. The interval is excluded and the exclusion is
    published.
    """
    window = realized_window(
        grid_import_kwh=[5.0],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[None],
        export_price_eur_kwh=[0.10],
    )

    assert window.intervals_priced == 0
    assert window.intervals_skipped == 1
    assert window.realized_grid_import_kwh == pytest.approx(0.0)
    assert window.realized_import_cost_eur == pytest.approx(0.0)


def test_an_interval_with_no_flow_at_all_is_skipped_not_counted() -> None:
    """A quarter the recorder missed is absent, not a quarter of zero flow."""
    window = realized_window(
        grid_import_kwh=[None, 1.0],
        grid_export_kwh=[None, 0.0],
        import_price_eur_kwh=[0.30, 0.30],
        export_price_eur_kwh=[0.10, 0.10],
    )

    assert window.intervals_priced == 1
    assert window.intervals_skipped == 1


def test_battery_movement_is_differenced_from_the_recorded_state() -> None:
    """Stored energy up means AC in, and the efficiency applies once.

    50 % to 60 % of 22 kWh is 2.2 kWh of stored energy, which took 2.2/eta of AC
    energy to put there. 60 % back to 55 % is 1.1 kWh out, which delivered
    1.1*eta. The same single-crossing rule Phase 3 uses.
    """
    stored = soc_series_to_energy([50.0, 60.0, 55.0], capacity_kwh=22.0)
    window = realized_window(
        grid_import_kwh=[1.0, 1.0, 1.0],
        grid_export_kwh=[0.0, 0.0, 0.0],
        import_price_eur_kwh=[0.30, 0.30, 0.30],
        export_price_eur_kwh=[0.10, 0.10, 0.10],
        stored_energy_kwh=stored,
        capacity_kwh=22.0,
        charge_efficiency=ETA,
        discharge_efficiency=ETA,
    )

    assert window.battery_basis == BATTERY_BASIS_STATE_OF_CHARGE
    assert window.realized_battery_charge_kwh == pytest.approx(2.2 / ETA, abs=1e-3)
    assert window.realized_battery_discharge_kwh == pytest.approx(1.1 * ETA, abs=1e-3)


def test_a_gap_in_the_recorded_state_ends_a_span_rather_than_being_bridged() -> None:
    """Bridging would credit the battery with whatever happened unobserved."""
    stored = soc_series_to_energy([50.0, None, 90.0], capacity_kwh=22.0)
    window = realized_window(
        grid_import_kwh=[1.0, 1.0, 1.0],
        grid_export_kwh=[0.0, 0.0, 0.0],
        import_price_eur_kwh=[0.30] * 3,
        export_price_eur_kwh=[0.10] * 3,
        stored_energy_kwh=stored,
        capacity_kwh=22.0,
        charge_efficiency=ETA,
        discharge_efficiency=ETA,
    )

    # The 50->90 jump spans the gap, so nothing is attributed to the battery --
    # and the honest answer is "not known", not a confident zero.
    assert window.realized_battery_charge_kwh is None
    assert window.realized_battery_discharge_kwh is None
    assert window.battery_basis == BATTERY_BASIS_UNAVAILABLE


def test_without_the_efficiencies_no_battery_figure_is_published() -> None:
    """A DC delta reported as AC energy would overstate what the house saw."""
    window = realized_window(
        grid_import_kwh=[1.0],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[0.30],
        export_price_eur_kwh=[0.10],
        stored_energy_kwh=(11.0, 12.0),
        capacity_kwh=22.0,
        charge_efficiency=None,
        discharge_efficiency=None,
    )

    assert window.realized_battery_charge_kwh is None
    assert window.battery_basis == BATTERY_BASIS_UNAVAILABLE


def test_load_avoidance_is_measured_rather_than_attributed() -> None:
    """What the meter would have shown, less what it did show.

    Load 1.0, production 0.0, so without a battery the meter would have shown
    1.0 kWh of import. It showed 0.2, so 0.8 kWh was avoided, worth 0.8 * 0.40.
    Every term measured; no assumption about where the energy in the pack came
    from, which is what makes this publishable while trade profit is not.
    """
    window = realized_window(
        grid_import_kwh=[0.2],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[0.40],
        export_price_eur_kwh=[0.10],
        load_kwh=[1.0],
        production_kwh=[0.0],
    )

    assert window.realized_load_avoidance_kwh == pytest.approx(0.8)
    assert window.realized_load_avoidance_value_eur == pytest.approx(0.32)


def test_production_covering_load_is_not_counted_as_battery_avoidance() -> None:
    """The sun supplying the house is not the battery saving money."""
    window = realized_window(
        grid_import_kwh=[0.0],
        grid_export_kwh=[0.5],
        import_price_eur_kwh=[0.40],
        export_price_eur_kwh=[0.10],
        load_kwh=[1.0],
        production_kwh=[1.5],
    )

    assert window.realized_load_avoidance_kwh == pytest.approx(0.0)


def test_load_avoidance_is_absent_when_the_evidence_is() -> None:
    """No load or production series means no claim, not a zero."""
    window = realized_window(
        grid_import_kwh=[0.2],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[0.40],
        export_price_eur_kwh=[0.10],
    )

    assert window.realized_load_avoidance_kwh is None
    assert window.realized_load_avoidance_value_eur is None


# ===========================================================================
# B. what is deliberately not claimed
# ===========================================================================


def test_trade_profit_is_published_as_absent_and_says_why() -> None:
    """**A number that depends on an arbitrary convention is not published.**

    Attributing a discharged kilowatt-hour to a particular earlier charge needs an
    inventory convention -- weighted average, first-in-first-out -- and a battery
    has no physical ordering that makes either true rather than conventional.
    Beside figures that are measured, such a number would borrow a precision it
    has not got.
    """
    payload = realized_window(
        grid_import_kwh=[1.0],
        grid_export_kwh=[1.0],
        import_price_eur_kwh=[0.30],
        export_price_eur_kwh=[0.10],
    ).as_dict()

    assert payload["trade_profit_eur"] is None
    assert "inventory convention" in payload["rule"]


def test_opening_inventory_is_reported_and_never_priced() -> None:
    """Energy that predates the window has unknown provenance, and says so.

    Assigning it a purchase price would be inventing history -- and would be the
    first step towards a cost basis the optimizer must never see.
    """
    window = realized_window(
        grid_import_kwh=[1.0],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[0.30],
        export_price_eur_kwh=[0.10],
        stored_energy_kwh=(13.2, 13.2),
        capacity_kwh=22.0,
        charge_efficiency=ETA,
        discharge_efficiency=ETA,
    )

    assert window.opening_inventory_kwh == pytest.approx(13.2)
    assert window.opening_inventory_provenance == PROVENANCE_UNKNOWN
    payload = window.as_dict()
    assert payload["opening_inventory_provenance"] == PROVENANCE_UNKNOWN
    # No price, no cost, no basis anywhere near it.
    assert not any(
        "opening" in key and ("cost" in key or "eur" in key) for key in payload
    )


def test_the_payload_says_these_are_realised_not_forecast() -> None:
    """The one confusion this module exists to prevent."""
    payload = realized_window(
        grid_import_kwh=[1.0],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[0.30],
        export_price_eur_kwh=[0.10],
    ).as_dict()

    assert "realised" in payload["rule"] or "realized" in payload["rule"]
    assert "never forecasts" in payload["rule"]


# ===========================================================================
# C. the isolation -- structural, not trusted
# ===========================================================================


def test_the_module_is_pure() -> None:
    """No Home Assistant, no socket, no clock. Exercisable against hand-written
    numbers, which is why every figure above could be checked by hand.
    """
    source = pathlib.Path(realized_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "homeassistant" not in imported
    assert not {"aiohttp", "socket", "requests"} & imported
    for forbidden in ("now(", "utcnow", "datetime.now", "time()"):
        assert forbidden not in source


def test_no_decision_module_imports_the_realised_layer() -> None:
    """**The sunk-cost guard.**

    The optimizer, the reserve, the policy, the safety gate and the battery model
    must not be able to *import* a realised figure. Not "must not use one" -- must
    not be able to, which is a property of the import graph and survives somebody
    later reaching for a convenient number.
    """
    package = pathlib.Path(realized_module.__file__).parent
    for name in (
        "economic.py",
        "reserve.py",
        "policy.py",
        "safety.py",
        "battery.py",
        "simulation.py",
    ):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "realized" not in node.module, name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "realized" not in alias.name, name


def test_only_the_payload_builder_can_even_name_a_realised_figure() -> None:
    """Inside the optimizer module, exactly one function may see it.

    ``economic_as_dict`` is the reporting boundary and takes the block as an
    argument so it can be published beside the plan. No function that *decides*
    anything may so much as name it -- if the search could read a realised cost,
    the sunk-cost mistake would be one line away.
    """
    package = pathlib.Path(realized_module.__file__).parent
    tree = ast.parse((package / "economic.py").read_text(encoding="utf-8"))
    touching = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            names |= {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
            if any("realized" in name or "realised" in name for name in names):
                touching.add(node.name)

    assert touching == {"economic_as_dict"}, touching


def test_the_realised_layer_reads_no_plan_and_no_price_forecast() -> None:
    """It takes measured series and prices as arguments, and nothing else.

    So it cannot accidentally acquire a forecast and start reporting one as
    realised -- the signature is the guard.
    """
    parameters = set(inspect.signature(realized_window).parameters)

    assert parameters == {
        "grid_import_kwh",
        "grid_export_kwh",
        "import_price_eur_kwh",
        "export_price_eur_kwh",
        "load_kwh",
        "production_kwh",
        "stored_energy_kwh",
        "capacity_kwh",
        "charge_efficiency",
        "discharge_efficiency",
    }


def test_every_published_name_is_marked_realised() -> None:
    """A reader must never have to guess whether a figure is a forecast."""
    payload = realized_window(
        grid_import_kwh=[1.0],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[0.30],
        export_price_eur_kwh=[0.10],
    ).as_dict()

    # The block is nested under "realized" in the report, so the keys inside it
    # are deliberately unprefixed -- but none of them may claim to be expected.
    for key in payload:
        assert not key.startswith("expected")
    for field in (
        "realized_import_cost_eur",
        "realized_export_revenue_eur",
        "realized_grid_import_kwh",
        "realized_grid_export_kwh",
    ):
        assert hasattr(realized_module.RealizedWindow, "__annotations__")
        assert field in realized_module.RealizedWindow.__annotations__
