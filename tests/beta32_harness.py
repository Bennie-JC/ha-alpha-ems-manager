"""A solved horizon on the live installation, for the beta.32 suites.

Deliberately thin. It builds the reference pack, the reserve inputs and the
horizon exactly as ``coordinator._async_economic_outcome`` does, so a beta.32 test
exercises the production path rather than a convenient approximation -- and it
returns the ``table`` alongside the outcome, because several beta.32 assertions
are about the lattice itself.

The anti-tautology rule applies: nothing here computes an expected answer. Every
expectation lives in the test that states it, hand-computed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from custom_components.alpha_ems_manager.battery import build_limits
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    PhysicsTable,
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

#: The Hillview installation, which is what every measured figure in the beta.32
#: notes was taken from.
LIMITS, _MISSING = build_limits(
    capacity_kwh=21.6,
    max_charge_kw=10.0,
    max_discharge_kw=10.0,
    round_trip_efficiency_percent=90.0,
    max_soc_percent=100.0,
)
assert _MISSING is None
FLOOR: float = LIMITS.energy_for_soc(20.0)


@dataclass(frozen=True, slots=True)
class Solved:
    """One solved horizon, with the pieces a beta.32 assertion needs."""

    outcome: Any
    table: PhysicsTable

    @property
    def desired(self) -> Any:
        """Return the published plan."""
        return self.outcome.desired

    def __getattr__(self, name: str) -> Any:
        """Delegate to the outcome, so a test can read either."""
        return getattr(self.outcome, name)


def flat(load_kwh: float) -> Callable[[int], float]:
    """Return a constant per-quarter house load."""
    return lambda index: load_kwh


def no_pv(index: int) -> float:
    """Return no production at all."""
    return 0.0


def solve_shape(
    *,
    load_fn: Callable[[int], float],
    price_fn: Callable[[int], float],
    n: int,
    stored: float,
    pv_fn: Callable[[int], float] = no_pv,
    allow_export: bool = True,
    allow_charge: bool = True,
    gain: float = 0.20,
    mae: float | None = 0.06,
    ambient_self_consumption: bool = False,
    forecast_risk: Any = None,
    export_spread: float = 0.13,
) -> Solved:
    """Solve one horizon through the production path.

    ``export_spread`` is the gap between the import and export price, which on the
    live tariff is roughly 0.13 EUR/kWh; the two are never netted, per the
    post-saldering rule.
    """
    demands = tuple(
        IntervalDemand(index=i, baseline_kwh=load_fn(i), pv_kwh=pv_fn(i))
        for i in range(n)
    )
    prices = tuple(
        IntervalPrice(
            import_eur_kwh=price_fn(i), export_eur_kwh=price_fn(i) - export_spread
        )
        for i in range(n)
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
    margin = uncertainty_margin(
        reachability,
        mae_kwh_per_interval=mae,
        usable_capacity_kwh=LIMITS.capacity_kwh,
    )
    enforced = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR + margin.total_dc_kwh,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    curve = tuple(
        entry.required_dc_kwh
        if entry.required_dc_kwh is not None
        else FLOOR + margin.total_dc_kwh
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
        grid_charge_margin_eur_per_kwh=0.05,
        battery_throughput_cost_eur_per_kwh=0.0,
        edge_value_eur_per_kwh=edge_value_eur_per_kwh(
            horizon.prices[:actionable],
            discharge_efficiency=LIMITS.discharge_efficiency,
        ),
        edge_creditable_kwh=edge_creditable_energy_kwh(
            ceiling_kwh=LIMITS.energy_for_soc(100.0), forecast_surplus_kwh=0.0
        ),
        autonomy=tuple(entry.required_dc_kwh for entry in autonomy.intervals),
        reachability=enforced,
        uncertainty=margin,
        actionable_interval_count=actionable,
        ambient_self_consumption=ambient_self_consumption,
        forecast_risk=forecast_risk,
        bucket_rule=rule,
    )
    return Solved(outcome=outcome, table=table)


# ---------------------------------------------------------------------------
# The live 17:45 shape, which every campaign figure in the notes was measured on
# ---------------------------------------------------------------------------


def live_price(index: int) -> float:
    """Return a Dutch-shaped dynamic tariff, hour-granular with real jitter."""
    hour = ((index + 71) % 96) / 4.0
    if 17 <= hour < 21:
        base = 0.30 + 0.06 * (1 - abs(hour - 19) / 2)
    elif 21 <= hour < 24:
        base = 0.26
    elif hour < 6:
        base = 0.20
    elif hour < 9:
        base = 0.28
    elif hour < 11:
        base = 0.16
    elif hour < 15:
        base = 0.09 + 0.02 * abs(hour - 13) / 2
    else:
        base = 0.22
    jitter = _JITTER
    return max(0.01, base + jitter[int((index + 71) / 4) % 200])


def _draw_jitter() -> list[float]:
    """Return the 200 jitter values, in the order one seeded stream produces them."""
    stream = random.Random(7)
    return [stream.uniform(-0.035, 0.035) for _ in range(200)]


#: Per-quarter jitter, drawn once from a **local** generator.
#:
#: These two shapes used to call ``random.seed()``, which reseeds the *global*
#: generator as a side effect -- so every test running after one of them in the same
#: worker inherited a deterministic-but-arbitrary global state, and execution order
#: became observable. A sharded suite must not have that.
#:
#: **The sequence is unchanged.** One generator seeded at 7, drawn 200 times in
#: order, is exactly what ``random.seed(7)`` followed by 200 ``random.uniform``
#: calls produced -- so every recorded figure in the beta.32 family is byte-identical
#: to before. Two hundred separately-seeded generators would *not* have been.
_JITTER: dict[int, float] = dict(enumerate(_draw_jitter()))


def live_load(index: int) -> float:
    """Return a peaky household shape with quarter-level variation.

    What a learned per-quarter model actually produces: a morning shoulder, a
    strong evening peak, and real variance between adjacent quarters. The flat
    average that a simpler fixture would use is precisely what hid the label
    alternation.
    """
    noise = random.Random(1000 + index)
    hour = ((index + 71) % 96) / 4.0
    shape = (
        0.10
        + 0.28 * math.exp(-((hour - 7.5) ** 2) / 2.0)
        + 0.42 * math.exp(-((hour - 19.0) ** 2) / 4.0)
    )
    return max(0.02, shape * noise.uniform(0.55, 1.55))


def live_pv(index: int) -> float:
    """Return a clear-day production curve."""
    hour = ((index + 71) % 96) / 4.0
    if 8 <= hour <= 18:
        return 0.55 * max(0.0, math.sin(math.pi * (hour - 8) / 10)) ** 1.5
    return 0.0


def live_shape(**overrides: Any) -> Solved:
    """Solve the 121-interval today+tomorrow horizon from the 17:45 diagnostic."""
    arguments: dict[str, Any] = {
        "load_fn": live_load,
        "pv_fn": live_pv,
        "price_fn": live_price,
        "n": 121,
        "stored": 14.77,
    }
    arguments.update(overrides)
    return solve_shape(**arguments)
