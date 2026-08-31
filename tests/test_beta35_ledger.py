"""beta.35: what actually happened, priced honestly, and claiming nothing more.

**The ledger answers a question the forecasts cannot** -- *what did this cost, and
where does the position stand now* -- and the whole difficulty is answering it
without inventing provenance. A battery has no physical ordering, so no
measurement can say that the kilowatt-hour just discharged is the one bought at
03:00. Every figure here is therefore labelled with what kind of number it is, and
the labels are published beside the figures rather than argued in a docstring:

``measured``
    integrated from a source reading.
``attributed``
    measured energy split by a stated per-interval rule. A bound, never a claim
    about which electron went where.
``estimated``
    derived from a model constant, such as a conversion efficiency.
``planner_derived``
    from the optimiser's own value function, and an opportunity value from now.
``model_term``
    a hurdle rate or a wear proxy from the objective. **Not money anybody paid**,
    and kept out of every cash total on purpose.

``trade_profit_eur`` stays ``None`` for the reason it always has.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    EXECUTION_STOP_WINDOW_ENDED,
    LEDGER_BASES,
    LEDGER_BASIS_ATTRIBUTED,
    LEDGER_BASIS_ESTIMATED,
    LEDGER_BASIS_MEASURED,
    LEDGER_BASIS_MODEL_TERM,
    LEDGER_BASIS_PLANNER_DERIVED,
)
from custom_components.alpha_ems_manager.realized import realized_window

from .test_beta24_live_charge import LiveSurface

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


PRICES_BUY = [0.10, 0.10, 0.40, 0.40]
PRICES_SELL = [0.05, 0.05, 0.30, 0.30]


def _window(**overrides):
    """Return a small measured window: two cheap charging intervals, two dear ones."""
    fields = {
        "grid_import_kwh": [4.0, 4.0, 0.0, 0.0],
        "grid_export_kwh": [0.0, 0.0, 3.0, 3.0],
        "import_price_eur_kwh": PRICES_BUY,
        "export_price_eur_kwh": PRICES_SELL,
        "load_kwh": [1.0, 1.0, 1.0, 1.0],
        "production_kwh": [0.0, 0.0, 0.0, 0.0],
        "stored_energy_kwh": [5.0, 8.0, 11.0, 7.5],
        "capacity_kwh": 21.6,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "battery_charge_kwh": [None, 3.0, 3.0, 0.0],
        "battery_discharge_kwh": [None, 0.0, 0.0, 3.5],
        "opening_inventory_value_eur": 0.60,
        "closing_inventory_value_eur": 1.40,
        "model_switching_cost_eur": 0.40,
        "model_grid_charge_margin_eur": 0.35,
        "model_throughput_cost_eur": 0.0,
    }
    fields.update(overrides)
    return realized_window(**fields)


# ===========================================================================
# 1. provenance -- every figure says what kind of number it is
# ===========================================================================


def test_every_published_figure_carries_a_basis_from_the_vocabulary() -> None:
    """A reader who cannot tell the kinds apart will add a hurdle to a cash total.

    So the map is published, it covers every figure, and every entry is one of the
    five defined bases -- not free text that could drift into a sixth.
    """
    ledger = _window().ledger()["ledger"]
    basis = ledger["basis"]

    assert set(basis.values()) <= set(LEDGER_BASES)
    for name in ledger:
        if name in {"basis", "rule", "model_terms"}:
            continue
        assert name in basis, name
    for name in ledger["model_terms"]:
        if name in {"is_cash", "rule"}:
            continue
        assert f"model_terms.{name}" in basis, name


def test_the_kinds_are_assigned_to_the_right_figures() -> None:
    """Named individually, because the whole point is that they differ.

    A meter reading is not a split, a split is not a model constant, and none of
    the three is the optimiser's opinion about what the pack is worth.
    """
    basis = _window().ledger()["ledger"]["basis"]

    assert basis["grid_import_kwh"] == LEDGER_BASIS_MEASURED
    assert basis["import_cost_eur"] == LEDGER_BASIS_MEASURED
    # Split from measured flows by a stated rule, and a bound rather than a claim.
    assert basis["grid_charge_kwh"] == LEDGER_BASIS_ATTRIBUTED
    assert basis["pv_charge_kwh"] == LEDGER_BASIS_ATTRIBUTED
    assert basis["battery_to_grid_kwh"] == LEDGER_BASIS_ATTRIBUTED
    # A model constant, and it is the only thing an efficiency can produce.
    assert basis["conversion_loss_kwh"] == LEDGER_BASIS_ESTIMATED
    # The optimiser's value function, which is an opportunity value from now.
    assert basis["closing_inventory_value_eur"] == LEDGER_BASIS_PLANNER_DERIVED
    assert basis["model_terms.switching_cost_eur"] == LEDGER_BASIS_MODEL_TERM


def test_the_model_terms_are_reported_and_never_totalled_as_cash() -> None:
    """**The one confusion that would make the ledger actively misleading.**

    ``minimum_trade_gain_eur`` is a hurdle rate: it exists to stop the optimiser
    taking trades too thin to be worth the wear, and nobody is ever billed for it.
    Adding it to a cash total would be the same category error as pricing sunk
    cost -- a number from the objective presented as a number from a bank.

    *Mutation: fold ``model_terms`` into ``realised_net_value_eur`` and this
    fails.*
    """
    window = _window()
    ledger = window.ledger()["ledger"]

    assert ledger["model_terms"]["is_cash"] is False
    assert ledger["model_terms"]["switching_cost_eur"] == pytest.approx(0.40)

    cash = window.realized_net_cash_flow_eur
    assert cash is not None
    expected = window.realized_import_cost_eur - window.realized_export_revenue_eur
    assert cash == pytest.approx(expected)

    # And the model terms appear in neither total. ``realized_net_value_eur`` is a
    # *benefit* and carries the opposite sign to the cost above, deliberately: two
    # quantities that mean opposite things must not read alike.
    avoided = window.realized_load_avoidance_value_eur
    assert avoided is not None
    assert window.realized_net_value_eur == pytest.approx(avoided - cash)

    # The hurdle and the margin are 0.75 between them and appear in neither.
    assert window.realized_net_value_eur != pytest.approx(avoided - cash - 0.75)
    assert window.realized_net_value_eur != pytest.approx(avoided - cash + 0.75)


def test_no_provenance_is_claimed_that_a_battery_cannot_have() -> None:
    """``trade_profit_eur`` stays absent, at every window size and shape.

    Reporting it would require an inventory convention -- FIFO, average cost --
    and the convention would be doing all the work while looking like a
    measurement.
    """
    assert _window().as_dict()["trade_profit_eur"] is None
    assert (
        _window(grid_export_kwh=[0.0, 0.0, 0.0, 0.0]).as_dict()["trade_profit_eur"]
        is None
    )


# ===========================================================================
# 2. the attributed split -- a bound, computed per interval
# ===========================================================================


def test_the_grid_charge_split_is_bounded_by_both_flows_in_each_interval() -> None:
    """``min(import, charge)`` per interval, never over the totals.

    Taking the minimum of the *totals* would credit an interval's import against a
    different interval's charge, which is not a bound on anything. Per interval it
    is a genuine upper bound: no more of the pack's charge can have come from the
    grid than the grid actually delivered while it was charging.
    """
    window = _window()
    ledger = window.ledger()["ledger"]

    # **Both sides of the minimum are AC.** The series are state-of-charge deltas,
    # which are DC; the meter reads AC. 3.0 kWh into the pack needed 3.0 / 0.95 =
    # 3.158 kWh at the meter, and that is what the 4.0 kWh import is measured
    # against -- comparing it against the DC 3.0 would credit the grid with energy
    # the charging loss consumed and hand the difference to production.
    drawn = 3.0 / 0.95
    assert ledger["grid_charge_kwh"] == pytest.approx(drawn, abs=1e-3)
    assert ledger["grid_charge_cost_eur"] == pytest.approx(drawn * 0.10, abs=1e-3)
    # Interval 2 charged 3.0 kWh with the meter reading zero import, so none of it
    # can have come from the grid whatever the pack did.
    assert ledger["pv_charge_kwh"] == pytest.approx(drawn, abs=1e-3)
    # The two shares are exactly the window's own charge total, in one unit.
    assert window.realized_battery_charge_kwh == pytest.approx(
        ledger["grid_charge_kwh"] + ledger["pv_charge_kwh"], abs=1e-3
    )


def test_the_export_split_counts_discharge_and_not_only_charge() -> None:
    """**The sign asymmetry that made every export report zero.**

    Stage B's realised-energy accumulators clamped at ``max(0.0, ...)`` in both
    bases, so a discharge integrated to exactly nothing and the first real Sell
    this project executed reported ``0.0`` against a 5.75 kWh target. The ledger
    keeps both directions, and this asserts the direction that used to vanish.

    *Mutation: clamp the discharge series to zero and this fails.*
    """
    ledger = _window().ledger()["ledger"]
    # 3.5 kWh left the pack in the last interval and 3.0 crossed the meter, so the
    # export is the binding side of the bound.
    assert ledger["battery_to_grid_kwh"] == pytest.approx(3.0)
    assert ledger["battery_to_grid_kwh"] > 0.0


def test_an_absent_battery_measurement_yields_none_and_not_zero() -> None:
    """No measurement is not a measurement of nothing.

    Without per-interval movement there is nothing to split the grid flows
    against, and a zero would read as "none of this charge came from the grid",
    which is a claim nobody made.
    """
    ledger = _window(battery_charge_kwh=None, battery_discharge_kwh=None).ledger()[
        "ledger"
    ]
    assert ledger["grid_charge_kwh"] is None
    assert ledger["pv_charge_kwh"] is None
    assert ledger["battery_to_grid_kwh"] is None


def test_the_position_value_is_carried_and_never_computed_here() -> None:
    """``realized`` may not import the solver, so the value is handed to it.

    That separation is the module's whole point: it imports no Home Assistant, no
    planner and no policy, and a structural test pins it. The inventory values are
    planner-derived and arrive as arguments.
    """
    window = _window()
    ledger = window.ledger()["ledger"]

    assert ledger["opening_inventory_value_eur"] == pytest.approx(0.60)
    assert ledger["closing_inventory_value_eur"] == pytest.approx(1.40)
    assert window.realized_plus_remaining_value_eur == pytest.approx(
        window.realized_net_value_eur + 1.40 - 0.60
    )

    source = __import__(
        "custom_components.alpha_ems_manager.realized", fromlist=["realized"]
    )
    import inspect

    text = inspect.getsource(source)
    assert "homeassistant" not in text
    assert "from .economic" not in text
    assert "from .coordinator" not in text


# ===========================================================================
# 3. it survives a restart, because it stores nothing of its own
# ===========================================================================


def test_the_ledger_is_a_pure_function_of_series_already_persisted() -> None:
    """**Restart survival, proved by construction rather than by a fixture.**

    Every input is a series ``DayRecord`` already keeps for 365 days and a price
    ``PriceSnapshot`` already keeps beside it. Handing the same series in twice
    gives the same ledger, which is what "rebuilt from disk" means -- and it is why
    beta.35 adds no storage and bumps no schema version.
    """
    first = _window().ledger()
    second = _window().ledger()
    assert first == second


def test_prices_that_were_never_stored_are_skipped_rather_than_valued_at_zero() -> None:
    """An unknown price is not a free kilowatt-hour.

    Skipping is the same rule Phase 6 applies everywhere, and ``intervals_skipped``
    is published beside the totals so a partial window can never be mistaken for a
    cheap one.
    """
    window = _window(import_price_eur_kwh=[None, 0.10, 0.40, 0.40])
    assert window.intervals_skipped >= 1
    # The unpriced import contributed no cost, and said so rather than adding zero.
    assert window.realized_import_cost_eur == pytest.approx(4.0 * 0.10)
    assert window.realized_grid_import_kwh == pytest.approx(4.0)


# ===========================================================================
# 4. the ledger spans days, because a battery does
# ===========================================================================


def _measured_day(day):
    """Return one civil day of measured flows: a cheap charge and a dear sale.

    Written through ``record_interval`` rather than by poking the arrays, so the
    record is shaped exactly as a live installation files one -- including the
    state-of-charge series the battery figures are differenced from.
    """
    from .synthetic import empty_day

    record = empty_day(day)
    count = record.interval_count
    stored = 30.0
    for index in range(count):
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
            soc_percent=min(95.0, max(20.0, stored)),
            pv_kwh=0.0,
            grid_import_kwh=imported,
            grid_export_kwh=exported,
        )
    return record


async def test_the_multi_day_ledger_prices_more_than_one_civil_day(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Midnight is a calendar event, not an economic one.**

    A pack charged at 03:00 and sold at 19:00 the following evening is one
    position, and a view that resets at midnight cannot describe it -- which is
    also the moment a stored-energy value would appear to step for no physical
    reason.

    Both days here are real: measured ``DayRecord``s from the fixture history, and
    a ``PriceForecast`` the production parser built, re-filed under the earlier day
    so the two overlap. The prices being the same series twice is deliberate and
    harmless -- what is under test is that a longer window is the same arithmetic
    over a longer list, and re-using one day's prices makes the additive result
    exactly predictable.

    **Nothing new is stored to make this work.** Every input is already persisted:
    ``DayRecord`` keeps the measured series for a year and ``PriceSnapshot`` keeps
    the prices, so the ledger rebuilds itself after a restart with nothing else to
    remember.
    """
    from dataclasses import replace

    from .forecast_helpers import NORMAL
    from .test_beta35_campaign_continuity import start_the_campaign

    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = (coordinator.data or {}).get("battery_plan")
    assert plan is not None and plan.target_day == NORMAL

    # Two measured days, identical by construction so the sum is predictable.
    yesterday = NORMAL - timedelta(days=1)
    coordinator.store.days[NORMAL] = _measured_day(NORMAL)
    coordinator.store.days[yesterday] = _measured_day(yesterday)

    forecast = (coordinator.price_forecasts or {}).get(NORMAL)
    assert forecast is not None, "the driven refresh must have stored today's prices"
    coordinator.price_forecasts[yesterday] = replace(forecast, target_day=yesterday)

    one = coordinator.realized_today(plan)
    assert one["available"] is True
    assert one["intervals_priced"] > 0

    many = coordinator.realized_days(plan, days=2)
    assert many["available"] is True
    assert many["days_requested"] == 2
    assert many["days_priced"] == 2
    assert many["first_day"] == yesterday.isoformat()
    assert many["last_day"] == NORMAL.isoformat()

    # Additive, and nothing dropped: two identical days price twice everything.
    assert many["intervals_priced"] == 2 * one["intervals_priced"]
    assert many["grid_import_kwh"] == pytest.approx(2 * one["grid_import_kwh"])
    assert many["grid_export_kwh"] == pytest.approx(2 * one["grid_export_kwh"])
    assert many["import_cost_eur"] == pytest.approx(2 * one["import_cost_eur"])
    assert one["grid_import_kwh"] > 0.0 and one["grid_export_kwh"] > 0.0

    # And it is the same ledger, with the same provenance map and the same refusal
    # to total a hurdle rate as cash.
    assert set(many["ledger"]["basis"]) == set(one["ledger"]["basis"])
    assert many["ledger"]["model_terms"]["is_cash"] is False
    assert many["trade_profit_eur"] is None


async def test_a_day_whose_prices_were_never_stored_is_skipped_not_zeroed(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """A window is honest about how much of itself it could price.

    Asking for a week on an installation that has kept two days of prices must
    report two days, not a week with five silently free ones -- which is the same
    rule ``intervals_skipped`` applies inside a single day.
    """
    from .forecast_helpers import NORMAL
    from .test_beta35_campaign_continuity import start_the_campaign

    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = (coordinator.data or {}).get("battery_plan")
    assert plan is not None
    coordinator.store.days[NORMAL] = _measured_day(NORMAL)

    many = coordinator.realized_days(plan, days=7)
    assert many["available"] is True
    assert many["days_requested"] == 7
    assert many["days_priced"] == 1, "only today has both a record and prices"
    assert many["first_day"] == many["last_day"] == NORMAL.isoformat()


# ===========================================================================
# beta.36: the campaign ledger's identities, as equalities
# ===========================================================================


async def test_a_row_is_accrued_exactly_once(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Exactly once, made a property of a field rather than of call ordering.**

    Three sites can record a completed row -- the tick's end-of-row, the tick's
    end-of-quarter and the refresh's between-ticks catch-up -- and nothing said they
    were mutually exclusive. beta.35 published ``quarters_admitted: 2`` against three
    completed rows, which is the *losing* half of that ambiguity; the same gap makes
    double-counting representable, and no trace has happened to exhibit it yet.

    Driven by calling the accrual twice for one row, which is exactly what two of
    those sites firing for the same boundary would do.

    *Mutation: drop the ``_campaign_accrued_row`` latch and this fails.*
    """
    from .test_beta35_campaign_continuity import start_the_campaign

    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    row = coordinator._quarter
    assert row is not None
    assert coordinator._campaign_id == row.campaign_id, "the witness: it is accruable"

    # The fixture already accrued this row, so the guard is already latched -- and
    # that is the first half of the property: a row cannot be accrued a second time
    # just because another site got there.
    assert coordinator._campaign_accrued_row == row.quarter_start
    latched_kwh = coordinator._campaign_realized_kwh
    latched_rows = coordinator._campaign_quarters_admitted
    coordinator._accrue_campaign_progress(row, 1.5)
    assert coordinator._campaign_realized_kwh == latched_kwh
    assert coordinator._campaign_quarters_admitted == latched_rows

    # And the second half: released, exactly one of two calls lands.
    coordinator._campaign_accrued_row = None
    before_kwh = coordinator._campaign_realized_kwh
    before_rows = coordinator._campaign_quarters_admitted

    coordinator._accrue_campaign_progress(row, 1.5)
    once_kwh = coordinator._campaign_realized_kwh
    once_rows = coordinator._campaign_quarters_admitted

    coordinator._accrue_campaign_progress(row, 1.5)

    assert once_kwh == before_kwh + 1.5
    assert once_rows == before_rows + 1
    # Equalities, not inequalities: ">= the first reading" would pass on a double.
    assert coordinator._campaign_realized_kwh == once_kwh
    assert coordinator._campaign_quarters_admitted == once_rows
    assert coordinator._campaign_accrued_row == row.quarter_start


async def test_the_terminal_ledger_balances_as_equalities(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Three identities, and every one of them an ``==``.**

    ``>=`` would have passed on the 2026-08-30 payload in one direction and on a
    double-count in the other. The terminal's realised energy is the sum of the rows
    it accrued, its row count is the number it accrued, and neither figure may
    disagree with the live one it was read from a moment earlier.
    """
    from .test_beta35_campaign_continuity import start_the_campaign

    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    row = coordinator._quarter
    assert row is not None
    coordinator._accrue_campaign_progress(row, 1.25)

    live_kwh = coordinator._campaign_realized_now()
    live_rows = coordinator._campaign_quarters_admitted
    assert live_rows >= 1, "the witness: something was accrued"

    coordinator._close_campaign(row.quarter_end, EXECUTION_STOP_WINDOW_ENDED)
    terminal = coordinator._closed_campaign or {}

    assert terminal, "a started campaign files a terminal"
    assert terminal["objective_realized_kwh"] == pytest.approx(live_kwh, abs=1e-9)
    assert terminal["quarters_admitted"] == live_rows
    assert terminal["rows_completed"] == live_rows
