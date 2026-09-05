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
    ACCOUNTING_BASIS_POSITION,
    ACCOUNTING_RECONCILIATION_TOLERANCE_EUR,
    AVOIDANCE_BASIS_NO_BATTERY,
    LEDGER_BASIS_ATTRIBUTED,
    LEDGER_BASIS_ESTIMATED,
    LEDGER_BASIS_FORECAST,
    LEDGER_BASIS_MEASURED,
    LEDGER_BASIS_MODEL_TERM,
    LEDGER_BASIS_PLANNER_DERIVED,
    LEDGER_BASIS_REVALUED,
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

    #: What a household with the same load and the same production but **no
    #: battery** would have imported and exported: ``max(0, load - pv)`` and
    #: ``max(0, pv - load)`` per interval, priced at that interval's own rates.
    #:
    #: **beta.42, and the export leg is the one that was missing.** Only the import
    #: side of this counterfactual existed, as ``realized_load_avoidance_*``, so any
    #: figure built from it credited the battery with the export revenue a bare
    #: photovoltaic array earns by itself. Both legs are needed to say what
    #: operating the battery actually *changed*.
    realized_no_battery_import_kwh: float | None = None
    realized_no_battery_export_kwh: float | None = None
    realized_no_battery_cost_eur: float | None = None
    realized_no_battery_export_revenue_eur: float | None = None

    #: How many intervals contributed to the counterfactual. Below
    #: ``intervals_priced`` wherever load or production was missing, and published
    #: so a partial comparison is visible rather than merely smaller.
    counterfactual_intervals_priced: int = 0

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
    def realized_no_battery_net_cash_eur(self) -> float | None:
        """Return what the same household would have paid with no battery.

        Positive means money would have left the household -- the same sign as
        :attr:`realized_net_cash_flow_eur`, so the two can be differenced directly.
        """
        if (
            self.realized_no_battery_cost_eur is None
            or self.realized_no_battery_export_revenue_eur is None
        ):
            return None
        return round(
            self.realized_no_battery_cost_eur
            - self.realized_no_battery_export_revenue_eur,
            _EUR_DECIMALS,
        )

    @property
    def realized_battery_benefit_eur(self) -> float | None:
        """Return what operating the battery actually saved, in cash.

        **The incremental comparison, and the only figure here an investment return
        may be built on.** beta.42 exists partly because a different one was
        mistaken for it::

            benefit = no_battery_net_cash - actual_net_cash
                    = (SUM p*N - SUM s*X) - (SUM p*I - SUM s*E)

        ``N`` and ``X`` are the import and export a household with the same load and
        the same production but no battery would have had; ``I`` and ``E`` are what
        the meter recorded. **Positive means the battery saved money.**

        Contrast :attr:`realized_net_value_eur`, which is
        ``avoided_import_value - net_cash_flow``. Expand it and it equals
        ``benefit - SUM p*min(I, N) + SUM s*X``: it subtracts the household's
        unavoidable electricity bill, and adds back export revenue a bare array
        earns without any battery. That is a household *position* -- a real thing to
        want, and kept -- but its dominant term is the bill, so it reads strongly
        negative for anyone who imports anything. Published under the name "what the
        battery saved" it would have reported the battery destroying value.

        ``None`` rather than a partial answer wherever a term is missing, which is
        the rule the rest of this module follows.
        """
        counterfactual = self.realized_no_battery_net_cash_eur
        if counterfactual is None:
            return None
        return round(counterfactual - self.realized_net_cash_flow_eur, _EUR_DECIMALS)

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
                # **The comparator, published beside the figure it replaces.
                # beta.42.** ``realised_net_value_eur`` below is the household's
                # whole position -- it subtracts an import bill no battery could
                # have avoided and credits PV export that needed none -- so it is
                # structurally negative for any household that imports anything and
                # was never a battery comparator, whatever its name suggested. These
                # six are: four measured cash legs and the two differences built
                # from them. Both are published, so a reader can see they are
                # different numbers rather than discovering it from a sign.
                "no_battery_import_kwh": self.realized_no_battery_import_kwh,
                "no_battery_export_kwh": self.realized_no_battery_export_kwh,
                "no_battery_cost_eur": self.realized_no_battery_cost_eur,
                "no_battery_export_revenue_eur": (
                    self.realized_no_battery_export_revenue_eur
                ),
                "no_battery_net_cash_eur": self.realized_no_battery_net_cash_eur,
                "battery_benefit_eur": self.realized_battery_benefit_eur,
                "counterfactual_intervals_priced": (
                    self.counterfactual_intervals_priced
                ),
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
        # beta.42. Four measured cash legs and the two comparators built from them.
        # Measured throughout: no attribution rule, no model constant, no planner
        # valuation -- which is exactly what makes them the only figures here an
        # investment return may be built on.
        "no_battery_import_kwh": LEDGER_BASIS_MEASURED,
        "no_battery_export_kwh": LEDGER_BASIS_MEASURED,
        "no_battery_cost_eur": LEDGER_BASIS_MEASURED,
        "no_battery_export_revenue_eur": LEDGER_BASIS_MEASURED,
        "no_battery_net_cash_eur": LEDGER_BASIS_MEASURED,
        "battery_benefit_eur": LEDGER_BASIS_MEASURED,
        # A count of intervals, not a quantity -- but the guard that every published
        # ledger figure carries a basis makes no exception for counts, and it is
        # right not to: a reader who has to learn which keys the map covers has
        # learned the map is incomplete. Measured, because it counts intervals that
        # had both a price and a reading.
        "counterfactual_intervals_priced": LEDGER_BASIS_MEASURED,
        "opening_inventory_value_eur": LEDGER_BASIS_PLANNER_DERIVED,
        "closing_inventory_value_eur": LEDGER_BASIS_PLANNER_DERIVED,
        # **``measured`` until beta.42, and it never was.** One of its two addends
        # is ``realized_load_avoidance_value_eur``, which is attributed -- measured
        # energy split by a stated rule -- so the total inherits the weaker of the
        # two claims. A basis map that upgrades a figure's honesty by summing it is
        # worse than no basis map.
        "realised_net_value_eur": LEDGER_BASIS_ATTRIBUTED,
        "realised_plus_remaining_value_eur": LEDGER_BASIS_PLANNER_DERIVED,
        # beta.39, and the reason the vocabulary grew a sixth word: these four
        # are not all the same kind of number. Two are measured, one is a
        # forecast of the rest of the day, and one is the same energy valued on
        # two different curves -- and a reader who cannot tell them apart will
        # eventually difference the last one against the first.
        "today_accounting.realised_today_eur": LEDGER_BASIS_PLANNER_DERIVED,
        "today_accounting.in_progress_interval_eur": LEDGER_BASIS_MEASURED,
        # A figure about energy that has not moved, over prices and a load forecast
        # that will both be wrong to some degree. ``planner_derived`` said only where
        # it came from; this says what can still falsify it.
        "today_accounting.remaining_expected_today_eur": LEDGER_BASIS_FORECAST,
        "today_accounting.forecast_revaluation_eur": LEDGER_BASIS_REVALUED,
        # Contains the forecast term above, so it inherits it. Same rule as
        # ``realised_net_value_eur``: a total is as strong as its weakest addend.
        "today_accounting.total_economic_value_today_eur": LEDGER_BASIS_FORECAST,
        # beta.42, and this is the half of the finding that mattered. Publishing the
        # map at the entity was the easy part; ten of the fifteen euro attributes an
        # operator actually sees were not *in* it, so the projection would have told
        # them "unclassified" ten times -- honest, and no use at all. These are the
        # Economic Value entity's own figures, classified where the ledger's are.
        #
        # **The forward-looking ones are ``forecast``, not ``planner_derived``.**
        # ``decision_advantage_eur`` is a comparison against a counterfactual *from
        # now to the end of the horizon*, and its two day halves are that same
        # comparison split by civil day. All three move when the weather does, which
        # is exactly the distinction beta.42 added the seventh word for.
        "decision_advantage_eur": LEDGER_BASIS_FORECAST,
        "advantage_cash_eur": LEDGER_BASIS_FORECAST,
        "today_interval_value_eur": LEDGER_BASIS_FORECAST,
        "tomorrow_interval_value_eur": LEDGER_BASIS_FORECAST,
        "next_planned_charge_price_eur_kwh": LEDGER_BASIS_FORECAST,
        # A residual between the published total and the published addends, so it
        # inherits the weakest of them by the same rule that made the total a
        # forecast. It is a rounding check, not a quantity of money.
        "accounting_reconciliation_error_eur": LEDGER_BASIS_FORECAST,
        # The value function's slope at the head bucket, and the credit at the
        # horizon edge. Both are the optimiser valuing energy that exists, at one
        # instant, on one curve -- which is what ``planner_derived`` means.
        "stored_energy_marginal_value_eur_kwh": LEDGER_BASIS_PLANNER_DERIVED,
        "terminal_edge_value_eur_kwh": LEDGER_BASIS_PLANNER_DERIVED,
        # The price of the interval in flight. The import leg is all-in cash taken
        # from the source's own total; the export leg is **not** -- the source
        # publishes no feed-in price, so it is reconstructed as market plus an
        # adjustment that defaults to zero. Same rate, two different kinds of number,
        # and giving them one word here would undo the whole point of the map.
        "current_import_price_eur_kwh": LEDGER_BASIS_MEASURED,
        "current_export_price_eur_kwh": LEDGER_BASIS_ESTIMATED,
        "model_terms.switching_cost_eur": LEDGER_BASIS_MODEL_TERM,
        "model_terms.grid_charge_margin_eur": LEDGER_BASIS_MODEL_TERM,
        "model_terms.throughput_cost_eur": LEDGER_BASIS_MODEL_TERM,
    }


def day_partition(*, head: int, interval_count: int) -> tuple[range, int | None, range]:
    """Return the civil day's three slices: closed, in flight, still to come.

    ``head`` is the solved plan's own first interval index -- the next interval,
    never the one in progress -- so the closed part of the day is ``[0, head-1)``,
    the quarter in flight is ``head-1`` and what remains is ``[head, N)``.

    **Defined by ``head`` and not by which intervals have data**, which is what
    makes the three provably disjoint and provably exhaustive on a 92, 96 or
    100-interval day alike. A partition inferred from the persisted record instead
    would silently drop a quarter whenever a sample was missing, and it is exactly
    that one missing quarter -- index 84 at 21:00 on 2026-09-02, in neither the
    realised window nor the plan's remaining slice -- that this exists to close.

    Clamped at both ends. ``head`` at or below zero leaves nothing closed and no
    quarter in flight, which is the shape of the first refresh after midnight.
    """
    count = max(0, interval_count)
    clamped = max(0, min(head, count))
    return (
        range(0, max(0, clamped - 1)),
        clamped - 1 if clamped >= 1 else None,
        range(clamped, count),
    )


@dataclass(frozen=True, slots=True)
class DayAccounting:
    """One civil day's economic position, as five terms that sum to a total.

    **The identity is the deliverable, not the total.** Published so it can be
    checked from the payload alone:

    .. code-block:: text

        realised_today_eur
        + in_progress_interval_eur
        + remaining_expected_today_eur
        + forecast_revaluation_eur
        = total_economic_value_today_eur

    and it telescopes to something a person can state in one sentence. Writing
    ``R`` for cash realised in the closed part of the day, ``G`` for cash
    realised so far in the quarter in flight, ``P`` for cash the plan still
    expects before midnight, ``V[now]`` for this refresh's value curve and
    ``V[open]`` for the curve as it stood when the day opened:

    .. code-block:: text

        realised_today_eur       = R + V[now](e_close) - V[now](e_open)
        in_progress_interval_eur = G
        remaining_expected_eur   = P
        forecast_revaluation_eur = V[now](e_open) - V[open](e_open)
        -------------------------------------------------------------
        total                    = R + G + P + V[now](e_close) - V[open](e_open)

    -- the day's cash, plus what the pack is worth now, less what it was worth
    when the day began. Every term cancels except the two ends of the position,
    so **no residual is hidden inside any addend**: if the five do not sum to the
    total within :data:`ACCOUNTING_RECONCILIATION_TOLERANCE_EUR`, the total is
    withheld and the error is published.

    **One counterfactual throughout.** ``R`` and ``G`` are measured against a
    household with no battery, and ``P`` is recomputed on the same basis from the
    solved plan rather than taken from the planner's per-interval idle figure.
    Mixing the two would have been the one dishonest move available here; see
    ``AVOIDANCE_BASIS_NO_BATTERY``.

    **The quarter in flight is its own term and never joins history.** It becomes
    part of ``realised_today_eur`` when the quarter closes and its measurement is
    persisted, and until then it is separately inspectable with its own coverage
    beside it -- because a partial quarter folded into realised history is a
    figure that can go down.

    ``None`` propagates. A total missing one of its terms is not a smaller total,
    it is a different number wearing the same name, so a missing addend takes the
    total with it and ``unavailable_reason`` says which.
    """

    realised_today_eur: float | None
    in_progress_interval_eur: float | None
    remaining_expected_today_eur: float | None
    forecast_revaluation_eur: float | None
    total_economic_value_today_eur: float | None
    #: The gap between the published total and the sum of the published addends.
    #:
    #: **A rounding check, and beta.42 stopped it claiming to be more.** The total
    #: is *defined* as the sum of the four addends and the error is that sum less
    #: the rounded addends, so both sides are the same four numbers and the residual
    #: can only ever be rounding -- bounded by four half-ulps at four decimals,
    #: against a tolerance five times that. It cannot fail, and it certainly cannot
    #: detect a *wrong* addend.
    #:
    #: What the prose used to imply is that it tested the telescoping identity
    #: ``R + G + P + V[now](close) - V[open](open)``. Nothing computes that
    #: right-hand side independently, so that check does not exist. Building it
    #: would mean deriving the position a second way, which is the thing this module
    #: refuses to do elsewhere for good reason -- two derivations of one number is
    #: how two published figures come to disagree. So the check keeps its real and
    #: modest job, and now says so.
    reconciliation_error_eur: float | None
    unavailable_reason: str | None

    #: The day's interval partition, published so exhaustiveness and disjointness
    #: are checkable rather than asserted. ``realised`` covers ``[0, h-1)``, the
    #: quarter in flight is ``h-1``, and ``remaining`` covers ``[h, N)`` -- where
    #: ``h`` is the solved plan's own first index. The three are disjoint and
    #: cover the whole civil day by construction, at 92, 96 and 100 intervals
    #: alike, because the partition is defined by ``h`` and not by which
    #: intervals happen to have data.
    interval_count: int | None = None
    realised_interval_count: int | None = None
    in_progress_interval_index: int | None = None
    remaining_interval_count: int | None = None
    #: How many of ``[h, N)`` the plan's priced horizon actually reaches. Equal to
    #: ``remaining_interval_count`` on a fully priced civil day; smaller where the
    #: source's market day does not span the local one, and then the total is
    #: withheld rather than published over a day with a hole in it.
    remaining_planned_interval_count: int | None = None
    realised_intervals_priced: int | None = None
    realised_intervals_skipped: int | None = None
    in_progress_coverage: float | None = None

    #: The two ends of the position, and the provenance of the opening valuation.
    opening_inventory_kwh: float | None = None
    opening_inventory_value_eur: float | None = None
    opening_valuation_eur: float | None = None
    opening_valued_at: str | None = None
    #: The reference each end of the position was measured against. Published
    #: rather than checked: the reserve floor moves with the load and production
    #: forecasts, and its effect on what the position is worth is forecast
    #: revaluation in the plainest sense -- so it is reported, not refused. The
    #: lattice pitch is the one that *is* refused, because a different pitch means
    #: a different configuration rather than a different forecast.
    opening_floor_kwh: float | None = None
    opening_bucket_kwh: float | None = None
    current_floor_kwh: float | None = None
    current_bucket_kwh: float | None = None
    closing_inventory_kwh: float | None = None
    closing_inventory_value_eur: float | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the published block, with its own rule attached."""
        return {
            "realised_today_eur": self.realised_today_eur,
            "in_progress_interval_eur": self.in_progress_interval_eur,
            "remaining_expected_today_eur": self.remaining_expected_today_eur,
            "forecast_revaluation_eur": self.forecast_revaluation_eur,
            "total_economic_value_today_eur": self.total_economic_value_today_eur,
            "reconciliation_error_eur": self.reconciliation_error_eur,
            "unavailable_reason": self.unavailable_reason,
            "accounting_basis": ACCOUNTING_BASIS_POSITION,
            "avoidance_basis": AVOIDANCE_BASIS_NO_BATTERY,
            "partition": {
                "interval_count": self.interval_count,
                "realised_intervals": self.realised_interval_count,
                "in_progress_index": self.in_progress_interval_index,
                "remaining_intervals": self.remaining_interval_count,
                "remaining_planned_intervals": self.remaining_planned_interval_count,
                "remaining_unpriced_intervals": (
                    None
                    if self.remaining_interval_count is None
                    or self.remaining_planned_interval_count is None
                    else self.remaining_interval_count
                    - self.remaining_planned_interval_count
                ),
                "realised_intervals_priced": self.realised_intervals_priced,
                "realised_intervals_skipped": self.realised_intervals_skipped,
                "in_progress_coverage": self.in_progress_coverage,
                "rule": (
                    "realised covers [0, h-1), the quarter in flight is h-1 and "
                    "remaining covers [h, N), where h is the solved plan's own "
                    "first index and N the civil day's real interval count. "
                    "disjoint and exhaustive by construction on a 92, 96 or "
                    "100-interval day: the partition is defined by h, not by "
                    "which intervals have data. skipped intervals are reported "
                    "rather than assumed absent"
                ),
            },
            "position": {
                "opening_inventory_kwh": self.opening_inventory_kwh,
                "opening_inventory_value_eur": self.opening_inventory_value_eur,
                "opening_valuation_eur": self.opening_valuation_eur,
                "opening_valued_at": self.opening_valued_at,
                "opening_floor_kwh": self.opening_floor_kwh,
                "opening_bucket_kwh": self.opening_bucket_kwh,
                "current_floor_kwh": self.current_floor_kwh,
                "current_bucket_kwh": self.current_bucket_kwh,
                "closing_inventory_kwh": self.closing_inventory_kwh,
                "closing_inventory_value_eur": self.closing_inventory_value_eur,
                "rule": (
                    "both inventory values are this refresh's curve, so their "
                    "difference is what operating the battery achieved and "
                    "carries no revaluation. opening_valuation_eur is the same "
                    "opening energy valued on the curve that existed when the "
                    "day opened, persisted once, and the revaluation is the "
                    "difference of the two. the two floors are published rather "
                    "than checked: the reserve floor moves with the load and "
                    "production forecasts and its effect on the position's worth "
                    "IS forecast revaluation. the lattice pitch is checked, "
                    "because a different pitch is a different configuration. no "
                    "purchase cost and no inventory convention is involved in "
                    "any of them"
                ),
            },
            "rule": (
                "realised + in_progress + remaining + revaluation = total, and "
                "the total telescopes to the day's cash plus what the pack is "
                "worth now less what it was worth when the day opened. this is "
                "an economic position, not money in the bank: two of the terms "
                "are planner valuations and one is a forecast. no residual is "
                "absorbed into any addend. reconciliation_error_eur is a rounding "
                "check on the four addends a reader can add up, not an independent "
                "derivation of the telescoped identity -- nothing computes that "
                "right-hand side separately, so this cannot detect a wrong addend, "
                "only a total that does not equal the numbers printed beside it"
            ),
        }


def day_accounting(
    *,
    realised: RealizedWindow | None,
    in_progress_eur: float | None,
    in_progress_index: int | None,
    in_progress_coverage: float | None,
    remaining_expected_eur: float | None,
    forecast_revaluation_eur: float | None,
    opening_valuation_eur: float | None = None,
    opening_valued_at: str | None = None,
    opening_floor_kwh: float | None = None,
    opening_bucket_kwh: float | None = None,
    current_floor_kwh: float | None = None,
    current_bucket_kwh: float | None = None,
    interval_count: int | None = None,
    realised_interval_count: int | None = None,
    remaining_interval_count: int | None = None,
    remaining_planned_interval_count: int | None = None,
    unavailable_reason: str | None = None,
) -> DayAccounting:
    """Assemble the four terms into a total, or refuse to.

    Pure arithmetic over figures computed elsewhere: this module may not import
    the solver, and the value curve lives on the other side of that line. What it
    owns is the *addition* -- which terms may be added, in what order, and when
    the answer must be withheld -- because that is the part that has to be
    reviewable in one place.

    ``realised_today_eur`` is taken from ``realized_plus_remaining_value_eur``
    and not recomputed. That figure is already ``R + V[now](close) - V[now](open)``,
    it is the identity beta.38 shipped, and a second derivation of the same thing
    is how two published numbers come to disagree.
    """
    realised_today = (
        None if realised is None else realised.realized_plus_remaining_value_eur
    )
    reason = unavailable_reason
    addends = (
        realised_today,
        in_progress_eur,
        remaining_expected_eur,
        forecast_revaluation_eur,
    )
    total: float | None = None
    error: float | None = None
    # **Finite, not merely present.** A ``nan`` addend propagates through the sum
    # and through the tolerance test too -- ``abs(nan) > tol`` is false -- so a
    # single non-finite term would have published a total reading ``nan`` and
    # passed its own reconciliation check.
    if reason is None and all(
        value is not None
        and float(value) == float(value)
        and abs(float(value)) != float("inf")
        for value in addends
    ):
        raw = sum(float(value) for value in addends)  # type: ignore[arg-type]
        total = round(raw, _EUR_DECIMALS)
        # **Checked against the rounded addends a reader can actually add up**,
        # not against the raw sum, because the published figures are what anybody
        # will reconcile and four-decimal rounding is exactly what makes five of
        # them miss their own total.
        error = round(
            total - sum(round(float(value), _EUR_DECIMALS) for value in addends),
            _EUR_DECIMALS + 4,
        )
        if abs(error) > ACCOUNTING_RECONCILIATION_TOLERANCE_EUR:
            # **Withheld, never patched.** Absorbing the residual into one of the
            # addends would make the equation balance and the figure a lie.
            total = None
    return DayAccounting(
        realised_today_eur=(
            None if realised_today is None else round(realised_today, _EUR_DECIMALS)
        ),
        in_progress_interval_eur=(
            None if in_progress_eur is None else round(in_progress_eur, _EUR_DECIMALS)
        ),
        remaining_expected_today_eur=(
            None
            if remaining_expected_eur is None
            else round(remaining_expected_eur, _EUR_DECIMALS)
        ),
        forecast_revaluation_eur=(
            None
            if forecast_revaluation_eur is None
            else round(forecast_revaluation_eur, _EUR_DECIMALS)
        ),
        total_economic_value_today_eur=total,
        reconciliation_error_eur=error,
        unavailable_reason=reason,
        interval_count=interval_count,
        realised_interval_count=realised_interval_count,
        in_progress_interval_index=in_progress_index,
        remaining_interval_count=remaining_interval_count,
        remaining_planned_interval_count=remaining_planned_interval_count,
        realised_intervals_priced=(
            None if realised is None else realised.intervals_priced
        ),
        realised_intervals_skipped=(
            None if realised is None else realised.intervals_skipped
        ),
        in_progress_coverage=in_progress_coverage,
        opening_inventory_kwh=(
            None if realised is None else realised.opening_inventory_kwh
        ),
        opening_inventory_value_eur=(
            None if realised is None else realised.opening_inventory_value_eur
        ),
        opening_valuation_eur=(
            None if opening_valuation_eur is None else round(opening_valuation_eur, 6)
        ),
        opening_valued_at=opening_valued_at,
        opening_floor_kwh=opening_floor_kwh,
        opening_bucket_kwh=opening_bucket_kwh,
        current_floor_kwh=current_floor_kwh,
        current_bucket_kwh=current_bucket_kwh,
        closing_inventory_kwh=(
            None if realised is None else realised.closing_inventory_kwh
        ),
        closing_inventory_value_eur=(
            None if realised is None else realised.closing_inventory_value_eur
        ),
    )


def open_quarter_value_eur(
    *,
    grid_import_kwh: float | None,
    grid_export_kwh: float | None,
    load_kwh: float | None,
    production_kwh: float | None,
    import_price_eur_kwh: float | None,
    export_price_eur_kwh: float | None,
) -> float | None:
    """Return what the quarter in flight has realised so far, or ``None``.

    **The same construction as one interval of** :func:`realized_window`, and
    that is the point: ``avoided + export_revenue - import_cost``, with the
    avoidance measured against a household that has no battery at all. Written
    once, here, so the open quarter and the closed ones cannot come to rest on
    different arithmetic -- which is precisely how a partial term ends up
    double-counting or contradicting the history it will become part of.

    Fed from the live per-quarter integrators rather than from storage, because
    storage does not have this interval yet: a quarter is persisted when it
    closes. That is also why it cannot double-count -- at the instant the
    measurement lands, the interval leaves this term and enters the realised
    slice, and the two are indexed by the same ``h``.

    ``None`` rather than zero wherever a flow exists that cannot be priced, on
    exactly the rule :func:`realized_window` applies per interval.
    """
    imported = _finite(grid_import_kwh)
    exported = _finite(grid_export_kwh)
    buy = _finite(import_price_eur_kwh)
    sell = _finite(export_price_eur_kwh)
    if imported is None or exported is None or buy is None or sell is None:
        return None
    load = _finite(load_kwh)
    produced = _finite(production_kwh)
    if load is None or produced is None:
        return None
    # ``max(0, max(0, load - pv) - import)``: what the meter would have shown with
    # no battery, less what it did show. Measured on both sides.
    avoided = max(0.0, max(0.0, load - produced) - max(0.0, imported))
    return round(
        avoided * buy + max(0.0, exported) * sell - max(0.0, imported) * buy,
        _EUR_DECIMALS,
    )


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
    # The no-battery counterfactual, both legs. beta.42.
    no_battery_import = no_battery_export = 0.0
    no_battery_cost = no_battery_revenue = 0.0
    counterfactual_intervals = 0
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
                # **Both legs of the counterfactual, priced. beta.42.**
                #
                # A household with the same load and the same production but no
                # battery would have imported ``max(0, load - pv)`` and exported
                # ``max(0, pv - load)``. The second of those did not exist anywhere
                # in this module before, which is why the old figure credited the
                # battery with export revenue that needed no battery.
                #
                # Priced at the same interval's rates as everything else here, and
                # counted only where the interval was priced at all, so the
                # comparison is like for like.
                spilled = max(0.0, produced - load)
                no_battery_import += without_battery
                no_battery_export += spilled
                no_battery_cost += without_battery * buy
                if sell is not None:
                    no_battery_revenue += spilled * sell
                counterfactual_intervals += 1

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
        realized_no_battery_import_kwh=(
            round(no_battery_import, _KWH_DECIMALS) if have_avoidance else None
        ),
        realized_no_battery_export_kwh=(
            round(no_battery_export, _KWH_DECIMALS) if have_avoidance else None
        ),
        realized_no_battery_cost_eur=(
            round(no_battery_cost, _EUR_DECIMALS) if have_avoidance else None
        ),
        realized_no_battery_export_revenue_eur=(
            round(no_battery_revenue, _EUR_DECIMALS) if have_avoidance else None
        ),
        counterfactual_intervals_priced=counterfactual_intervals,
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
