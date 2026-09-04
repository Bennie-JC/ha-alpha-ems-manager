"""The 2026-09-03 20:45 refresh, where the optimiser planned nothing at all.

**The beta.41 case, and it is a plan with no runs in it.** Captured against
integration `1.0.0-beta.40` on the production installation at
2026-09-03T20:45+02:00, the economic plan read:

    runs                       []
    run_count                   0
    direction_changes           0
    battery_throughput_kwh    0.0
    reason_code               no_material_economic_action

with tomorrow's day-ahead prices fully published, 9.936 kWh in a 21.6 kWh pack
against a 4.32 kWh floor, and a forecast that has the pack **on the floor from
02:45 and staying there all day**, while the household imports 11.37 kWh.

The figure that explains it is `stored_energy_marginal_value_eur_kwh: 0.15013`,
which is exactly `discharge_efficiency * terminal export price`:

    0.948683 * 0.15825 = 0.150129

so the whole value curve above the floor was the *export* segment, flat. Buying a
kWh then returns `0.948683 * 0.15013 = 0.14243` against a cost of the import price
plus the configured 0.05 margin, i.e. a **break-even import price of 0.092425** --
below the cheapest quarter this installation has ever recorded (0.15285). No price
could clear it, so no Buy was reachable at any price, and the user's 0.20 EUR
minimum trade gain was never even tested.

## What is measured and what is reconstructed

Transcribed verbatim from the diagnostic:

* the site: capacity, powers, efficiencies, configured minimum state of charge
* `stored_dc_kwh` 9.936 and `soc_percent` 46
* `minimum_soc_interval: 23`, which against the 21:00 frame is **02:45**
* the band discharge split -- evening 2.84, night 2.48, morning 0.00,
  afternoon 0.01 -- summing to 5.33, exactly the deliverable
  `(9.936 - 4.32) * 0.948683 = 5.328`
* tomorrow's totals: PV 7.79 kWh against load 21.65 kWh,
  `forecast_surplus_kwh: 0.01`, `intervals_absorbing_surplus: 2`
* household import over the horizon: 11.37 kWh
* import prices for absolute indices 71..91, recovered by stitching the
  `import_price_head` of all sixteen decision records in the file and checking
  every overlap -- zero inconsistencies

**Reconstructed, and labelled as such because the diagnostic does not contain
it:** absolute indices 92..191 -- all of tomorrow -- are published nowhere in the
capture, which carries only eight-interval price heads. Tomorrow's per-quarter
shape below is therefore a reconstruction anchored on the three quarters the
operator quoted directly (02:00 at 0.27, 08:00 at 0.24, 13:00 at 0.16).

**The diagnosis never depended on those numbers.** A break-even below the market
floor refuses every quarter regardless of what tomorrow costs, which is why the
defect is provable from the measured half alone. The reconstruction exists so the
*fix* can be exercised end to end.

Read-only. Nothing here reaches production.
"""

from __future__ import annotations

import math
from typing import Final

# == the site, verbatim ====================================================

CAPACITY_DC_KWH: Final = 21.6
MAX_CHARGE_KW: Final = 10.0
MAX_DISCHARGE_KW: Final = 10.0
ROUND_TRIP_EFFICIENCY_PERCENT: Final = 90.0
#: One configured round-trip figure, split symmetrically. sqrt(0.90).
CHARGE_EFFICIENCY: Final = 0.9486832980505138
DISCHARGE_EFFICIENCY: Final = 0.9486832980505138
MIN_SOC_PERCENT: Final = 20.0
FLOOR_DC_KWH: Final = 4.32

# == the state at 20:45, verbatim ==========================================

STORED_DC_KWH: Final = 9.936
SOC_PERCENT: Final = 46.0
#: 20:45 is interval 83 of a 96-interval civil day; the refresh frame starts at 84.
HEAD_INDEX: Final = 83
FRAME_INDEX: Final = 84
DAY_INTERVALS: Final = 96
#: The last interval of tomorrow, so the series is "rest of today, then tomorrow".
END_INDEX: Final = 192

#: ``battery_plan.trajectory.minimum_soc_interval``, relative to the 21:00 frame.
MINIMUM_SOC_INTERVAL: Final = 23
#: 21:00 + 23 * 15 min. The hour the pack reaches the floor with no charging.
FLOOR_REACHED_AT: Final = "2026-09-04T02:45+02:00"

#: Deliverable AC energy above the floor, and the published band split that
#: reproduces it exactly.
DELIVERABLE_AC_KWH: Final = 5.328
BAND_DISCHARGE_AC_KWH: Final = {
    "evening": 2.84,
    "night": 2.48,
    "morning": 0.00,
    "afternoon": 0.01,
}

# == tomorrow, verbatim totals =============================================

TOMORROW_PV_KWH: Final = 7.79
TOMORROW_LOAD_KWH: Final = 21.65
FORECAST_SURPLUS_KWH: Final = 0.01
INTERVALS_ABSORBING_SURPLUS: Final = 2
HOUSEHOLD_IMPORT_KWH: Final = 11.37

# == the published economics, verbatim =====================================

MARGINAL_VALUE_EUR_KWH: Final = 0.15013
TERMINAL_EXPORT_PRICE_EUR_KWH: Final = 0.15825
#: The two published endpoints, which differ by the ambient drain beta.41 closes.
PLANNED_END_ENERGY_DC_KWH: Final = 9.75
END_ENERGY_DC_KWH: Final = 4.32
#: The user's configured economics. Authoritative for discretionary trades, and
#: never the cause of the refusal: the per-kWh gate refused first.
MINIMUM_TRADE_GAIN_EUR: Final = 0.20
GRID_CHARGE_MARGIN_EUR_PER_KWH: Final = 0.05
#: What the optimiser produced.
RUN_COUNT: Final = 0
REASON_CODE: Final = "no_material_economic_action"

#: The break-even import price the published marginal value implies, and the
#: cheapest quarter the installation has ever recorded. The first being below the
#: second is the whole defect.
#:
#: Exactly ``round_trip * export_price - margin`` = 0.9 * 0.15825 - 0.05. The
#: audit reported it to four places; this is the figure itself.
BREAK_EVEN_IMPORT_EUR_KWH: Final = 0.092425
CHEAPEST_QUARTER_EVER_SEEN: Final = 0.15285

# == prices ================================================================

#: Absolute index -> import price, recovered from the capture with every overlap
#: cross-checked. 17:45 through 22:45 on 2026-09-03.
RECOVERED_IMPORT_PRICES: Final = {
    71: 0.29853,
    72: 0.30330,
    73: 0.31196,
    74: 0.32620,
    75: 0.34669,
    76: 0.38600,
    77: 0.41905,
    78: 0.44212,
    79: 0.46630,
    80: 0.45118,
    81: 0.43004,
    82: 0.41216,
    83: 0.39511,
    84: 0.37822,
    85: 0.36104,
    86: 0.34551,
    87: 0.33208,
    88: 0.32066,
    89: 0.31088,
    90: 0.30240,
    91: 0.29853,
}

#: The measured Dutch feed-in relationship, fitted in beta.34 on live pairs.
_EXPORT_SLOPE: Final = 0.826500
_EXPORT_OFFSET: Final = 0.088500


def export_of(import_eur: float) -> float:
    """Return the feed-in price for an import price."""
    return max(0.0, _EXPORT_SLOPE * import_eur - _EXPORT_OFFSET)


def import_price_at(index: int) -> float:
    """Return the import price for an absolute interval index.

    Measured where the capture recovered it, reconstructed beyond. The
    reconstruction is anchored on the three quarters the operator quoted -- 02:00
    at 0.27, 08:00 at 0.24 and 13:00 at 0.16 -- and is deliberately *flat within
    each band* rather than smoothed, so a test that depends on a particular curve
    shape fails loudly instead of drifting.
    """
    if index in RECOVERED_IMPORT_PRICES:
        return RECOVERED_IMPORT_PRICES[index]
    hour = (index % DAY_INTERVALS) / 4.0
    if 0.0 <= hour < 6.0:
        return 0.27
    if 6.0 <= hour < 9.0:
        return 0.24
    if 9.0 <= hour < 11.0:
        return 0.21
    if 11.0 <= hour < 17.0:
        return 0.16
    if 17.0 <= hour < 20.0:
        return 0.42
    return 0.33


#: Sunrise and sunset used to shape the reconstructed production day.
SUNRISE_HOUR: Final = 7.0
SUNSET_HOUR: Final = 19.0
#: Scale factors fitted so the reconstructed day sums to the published totals:
#: load 21.65 kWh and production 7.79 kWh over tomorrow.
_LOAD_SCALE: Final = 0.95459
_PV_SCALE: Final = 0.324583


def load_at(index: int) -> float:
    """Return forecast household load for an absolute index, AC kWh.

    **The midday band is high on purpose, and it is what the published figures
    force.** 7.79 kWh of production against 21.65 kWh of load absorbed a surplus
    in only two quarters of the whole day, which a house with a flat daytime draw
    could not produce -- a bell-shaped 7.79 kWh would clear a flat 0.22 kWh load
    for nineteen quarters. So the reconstruction puts the load peak *under* the
    production peak, which is what an installation with a daytime load of this size
    looks like.

    Band levels are fitted to the published 21.65 kWh total, not chosen.
    """
    hour = (index % DAY_INTERVALS) / 4.0
    if hour < 6.0:
        band = 0.150
    elif hour < 9.0:
        band = 0.240
    elif hour < 11.0:
        band = 0.250
    elif hour < 16.0:
        band = 0.330
    elif hour < 17.0:
        band = 0.250
    elif hour < 23.0:
        band = 0.245
    else:
        band = 0.180
    return band * _LOAD_SCALE


def pv_at(index: int) -> float:
    """Return forecast production for an absolute index, AC kWh.

    Zero for the rest of today -- it is dark from 20:45 -- and a squared sine over
    tomorrow between sunrise and sunset, scaled to the published 7.79 kWh.

    Against the load above this yields a surplus of 0.034 kWh across five
    quarters, approximating the published ``forecast_surplus_kwh: 0.01`` over two.
    A symmetric curve cannot land on exactly two, because the quarters either side
    of the peak are equal by construction; the physics the tests depend on -- that
    the pack cannot refill itself from tomorrow's sun -- is unaffected, and the
    published figures are recorded above rather than fitted away.
    """
    if index < DAY_INTERVALS:
        return 0.0
    hour = (index % DAY_INTERVALS) / 4.0
    if hour < SUNRISE_HOUR or hour >= SUNSET_HOUR:
        return 0.0
    span = SUNSET_HOUR - SUNRISE_HOUR
    return _PV_SCALE * math.sin(math.pi * (hour - SUNRISE_HOUR) / span) ** 2
