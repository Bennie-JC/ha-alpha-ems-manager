"""What actually happened, priced at what it actually cost.

Pure. Nothing here imports Home Assistant, and nothing here is read by the
optimizer, the reserve, the policy, the safety gate or the control pipeline --
which is the whole point and is asserted structurally rather than trusted.

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


def _finite(value: float | None) -> float | None:
    """Return the value when it is a usable number."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):  # pragma: no cover - storage is typed
        return None
    return number if number == number and abs(number) != float("inf") else None


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

    opening = None
    if stored_energy_kwh:
        for value in stored_energy_kwh:
            opening = _finite(value)
            if opening is not None:
                break

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
    "realized_window",
    "soc_series_to_energy",
]
