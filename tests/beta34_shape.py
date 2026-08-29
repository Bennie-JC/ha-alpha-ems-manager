"""The 2026-08-29 Hillview shape, reconstructed from the two live diagnostics.

Absolute interval indices, because that is where the frame bug lives: the test
harnesses all start at index 0, so ``survival_window_end`` returning an absolute
index has never been distinguishable from a relative one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from custom_components.alpha_ems_manager.battery import build_limits
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    actionable_intervals,
    build_horizon,
    build_outcome,
    build_physics_table,
    edge_creditable_energy_kwh,
    edge_value_eur_per_kwh,
    select_bucket_kwh,
)
from custom_components.alpha_ems_manager.reserve import (
    build_reserve,
    build_reserve_reachable,
    uncertainty_margin,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

#: The reference pack at Hillview.
CAPACITY: float = 21.6

LIMITS, _MISSING = build_limits(
    capacity_kwh=CAPACITY,
    max_charge_kw=10.0,
    max_discharge_kw=10.0,
    round_trip_efficiency_percent=90.0,
    max_soc_percent=100.0,
)
assert _MISSING is None
FLOOR: float = LIMITS.energy_for_soc(20.0)

# Fitted on two measured pairs from the live payload:
#   (0.1293, 0.0184) and (0.38600, 0.23055)   -> residual < 0.0001 on both,
# and verified against (0.34669, 0.19806) and (0.12783, 0.01718).
_EXPORT_SLOPE = 0.826500
_EXPORT_OFFSET = 0.088500


def export_of(import_eur: float) -> float:
    """Return the measured Dutch feed-in price for an import price."""
    return max(0.0, _EXPORT_SLOPE * import_eur - _EXPORT_OFFSET)


def hour_of(index: int) -> float:
    """Return the local hour for an absolute quarter index from midnight."""
    return (index % 96) / 4.0


def price_29aug(index: int) -> float:
    """Import price, EUR/kWh, matching every figure the payload pins.

    Pinned points: 0.128 through the sunny afternoon (``current_price`` at 13:55
    and 14:01), a 0.30-0.386 evening peak (the tomorrow-evening run's own
    intervals), and a demand-weighted mean of ~0.317 across 14:00-24:00, which is
    what the 13:45 ``protect_price`` head reports.
    """
    hour = hour_of(index)
    if hour < 6.0:
        return 0.235
    if hour < 8.0:
        return 0.275
    if hour < 11.0:
        return 0.185
    if hour < 16.0:
        return 0.128
    if hour < 17.0:
        return 0.205
    if hour < 22.0:
        # 0.30 at 17:00 rising to 0.386 at 19:30 and easing to 0.347 by 22:00
        return 0.300 + 0.086 * max(0.0, 1.0 - abs(hour - 19.5) / 2.5)
    return 0.262


def load_29aug(index: int) -> float:
    """House load per quarter, kWh. Integrates to ~21.2 kWh/day.

    Shape taken from the plan's own band split: 2.12 kWh overnight, 1.81 morning,
    0.97 afternoon and 10.07 evening of *battery* discharge, plus the reported
    ``today_total_kwh`` of 21.206 and ``today_remaining_kwh`` of 10.389 at 14:15.
    """
    hour = hour_of(index)
    shape = (
        0.105
        + 0.30 * math.exp(-((hour - 7.5) ** 2) / 2.2)
        + 0.62 * math.exp(-((hour - 19.5) ** 2) / 5.0)
    )
    return round(shape * 0.92057, 5)


def pv_29aug(index: int) -> float:
    """Production per quarter, kWh. Integrates to ~14.7 kWh, the reported total."""
    hour = hour_of(index)
    if 6.5 <= hour <= 20.0:
        return round(
            0.58 * 0.86420 * max(0.0, math.sin(math.pi * (hour - 6.5) / 13.5)) ** 1.6, 5
        )
    return 0.0


#: The measured forecast error from the 29 August payload, as the default.
#:
#: **Not cosmetic, and it cost half a day.** ``build_outcome`` runs the whole
#: export-permission pass only ``if forecast_risk is not None``: no risk, no
#: ungated solve, no survival window, no floor, no protection. A harness that
#: defaulted it to ``None`` therefore solved a different problem from production
#: and reported ``export_free=()`` -- which reads exactly like "the gate did not
#: bind" rather than "the gate did not run".
_LIVE_RISK: Any = "live"


@dataclass(frozen=True, slots=True)
class Solved:
    """One solved horizon plus the pieces an assertion needs."""

    outcome: Any
    table: Any
    head: int

    def __getattr__(self, name: str) -> Any:
        return getattr(self.outcome, name)


def solve_at(
    *,
    head: int,
    end: int,
    stored: float,
    price_fn=price_29aug,
    load_fn=load_29aug,
    pv_fn=pv_29aug,
    gain: float = 0.20,
    margin: float = 0.05,
    throughput: float = 0.0,
    mae: float | None = 0.06,
    forecast_risk: Any = _LIVE_RISK,
    allow_export: bool = True,
    allow_charge: bool = True,
    #: beta.35. What Stage B reports is physically running at the head, and how the
    #: horizon's edge is priced. Both default to the beta.34 behaviour -- idle, and
    #: the flat edge credit -- so every existing caller solves exactly as it did.
    head_run_state: int = 0,
    terminal_value: Any = None,
) -> Solved:
    """Solve one horizon through the production path, at absolute indices.

    ``forecast_risk`` defaults to the live measurement. Pass ``None`` explicitly
    to solve with the export permission switched off entirely -- which is a
    legitimate comparison and never an accident.
    """
    if forecast_risk == _LIVE_RISK:
        forecast_risk = risk_of()
    demands = tuple(
        IntervalDemand(index=i, baseline_kwh=load_fn(i), pv_kwh=pv_fn(i))
        for i in range(head, end)
    )
    prices = tuple(
        IntervalPrice(import_eur_kwh=price_fn(i), export_eur_kwh=export_of(price_fn(i)))
        for i in range(head, end)
    )
    bucket, rule = select_bucket_kwh(LIMITS, floor_energy_kwh=FLOOR)
    table = build_physics_table(LIMITS, floor_energy_kwh=FLOOR, bucket_kwh=bucket)
    actionable = actionable_intervals(demands, prices)
    reachability = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    autonomy = build_reserve(limits=LIMITS, floor_energy_kwh=FLOOR, demands=demands)
    uncertainty = uncertainty_margin(
        reachability, mae_kwh_per_interval=mae, usable_capacity_kwh=LIMITS.capacity_kwh
    )
    enforced = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR + uncertainty.total_dc_kwh,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    curve = tuple(
        entry.required_dc_kwh
        if entry.required_dc_kwh is not None
        else FLOOR + uncertainty.total_dc_kwh
        for entry in enforced.intervals
    )
    horizon = build_horizon(
        demands=demands, prices=prices, required_reserve_kwh=curve, table=table
    )
    outcome = build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=stored,
        terminal_floor_kwh=FLOOR,
        floor_energy_kwh=FLOOR,
        minimum_trade_gain_eur=gain,
        allow_grid_charging=allow_charge,
        allow_battery_export=allow_export,
        grid_charge_margin_eur_per_kwh=margin,
        battery_throughput_cost_eur_per_kwh=throughput,
        edge_value_eur_per_kwh=edge_value_eur_per_kwh(
            horizon.prices[:actionable],
            discharge_efficiency=LIMITS.discharge_efficiency,
        ),
        edge_creditable_kwh=edge_creditable_energy_kwh(
            ceiling_kwh=LIMITS.energy_for_soc(100.0),
            forecast_surplus_kwh=sum(d.surplus_kwh for d in demands[:actionable]),
        ),
        autonomy=tuple(entry.required_dc_kwh for entry in autonomy.intervals),
        reachability=enforced,
        uncertainty=uncertainty,
        actionable_interval_count=actionable,
        ambient_self_consumption=True,
        forecast_risk=forecast_risk,
        bucket_rule=rule,
        head_run_state=head_run_state,
        terminal_value=terminal_value,
    )
    return Solved(outcome=outcome, table=table, head=head)


def risk_of(mae: float = 0.06353, bias: float = -0.00154, rho: float = 0.1825):
    """Return the live ForecastRisk from the 29 August payload."""
    from custom_components.alpha_ems_manager.economic import ForecastRisk

    return ForecastRisk(
        bias_kwh=bias,
        mae_kwh=mae,
        error_persistence=rho,
        adaptation_ratio=1.0218,
        today_interval_count=96,
    )


def summarise(tag: str, solved: Solved) -> str:
    """Return one line of the comparison table."""
    plan = solved.outcome.desired
    return (
        f"{tag:<34} runs={len(plan.runs):<2} "
        f"chg={plan.planned_charge_ac_kwh:6.2f} "
        f"dis={plan.planned_discharge_ac_kwh:6.2f} "
        f"imp={plan.planned_grid_import_kwh:6.2f} "
        f"exp={plan.planned_grid_export_kwh:6.2f} "
        f"cost={plan.cost_eur:8.4f} hold={plan.hold_cost_eur:7.4f} "
        f"adv={plan.hold_cost_eur - plan.cost_eur:7.4f}"
    )
