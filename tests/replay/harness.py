"""Run one Stage-A decision through several architectures and compare them.

**The gate beta.31's economics must pass, and the instrument that makes any claim
about them checkable.** The predecessor review could not cost two real days of
purchases because three inputs were never recorded; this exists so that never
happens again, and so a change to the objective is argued from numbers rather
than from intuition.

Every architecture below is the *production* solver -- ``build_physics_table``,
``build_horizon``, ``solve`` -- with one thing varied. Nothing here reimplements
physics, prices or the objective, because a comparison run on a simplified model
is a statement about the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from custom_components.alpha_ems_manager.battery import BatteryLimits
from custom_components.alpha_ems_manager.economic import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    EconomicPlan,
    IntervalPrice,
    actionable_intervals,
    build_horizon,
    build_physics_table,
    edge_creditable_energy_kwh,
    edge_value_eur_per_kwh,
    solve,
)
from custom_components.alpha_ems_manager.reserve import (
    build_reserve,
    build_reserve_reachable,
    uncertainty_margin,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

#: The four architectures the review compared, plus the do-nothing reference.
ARCH_AUTONOMY = "A_autonomy_beta30"
ARCH_REACHABILITY = "D_reachability_beta31"
ARCH_FLOOR_RELAXED = "C_floor_relaxed"
ARCH_CHEAPEST_NO_EXPORT = "B_cheapest_feasible_no_export"
ARCH_HOLD = "hold_no_ems"


@dataclass(frozen=True, slots=True)
class Decision:
    """One recorded Stage-A decision, as everything needed to replay it.

    The field list is the answer to "what was missing" from the beta.30 evidence:
    the demands and prices were recoverable, and ``start_energy_kwh`` and the
    three gates were not.
    """

    limits: BatteryLimits
    floor_energy_kwh: float
    start_energy_kwh: float
    demands: tuple[IntervalDemand, ...]
    prices: tuple[IntervalPrice, ...]
    bucket_kwh: float = 0.25
    minimum_trade_gain_eur: float = 0.20
    grid_charge_margin_eur_per_kwh: float = 0.0
    battery_throughput_cost_eur_per_kwh: float = 0.0
    mae_kwh_per_interval: float | None = None
    allow_grid_charging: bool = True
    allow_battery_export: bool = True


@dataclass(frozen=True, slots=True)
class Result:
    """What one architecture did with one decision.

    Every figure is read off the plan the solver produced, never accumulated by
    this module, so a disagreement between two of them is a disagreement inside
    the plan rather than an artefact of the harness.
    """

    architecture: str
    available: bool
    grid_purchase_kwh: float = 0.0
    purchase_cost_eur: float = 0.0
    cost_eur: float = 0.0
    hold_cost_eur: float = 0.0
    objective_eur: float = 0.0
    minimum_soc_percent: float | None = None
    floor_violations: int = 0
    reserve_violation_kwh: float = 0.0
    charge_throughput_kwh: float = 0.0
    discharge_throughput_kwh: float = 0.0
    throughput_kwh: float = 0.0
    throughput_cost_eur: float = 0.0
    pv_stored_kwh: float = 0.0
    pv_displaced_kwh: float = 0.0
    export_kwh: float = 0.0
    export_revenue_eur: float = 0.0
    avoided_import_kwh: float = 0.0
    switching_count: int = 0
    end_soc_percent: float | None = None
    edge_energy_kwh: float = 0.0
    edge_value_eur: float = 0.0
    bridge_kwh_now: float | None = None
    reserve_now_dc_kwh: float | None = None
    uncertainty: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        """Return a flat comparison row, rounded for reading."""
        return {
            "architecture": self.architecture,
            "buy_kwh": round(self.grid_purchase_kwh, 3),
            "buy_cost_eur": round(self.purchase_cost_eur, 4),
            "cost_eur": round(self.cost_eur, 4),
            "objective_eur": round(self.objective_eur, 4),
            "min_soc": None
            if self.minimum_soc_percent is None
            else round(self.minimum_soc_percent, 1),
            "end_soc": None
            if self.end_soc_percent is None
            else round(self.end_soc_percent, 1),
            "floor_violations": self.floor_violations,
            "reserve_violation_kwh": round(self.reserve_violation_kwh, 3),
            "throughput_kwh": round(self.throughput_kwh, 3),
            "throughput_cost_eur": round(self.throughput_cost_eur, 4),
            "pv_stored_kwh": round(self.pv_stored_kwh, 3),
            "pv_displaced_kwh": round(self.pv_displaced_kwh, 3),
            "export_kwh": round(self.export_kwh, 3),
            "export_revenue_eur": round(self.export_revenue_eur, 4),
            "avoided_import_kwh": round(self.avoided_import_kwh, 3),
            "switches": self.switching_count,
            "edge_kwh": round(self.edge_energy_kwh, 3),
            "edge_value_eur": round(self.edge_value_eur, 4),
            "bridge_kwh_now": None
            if self.bridge_kwh_now is None
            else round(self.bridge_kwh_now, 3),
            "reserve_now": None
            if self.reserve_now_dc_kwh is None
            else round(self.reserve_now_dc_kwh, 3),
        }


def _permitted(decision: Decision, *, export: bool) -> frozenset[str]:
    """Return the action set an architecture is allowed to use."""
    actions = {ECONOMIC_ACTION_DISCHARGE, ECONOMIC_ACTION_CURTAIL}
    if decision.allow_grid_charging:
        actions.add(ECONOMIC_ACTION_CHARGE)
    if export and decision.allow_battery_export:
        actions.add(ECONOMIC_ACTION_EXPORT)
    return frozenset(actions)


def _measure(
    architecture: str,
    plan: EconomicPlan,
    decision: Decision,
    *,
    reserve_now: float | None,
    bridge: float | None,
    uncertainty: dict[str, Any],
    edge_value: float,
) -> Result:
    """Read the comparison figures off a solved plan."""
    capacity = decision.limits.capacity_kwh
    floor = decision.floor_energy_kwh
    energies = [entry.start_energy_dc_kwh for entry in plan.intervals]
    energies.append(plan.end_energy_dc_kwh)
    # A floor violation is counted against the *configured* floor, never against
    # the reserve: the reserve is a planning bound and the floor is physical.
    violations = sum(1 for value in energies if value < floor - 1e-9)

    purchase_cost = sum(
        max(0.0, entry.marginal_grid_import_kwh) * entry.import_price_eur_kwh
        for entry in plan.intervals
    )
    export_revenue = sum(
        max(0.0, entry.marginal_grid_export_kwh) * entry.export_price_eur_kwh
        for entry in plan.intervals
    )
    # Production the plan stored, and production it could not store because the
    # pack was full. The second is the cost of buying too early.
    pv_stored = sum(
        min(entry.battery_charge_ac_kwh, demand.surplus_kwh)
        for entry, demand in zip(plan.intervals, decision.demands, strict=False)
    )
    pv_displaced = sum(
        max(0.0, demand.surplus_kwh - entry.battery_charge_ac_kwh)
        for entry, demand in zip(plan.intervals, decision.demands, strict=False)
    )
    return Result(
        architecture=architecture,
        available=plan.available,
        grid_purchase_kwh=sum(
            max(0.0, entry.marginal_grid_import_kwh) for entry in plan.intervals
        ),
        purchase_cost_eur=purchase_cost,
        cost_eur=plan.cost_eur,
        hold_cost_eur=plan.hold_cost_eur,
        # The quantity the solver actually minimised, edge credit included.
        objective_eur=plan.cost_eur - plan.edge_value_eur,
        minimum_soc_percent=(
            None if not energies else 100.0 * min(energies) / capacity
        ),
        floor_violations=violations,
        reserve_violation_kwh=plan.violation_kwh,
        charge_throughput_kwh=plan.planned_charge_ac_kwh,
        discharge_throughput_kwh=plan.planned_discharge_ac_kwh,
        throughput_kwh=plan.battery_throughput_kwh,
        throughput_cost_eur=plan.battery_throughput_cost_eur,
        pv_stored_kwh=pv_stored,
        pv_displaced_kwh=pv_displaced,
        export_kwh=sum(
            max(0.0, entry.marginal_grid_export_kwh) for entry in plan.intervals
        ),
        export_revenue_eur=export_revenue,
        avoided_import_kwh=sum(
            max(0.0, -entry.marginal_grid_import_kwh) for entry in plan.intervals
        ),
        switching_count=plan.direction_changes,
        end_soc_percent=100.0 * plan.end_energy_dc_kwh / capacity,
        edge_energy_kwh=plan.edge_energy_kwh,
        edge_value_eur=plan.edge_value_eur,
        bridge_kwh_now=bridge,
        reserve_now_dc_kwh=reserve_now,
        uncertainty=uncertainty,
    )


def replay(decision: Decision, architectures: Sequence[str] | None = None):
    """Return one :class:`Result` per architecture, keyed by name.

    The only difference between them is the reserve curve and the terminal value.
    Prices, demands, physics, the bucket lattice, the gates and the objective are
    identical, which is what makes the difference attributable.
    """
    wanted = list(
        architectures
        or (
            ARCH_AUTONOMY,
            ARCH_REACHABILITY,
            ARCH_FLOOR_RELAXED,
            ARCH_CHEAPEST_NO_EXPORT,
        )
    )
    table = build_physics_table(
        decision.limits,
        floor_energy_kwh=decision.floor_energy_kwh,
        bucket_kwh=decision.bucket_kwh,
    )
    if table is None:  # pragma: no cover - build_limits precludes it
        return {}

    actionable = actionable_intervals(decision.demands, decision.prices)
    autonomy = build_reserve(
        limits=decision.limits,
        floor_energy_kwh=decision.floor_energy_kwh,
        demands=decision.demands,
    )
    probe = build_reserve_reachable(
        limits=decision.limits,
        floor_energy_kwh=decision.floor_energy_kwh,
        demands=decision.demands,
        grid_credit_intervals=actionable,
    )
    margin = uncertainty_margin(
        probe,
        mae_kwh_per_interval=decision.mae_kwh_per_interval,
        usable_capacity_kwh=decision.limits.capacity_kwh,
    )
    reachability = build_reserve_reachable(
        limits=decision.limits,
        floor_energy_kwh=decision.floor_energy_kwh + margin.total_dc_kwh,
        demands=decision.demands,
        grid_credit_intervals=actionable,
    )

    curves = {
        ARCH_AUTONOMY: (
            tuple(entry.required_dc_kwh for entry in autonomy.intervals),
            autonomy,
            0.0,
            True,
        ),
        ARCH_REACHABILITY: (
            tuple(entry.required_dc_kwh for entry in reachability.intervals),
            reachability,
            None,  # filled below: the edge value needs the built horizon
            True,
        ),
        ARCH_FLOOR_RELAXED: (
            tuple(decision.floor_energy_kwh for _ in decision.demands),
            None,
            0.0,
            True,
        ),
        ARCH_CHEAPEST_NO_EXPORT: (
            tuple(decision.floor_energy_kwh for _ in decision.demands),
            None,
            0.0,
            False,
        ),
    }

    results: dict[str, Result] = {}
    for name in wanted:
        curve, projection, edge, export = curves[name]
        horizon = build_horizon(
            demands=decision.demands,
            prices=decision.prices,
            required_reserve_kwh=curve,
            table=table,
        )
        if not horizon.intervals:
            results[name] = Result(architecture=name, available=False)
            continue
        edge_value = (
            edge_value_eur_per_kwh(
                horizon.prices,
                discharge_efficiency=decision.limits.discharge_efficiency,
            )
            if edge is None
            else edge
        )
        creditable = edge_creditable_energy_kwh(
            ceiling_kwh=decision.limits.energy_for_soc(decision.limits.max_soc_percent),
            forecast_surplus_kwh=sum(
                demand.surplus_kwh for demand in decision.demands[:actionable]
            ),
        )
        plan = solve(
            table=table,
            horizon=horizon,
            start_energy_kwh=decision.start_energy_kwh,
            terminal_floor_kwh=decision.floor_energy_kwh,
            minimum_trade_gain_eur=decision.minimum_trade_gain_eur,
            permitted=_permitted(decision, export=export),
            grid_charge_margin_eur_per_kwh=decision.grid_charge_margin_eur_per_kwh,
            battery_throughput_cost_eur_per_kwh=(
                decision.battery_throughput_cost_eur_per_kwh
            ),
            edge_value_eur_per_kwh=edge_value,
            edge_creditable_kwh=creditable,
        )
        results[name] = _measure(
            name,
            plan,
            decision,
            reserve_now=None if projection is None else projection.required_now_dc_kwh,
            bridge=(
                None
                if projection is None
                else projection.bridge_kwh(decision.start_energy_kwh)
            ),
            uncertainty=margin.as_dict() if name == ARCH_REACHABILITY else {},
            edge_value=edge_value,
        )
    return results


def table_of(results: dict[str, Result]) -> str:
    """Return a fixed-width comparison table, for a report or a failure message."""
    rows = [result.as_row() for result in results.values()]
    if not rows:
        return "(no architectures produced a plan)"
    columns = list(rows[0])
    widths = {
        column: max(len(column), *(len(str(row[column])) for row in rows))
        for column in columns
    }
    header = "  ".join(column.rjust(widths[column]) for column in columns)
    body = [
        "  ".join(str(row[column]).rjust(widths[column]) for column in columns)
        for row in rows
    ]
    return "\n".join([header, *body])
