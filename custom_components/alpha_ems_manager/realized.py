"""What actually happened, priced at what it actually cost.

Pure. Nothing here imports Home Assistant, and nothing here is read by the
optimizer, the reserve, the policy, the safety gate or the control pipeline --
which is the whole point and is asserted structurally rather than trusted. The one
import beyond the standard library is ``const``, which is itself pure: a shared
vocabulary is not a dependency on a runtime.

Realized is not expected
------------------------

Every euro Phase 8 publishes is a *forecast*: what a plan should cost at prices it
can see. This module answers the other question, and the two must never be
confused. A forecast presented as money earned is the single most misleading thing
an energy integration can publish, so the names here all begin ``realized_`` and
the figures are computed from measured flows only.

Why this needs no new storage
-----------------------------

The evidence was already being recorded. ``storage.DayRecord`` keeps, per interval
and per day, the measured house baseline, the flexible load, the state of charge,
production, and **grid import and export** -- integrated through
``QuarterAccumulator`` and subject to the same coverage threshold as everything
else. ``price_forecast.PriceSnapshot`` keeps the import and export price series for
the same intervals. So realized cost is a multiplication over data already on
disk, and beta.18 adds no persistence at all.

The sunk-cost trap, and why there is no ledger here
---------------------------------------------------

It is tempting to carry a purchase cost for the energy in the pack and refuse to
sell below it. That is **economically wrong**, and the reason is worth writing
down: energy that cost 0.20 EUR/kWh is a sunk cost. If exporting it now at 0.18
makes room for production that would otherwise be curtailed, or for energy at
0.05, then selling at a "loss" is the correct decision and a cost-basis rule would
forbid it.

So this module computes **no cost basis**, and the optimizer is given nothing from
it. What a kilowatt-hour cost in the past cannot change what the next one is worth.

What is deliberately not published
----------------------------------

``realized_trade_profit`` is absent. Attributing a discharged kilowatt-hour to a
particular earlier charge requires an inventory convention -- weighted average,
first-in-first-out -- and a battery has no physical ordering that would make either
one true rather than merely conventional. Publishing a number that depends on an
arbitrary choice, beside numbers that do not, would lend it a precision it has not
got. Load avoidance and cash flow are reported instead, because both are
measurable without inventing anything.

Energy already in the pack when the window opens has **unknown provenance**. It is
reported as an opening inventory in kilowatt-hours and never priced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .const import (
    LEDGER_BASIS_ATTRIBUTED,
    LEDGER_BASIS_ESTIMATED,
    LEDGER_BASIS_MEASURED,
    LEDGER_BASIS_MODEL_TERM,
    LEDGER_BASIS_PLANNER_DERIVED,
)

#: Rounding for published energies and euro figures, matching the rest of Phase 8.
_KWH_DECIMALS = 3
_EUR_DECIMALS = 4

#: How the battery figures were arrived at, because two routes exist and they are
#: not equally trustworthy.
BATTERY_BASIS_STATE_OF_CHARGE = "state_of_charge_delta"
BATTERY_BASIS_UNAVAILABLE = "unavailable"

#: Provenance of energy already stored when the window opened.
PROVENANCE_UNKNOWN = "unknown_provenance"


@dataclass(frozen=True, slots=True)
class RealizedWindow:
    """Measured flows and what they cost, over one contiguous set of intervals.

    Every field is either measured, or arithmetic over measured values at prices
    recorded for the same intervals. Nothing is forecast and nothing is inferred
    from a convention.
    """

    #: How many intervals had everything needed to price them, and how many were
    #: skipped. Published because a partial day must not look like a whole one.
    intervals_priced: int
    intervals_skipped: int

    realized_grid_import_kwh: float
    realized_grid_export_kwh: float
    realized_import_cost_eur: float
    realized_export_revenue_eur: float

    #: Battery movement, and how it was derived. ``None`` when the state of charge
    #: was not recorded densely enough to difference honestly.
    realized_battery_charge_kwh: float | None
    realized_battery_discharge_kwh: float | None
    battery_basis: str

    #: What the battery saved by supplying load that would otherwise have been
    #: imported. Measured throughout: the counterfactual import is
    #: ``max(0, load - production)`` and the actual import is recorded, so the
    #: difference needs no assumption about where the energy came from.
    realized_load_avoidance_kwh: float | None
    realized_load_avoidance_value_eur: float | None

    #: Stored energy at the first priced interval, and its provenance. Never
    #: priced: a figure for what it cost would have to be invented.
    opening_inventory_kwh: float | None
    opening_inventory_provenance: str

    # -- beta.35: the attributed split, and the model terms kept out of cash ----
    #
    #: Grid energy that arrived while the battery was charging, and what it cost.
    #:
    #: **Attributed, never provenance.** The rule is stated and applied per
    #: interval -- ``min(import, battery_charge)`` -- and it is a *bound*, not a
    #: claim that those electrons entered the pack. A battery has no physical
    #: ordering that would make any stronger statement true, which is the same
    #: argument this module has made against a cost basis since beta.18.
    realized_grid_charge_kwh: float | None = None
    realized_grid_charge_cost_eur: float | None = None
    #: The remainder of the charge, by subtraction. Attributed on the same terms.
    realized_pv_charge_kwh: float | None = None
    #: Discharge that left through the meter, ``min(export, battery_discharge)``.
    realized_battery_to_grid_kwh: float | None = None
    #: Conversion loss, from the configured efficiencies. **Estimated**: it is a
    #: model constant applied to measured energy, and a real inverter's loss varies
    #: with power in ways one round-trip figure cannot express.
    realized_conversion_loss_kwh: float | None = None
    #: Stored energy at the last priced interval.
    closing_inventory_kwh: float | None = None
    #: What that closing inventory is worth, from the planner's own value function.
    #:
    #: **Planner-derived, and not a measurement of anything.** It is
    #: ``V(floor) - V(current)`` at the head of the current solve: how much better
    #: off the plan is for holding this energy rather than sitting at the floor.
    #: ``None`` where the optimiser could not state it -- see the stored-value
    #: undefined reasons -- because zero would read as "worthless".
    closing_inventory_value_eur: float | None = None
    #: The same figure for the opening inventory, where one was recorded.
    opening_inventory_value_eur: float | None = None
    #: The objective's hurdle and wear terms. **Not cash, and never totalled with
    #: it.** The minimum trade gain is a hurdle rate, the grid-charge margin is a
    #: hurdle per purchased kWh and the throughput cost is a wear proxy. None of
    #: them is an expense anybody paid, and adding them to a cash total would be
    #: the same error as pricing sunk cost.
    model_switching_cost_eur: float | None = None
    model_grid_charge_margin_eur: float | None = None
    model_throughput_cost_eur: float | None = None

    @property
    def realized_net_cash_flow_eur(self) -> float:
        """Return cost less revenue. **Positive means money left the household.**

        The same sign convention Phase 8 uses for ``cost_eur``, so the two can be
        read side by side without a mental flip -- which is exactly the confusion
        this module exists to prevent.
        """
        return round(
            self.realized_import_cost_eur - self.realized_export_revenue_eur,
            _EUR_DECIMALS,
        )

    @property
    def realized_net_value_eur(self) -> float | None:
        """Return what the household is better off by, this window, in cash terms.

        Avoided import is a real saving and belongs here; the model terms do not.
        **Negative means money left the household**, which is the opposite sign
        from :attr:`realized_net_cash_flow_eur` on purpose: that one is a cost and
        this one is a benefit, and giving them the same sign would make them look
        like the same quantity.
        """
        if self.realized_load_avoidance_value_eur is None:
            return None
        return round(
            self.realized_load_avoidance_value_eur - self.realized_net_cash_flow_eur,
            _EUR_DECIMALS,
        )

    @property
    def realized_plus_remaining_value_eur(self) -> float | None:
        """Return the window's value **including the change in what is stored**.

        **The answer to "was buying twenty and selling five worth it".** Judging
        that trade on the sold slice alone is exactly the error the user named: the
        fifteen still in the pack are worth something, and what they are worth is
        not their purchase price but what the plan can still do with them.

        ``None`` rather than a partial sum wherever either inventory could not be
        valued -- a total missing one of its terms is not a smaller total, it is a
        different number wearing the same name.
        """
        base = self.realized_net_value_eur
        if (
            base is None
            or self.closing_inventory_value_eur is None
            or self.opening_inventory_value_eur is None
        ):
            return None
        return round(
            base + self.closing_inventory_value_eur - self.opening_inventory_value_eur,
            _EUR_DECIMALS,
        )

    def ledger(self) -> dict[str, object]:
        """Return the beta.35 ledger block, with every figure's basis beside it.

        **Descriptive. It is not a second decision engine and cannot become one:**
        this module imports no Home Assistant, is read by no planner, reserve,
        policy, safety or control path, and a structural test pins all of that.

        It answers a question the forecast figures cannot -- *what actually
        happened, and where does the position stand now* -- and it answers it
        without inventing provenance. ``trade_profit_eur`` stays absent for the
        reason it always has.
        """
        return {
            "ledger": {
                "grid_charge_kwh": self.realized_grid_charge_kwh,
                "grid_charge_cost_eur": self.realized_grid_charge_cost_eur,
                "pv_charge_kwh": self.realized_pv_charge_kwh,
                "battery_to_grid_kwh": self.realized_battery_to_grid_kwh,
                "battery_to_house_kwh": self.realized_load_avoidance_kwh,
                "avoided_import_value_eur": self.realized_load_avoidance_value_eur,
                "conversion_loss_kwh": self.realized_conversion_loss_kwh,
                "opening_inventory_kwh": self.opening_inventory_kwh,
                "opening_inventory_value_eur": self.opening_inventory_value_eur,
                "closing_inventory_kwh": self.closing_inventory_kwh,
                "closing_inventory_value_eur": self.closing_inventory_value_eur,
                "realised_net_value_eur": self.realized_net_value_eur,
                "realised_plus_remaining_value_eur": (
                    self.realized_plus_remaining_value_eur
                ),
                "model_terms": {
                    "switching_cost_eur": self.model_switching_cost_eur,
                    "grid_charge_margin_eur": self.model_grid_charge_margin_eur,
                    "throughput_cost_eur": self.model_throughput_cost_eur,
                    "is_cash": False,
                    "rule": (
                        "hurdle rates and a wear proxy from the optimiser's "
                        "objective, not expenses anybody paid. reported here so a "
                        "plan's arithmetic can be reconciled, and kept out of "
                        "every cash total on purpose"
                    ),
                },
                "basis": _basis_map(),
                "rule": (
                    "attributed figures split measured energy by a stated "
                    "per-interval rule and are bounds, never a claim that "
                    "particular energy took a particular path -- a battery has no "
                    "physical ordering that would make one true. inventory values "
                    "come from the planner's own value function and are an "
                    "opportunity value from now, never a purchase cost"
                ),
            }
        }

    def as_dict(self) -> dict[str, object]:
        """Return the published shape, with its own caveats attached."""
        return {
            "intervals_priced": self.intervals_priced,
            "intervals_skipped": self.intervals_skipped,
            "grid_import_kwh": self.realized_grid_import_kwh,
            "grid_export_kwh": self.realized_grid_export_kwh,
            "import_cost_eur": self.realized_import_cost_eur,
            "export_revenue_eur": self.realized_export_revenue_eur,
            "net_cash_flow_eur": self.realized_net_cash_flow_eur,
            "battery_charge_kwh": self.realized_battery_charge_kwh,
            "battery_discharge_kwh": self.realized_battery_discharge_kwh,
            "battery_basis": self.battery_basis,
            "load_avoidance_kwh": self.realized_load_avoidance_kwh,
            "load_avoidance_value_eur": self.realized_load_avoidance_value_eur,
            "opening_inventory_kwh": self.opening_inventory_kwh,
            "opening_inventory_provenance": self.opening_inventory_provenance,
            "trade_profit_eur": None,
            **self.ledger(),
            "rule": (
                "measured flows at the prices recorded for the same intervals. "
                "these are realised figures, never forecasts, and nothing in the "
                "optimizer, the reserve or the safety path reads them. "
                "trade_profit_eur is deliberately absent: attributing a "
                "discharged kilowatt-hour to a particular earlier charge needs an "
                "inventory convention a battery does not physically have. energy "
                "already stored when the window opened is reported as opening "
                "inventory and never priced"
            ),
        }


def _basis_map() -> dict[str, str]:
    """Return which kind of number every ledger figure is.

    **Published beside the figures, not in a docstring.** The ledger mixes four
    kinds of number and a fifth kind of non-number, and a reader who cannot tell
    them apart will eventually add a hurdle rate to a cash total. Measured came
    from a meter; attributed is measured energy split by a stated rule; estimated
    came from a model constant; planner-derived came from the optimiser's value
    function; and a model term is not money at all.
    """
    return {
        "grid_import_kwh": LEDGER_BASIS_MEASURED,
        "grid_export_kwh": LEDGER_BASIS_MEASURED,
        "import_cost_eur": LEDGER_BASIS_MEASURED,
        "export_revenue_eur": LEDGER_BASIS_MEASURED,
        "net_cash_flow_eur": LEDGER_BASIS_MEASURED,
        "battery_charge_kwh": LEDGER_BASIS_MEASURED,
        "battery_discharge_kwh": LEDGER_BASIS_MEASURED,
        "opening_inventory_kwh": LEDGER_BASIS_MEASURED,
        "closing_inventory_kwh": LEDGER_BASIS_MEASURED,
        "grid_charge_kwh": LEDGER_BASIS_ATTRIBUTED,
        "grid_charge_cost_eur": LEDGER_BASIS_ATTRIBUTED,
        "pv_charge_kwh": LEDGER_BASIS_ATTRIBUTED,
        "battery_to_grid_kwh": LEDGER_BASIS_ATTRIBUTED,
        "battery_to_house_kwh": LEDGER_BASIS_ATTRIBUTED,
        "avoided_import_value_eur": LEDGER_BASIS_ATTRIBUTED,
        "conversion_loss_kwh": LEDGER_BASIS_ESTIMATED,
        "opening_inventory_value_eur": LEDGER_BASIS_PLANNER_DERIVED,
        "closing_inventory_value_eur": LEDGER_BASIS_PLANNER_DERIVED,
        "realised_net_value_eur": LEDGER_BASIS_MEASURED,
        "realised_plus_remaining_value_eur": LEDGER_BASIS_PLANNER_DERIVED,
        "model_terms.switching_cost_eur": LEDGER_BASIS_MODEL_TERM,
        "model_terms.grid_charge_margin_eur": LEDGER_BASIS_MODEL_TERM,
        "model_terms.throughput_cost_eur": LEDGER_BASIS_MODEL_TERM,
    }


def _finite(value: float | None) -> float | None:
    """Return the value when it is a usable number."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):  # pragma: no cover - storage is typed
        return None
    return number if number == number and abs(number) != float("inf") else None


def opening_inventory_kwh(
    stored_energy_kwh: Sequence[float | None] | None,
) -> float | None:
    """Return the pack level the window opened at, or ``None``.

    The first reading the window has, which is what an opening inventory means.

    **Extracted in beta.38 so two callers cannot disagree.** The ledger reports
    this figure and the coordinator has to *value* it, and those are different
    modules -- this one may not import the solver, and the solver's value curve
    lives on the other side of that line. Two independent readings of the same
    series is exactly how a value comes to describe an energy nobody reported, so
    there is one rule and both sides call it.
    """
    if not stored_energy_kwh:
        return None
    for value in stored_energy_kwh:
        opening = _finite(value)
        if opening is not None:
            return opening
    return None


def closing_inventory_kwh(stored_energy_kwh):
    """Return the pack level the window closed at, or ``None``.

    The last reading the window has, which is where the position stands now.
    Measured, like the opening figure, and priced by nobody here -- the caller
    values it, and calls this so the energy it prices is the energy published.
    """
    if not stored_energy_kwh:
        return None
    for value in reversed(stored_energy_kwh):
        closing = _finite(value)
        if closing is not None:
            return closing
    return None


def realized_window(
    *,
    grid_import_kwh: Sequence[float | None],
    grid_export_kwh: Sequence[float | None],
    import_price_eur_kwh: Sequence[float | None],
    export_price_eur_kwh: Sequence[float | None],
    load_kwh: Sequence[float | None] | None = None,
    production_kwh: Sequence[float | None] | None = None,
    stored_energy_kwh: Sequence[float | None] | None = None,
    capacity_kwh: float | None = None,
    charge_efficiency: float | None = None,
    discharge_efficiency: float | None = None,
    #: Per-interval battery movement, when it is measured densely enough to split
    #: the grid flows against. Absent leaves every attributed figure ``None``,
    #: which is the honest answer rather than a zero.
    battery_charge_kwh: Sequence[float | None] | None = None,
    battery_discharge_kwh: Sequence[float | None] | None = None,
    #: What the optimiser says the pack is worth at each end of the window.
    #: Planner-derived and passed in rather than computed here, because this
    #: module may not import the solver -- that separation is the point of it.
    opening_inventory_value_eur: float | None = None,
    closing_inventory_value_eur: float | None = None,
    #: The objective's hurdle and wear terms, reported and never totalled as cash.
    model_switching_cost_eur: float | None = None,
    model_grid_charge_margin_eur: float | None = None,
    model_throughput_cost_eur: float | None = None,
) -> RealizedWindow:
    """Return what the measured intervals actually cost.

    Every series is indexed alike and may contain gaps. An interval is priced only
    when its flow **and** the price for that same flow are both present -- an
    import with no import price is skipped rather than valued at zero, which is
    the same rule Phase 6 applies to unknown prices and the reason
    ``intervals_skipped`` is published beside the totals.

    ``stored_energy_kwh`` is the recorded state of charge expressed as energy. When
    it is dense enough the battery figures are differenced from it; otherwise they
    are reported as ``None`` with the basis saying so. They are never derived from
    the balance identity as a fallback, because that route silently absorbs every
    measurement error in the other four terms and would look like a battery figure
    while being a residual.
    """
    charge_series = battery_charge_kwh or ()
    discharge_series = battery_discharge_kwh or ()
    grid_charge_kwh = 0.0
    grid_charge_cost = 0.0
    battery_to_grid_kwh = 0.0
    attributable = bool(charge_series) or bool(discharge_series)

    count = min(
        len(grid_import_kwh),
        len(grid_export_kwh),
        len(import_price_eur_kwh),
        len(export_price_eur_kwh),
    )

    priced = skipped = 0
    total_import = total_export = 0.0
    cost = revenue = 0.0
    avoided_kwh = 0.0
    avoided_value = 0.0
    have_avoidance = load_kwh is not None and production_kwh is not None

    for index in range(count):
        imported = _finite(grid_import_kwh[index])
        exported = _finite(grid_export_kwh[index])
        buy = _finite(import_price_eur_kwh[index])
        sell = _finite(export_price_eur_kwh[index])
        if imported is None and exported is None:
            skipped += 1
            continue
        # An interval counts as priced when the flows it has can all be valued.
        if (imported is not None and buy is None) or (
            exported is not None and sell is None
        ):
            skipped += 1
            continue
        priced += 1
        if imported is not None and buy is not None:
            total_import += imported
            cost += imported * buy
        if exported is not None and sell is not None:
            total_export += exported
            revenue += exported * sell

        # **The attributed split, per interval, by a stated rule. beta.35.**
        #
        # ``min(import, charge)`` bounds how much of this interval's import could
        # have gone into the pack, and ``min(export, discharge)`` bounds how much
        # of the export could have come out of it. Both are *bounds*, and the
        # ledger says so: a battery has no physical ordering that would let anyone
        # claim more, which is why there is still no cost basis anywhere here.
        #
        # **Both sides of each minimum are AC, and getting that wrong is silent.**
        # The series arrive as state-of-charge deltas, which are DC; the meter
        # reads AC. Comparing the two directly overstates how much of an import
        # could have reached the pack by the whole charging loss, and the residual
        # ``pv_charge_kwh`` then inherits the error with the opposite sign -- a
        # 6.32 kWh AC charge split as 3.00 grid and 3.32 production when the honest
        # answer is 3.16 and 3.16. Converted here with the same two efficiencies
        # ``_battery_from_state_of_charge`` uses on the totals, so the per-interval
        # split and the window totals are in one unit by construction.
        moved_in = _finite(charge_series[index]) if index < len(charge_series) else None
        moved_out = (
            _finite(discharge_series[index]) if index < len(discharge_series) else None
        )
        if moved_in is not None and imported is not None:
            drawn = max(0.0, moved_in) / (charge_efficiency or 1.0)
            attributed = min(max(0.0, imported), drawn)
            grid_charge_kwh += attributed
            if buy is not None:
                grid_charge_cost += attributed * buy
        if moved_out is not None and exported is not None:
            delivered = max(0.0, moved_out) * (discharge_efficiency or 1.0)
            battery_to_grid_kwh += min(max(0.0, exported), delivered)

        if have_avoidance and buy is not None:
            load = _finite(load_kwh[index]) if index < len(load_kwh) else None
            produced = (
                _finite(production_kwh[index]) if index < len(production_kwh) else None
            )
            if load is not None and produced is not None:
                # What the meter would have shown with no battery, less what it
                # did show. Both sides measured; no attribution needed.
                without_battery = max(0.0, load - produced)
                saved = max(0.0, without_battery - (imported or 0.0))
                avoided_kwh += saved
                avoided_value += saved * buy

    charge, discharge, basis = _battery_from_state_of_charge(
        stored_energy_kwh,
        capacity_kwh=capacity_kwh,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
    )

    opening = opening_inventory_kwh(stored_energy_kwh)

    closing = closing_inventory_kwh(stored_energy_kwh)

    # **Estimated, and labelled so.** One round-trip figure split symmetrically is
    # what the model has; a real inverter's loss varies with power in ways it
    # cannot express, so this is an indication of scale rather than a measurement.
    conversion_loss = None
    if charge is not None and discharge is not None:
        eta_c = charge_efficiency if charge_efficiency else 1.0
        eta_d = discharge_efficiency if discharge_efficiency else 1.0
        conversion_loss = round(
            max(0.0, charge * (1.0 - eta_c)) + max(0.0, discharge * (1.0 - eta_d)),
            _KWH_DECIMALS,
        )

    pv_charge = None
    if charge is not None:
        pv_charge = round(max(0.0, charge - grid_charge_kwh), _KWH_DECIMALS)

    return RealizedWindow(
        intervals_priced=priced,
        intervals_skipped=skipped,
        realized_grid_import_kwh=round(total_import, _KWH_DECIMALS),
        realized_grid_export_kwh=round(total_export, _KWH_DECIMALS),
        realized_import_cost_eur=round(cost, _EUR_DECIMALS),
        realized_export_revenue_eur=round(revenue, _EUR_DECIMALS),
        realized_battery_charge_kwh=charge,
        realized_battery_discharge_kwh=discharge,
        battery_basis=basis,
        realized_load_avoidance_kwh=(
            round(avoided_kwh, _KWH_DECIMALS) if have_avoidance else None
        ),
        realized_load_avoidance_value_eur=(
            round(avoided_value, _EUR_DECIMALS) if have_avoidance else None
        ),
        opening_inventory_kwh=(
            round(opening, _KWH_DECIMALS) if opening is not None else None
        ),
        opening_inventory_provenance=PROVENANCE_UNKNOWN,
        # The attributed split. ``None`` throughout when no per-interval battery
        # series was supplied -- there is nothing to attribute against, and a zero
        # would claim the pack neither charged from the grid nor exported.
        realized_grid_charge_kwh=(
            round(grid_charge_kwh, _KWH_DECIMALS) if attributable else None
        ),
        realized_grid_charge_cost_eur=(
            round(grid_charge_cost, _EUR_DECIMALS) if attributable else None
        ),
        realized_pv_charge_kwh=pv_charge if attributable else None,
        realized_battery_to_grid_kwh=(
            round(battery_to_grid_kwh, _KWH_DECIMALS) if attributable else None
        ),
        realized_conversion_loss_kwh=conversion_loss,
        closing_inventory_kwh=(
            round(closing, _KWH_DECIMALS) if closing is not None else None
        ),
        # **Rounded here, at four decimals, like every other euro figure.** The
        # planner hands over a raw float and beta.37 published it whole --
        # ``3.871514669200126`` beside a sensor showing ``3.8715`` for the same
        # quantity. Same number, two spellings, and a reader has to work out which.
        opening_inventory_value_eur=(
            None
            if opening_inventory_value_eur is None
            else round(opening_inventory_value_eur, _EUR_DECIMALS)
        ),
        closing_inventory_value_eur=(
            None
            if closing_inventory_value_eur is None
            else round(closing_inventory_value_eur, _EUR_DECIMALS)
        ),
        model_switching_cost_eur=model_switching_cost_eur,
        model_grid_charge_margin_eur=model_grid_charge_margin_eur,
        model_throughput_cost_eur=model_throughput_cost_eur,
    )


def _battery_from_state_of_charge(
    stored_energy_kwh: Sequence[float | None] | None,
    *,
    capacity_kwh: float | None,
    charge_efficiency: float | None,
    discharge_efficiency: float | None,
) -> tuple[float | None, float | None, str]:
    """Return AC charge and discharge energy differenced from the recorded state.

    Consecutive samples only. A gap ends one span and starts another rather than
    being bridged, because bridging would attribute to the battery whatever
    happened while nobody was looking.

    The conversion is the same one Phase 3 uses: a rise in stored DC energy took
    more AC energy than it gained, and a fall delivered less. Both efficiencies
    are required -- without them there is no honest AC figure, and reporting the
    DC delta as though it were AC would overstate what the house saw.
    """
    if not stored_energy_kwh or capacity_kwh is None:
        return None, None, BATTERY_BASIS_UNAVAILABLE
    if not charge_efficiency or not discharge_efficiency:
        return None, None, BATTERY_BASIS_UNAVAILABLE

    charge = discharge = 0.0
    seen = False
    previous: float | None = None
    for value in stored_energy_kwh:
        current = _finite(value)
        if current is None:
            previous = None
            continue
        if previous is not None:
            delta = current - previous
            if delta > 0.0:
                charge += delta / charge_efficiency
            elif delta < 0.0:
                discharge += -delta * discharge_efficiency
            seen = True
        previous = current

    if not seen:
        return None, None, BATTERY_BASIS_UNAVAILABLE
    return (
        round(charge, _KWH_DECIMALS),
        round(discharge, _KWH_DECIMALS),
        BATTERY_BASIS_STATE_OF_CHARGE,
    )


def soc_series_to_energy(
    soc_percent: Sequence[float | None], *, capacity_kwh: float | None
) -> tuple[float | None, ...]:
    """Return a recorded percentage series as stored energy.

    A convenience for the caller that holds the capacity, kept here so the
    conversion is written once rather than at each call site.
    """
    if capacity_kwh is None or capacity_kwh <= 0.0:
        return tuple(None for _ in soc_percent)
    return tuple(
        None if _finite(value) is None else (float(value) / 100.0) * capacity_kwh
        for value in soc_percent
    )


__all__ = [
    "BATTERY_BASIS_STATE_OF_CHARGE",
    "BATTERY_BASIS_UNAVAILABLE",
    "PROVENANCE_UNKNOWN",
    "RealizedWindow",
    "closing_inventory_kwh",
    "opening_inventory_kwh",
    "realized_window",
    "soc_series_to_energy",
]
