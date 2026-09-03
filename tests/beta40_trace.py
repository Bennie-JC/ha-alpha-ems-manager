"""The 2026-09-03 exported-production capture, as one replayable fixture.

**Every figure here is transcribed from the live beta.39 diagnostic**, not chosen
to make a test pass: `config_entry-alpha_ems_manager-01M05PAPSZZHFE19X10W7SNFYT`,
captured 2026-09-03T12:14:16+02:00 against integration `1.0.0-beta.39` on the
production installation.

What the capture shows, and why it is the whole of beta.40's case:

    house_load_w  792     battery_charge_w  1490     grid_import_w  0
    pv_w         3309     battery_discharge_w  0     grid_export_w  942

2.517 kW of free production, 1.490 kW of it going into the pack and 0.942 kW going
out to the meter -- with 12.61 kWh of pack headroom, an owned and *executing*
economic charge campaign, 25 of 25 safety checks passing, and **8.527 kWh of grid
authorisation still unspent** and still intended to be bought later at
0.1745 EUR/kWh.

Nothing had malfunctioned. `control.state` was `executing`, `ownership.state` was
`owned`, `command_result` was `executed`, `stop_reason` was `null`. The frozen row
was doing exactly what it said: a 0.28 kWh objective with 0.037 kWh left over a
floored 0.025 h is a 1.49 kW request, and every line of `decide_charge` after the
seed only reduces. So the row's own objective was also the ceiling on storing
production nobody had to buy.

**The shape of the plan is not the defect, and this fixture records that too.** The
schedule's three 2.50 kWh rows are exactly the three cheapest quarters in the
window -- the optimiser already concentrates its purchase at full inverter power --
and the fifteen 0.28 kWh rows are one lattice bucket each, sized to the *forecast*
surplus (0.22 kWh of production against 0.06 kWh of grid at interval 49, i.e.
0.88 kW). Reality delivered 2.517 kW. The row was right about the forecast and the
forecast was wrong, and a frozen row turns a forecast into a hard cap.

Read-only. Nothing here reaches production, and no figure is rounded differently
from the document it came from.
"""

from __future__ import annotations

from typing import Final

# == the site, from data.battery_plan.inputs and .model ====================

#: Usable pack energy, DC. ``battery_plan.inputs.capacity_kwh``.
CAPACITY_DC_KWH: Final = 21.6
#: AC charge and discharge power the inverter allows.
MAX_CHARGE_KW: Final = 10.0
MAX_DISCHARGE_KW: Final = 10.0
#: ``battery_plan.model.charge_efficiency`` / ``discharge_efficiency``. One
#: configured round-trip figure of 90 %, split symmetrically.
CHARGE_EFFICIENCY: Final = 0.948683
DISCHARGE_EFFICIENCY: Final = 0.948683
ROUND_TRIP_EFFICIENCY: Final = 0.9
#: ``battery_plan.reserve.configured_min_soc_percent`` and the floor it implies.
MIN_SOC_PERCENT: Final = 20.0
FLOOR_DC_KWH: Final = 4.32

# == the lattice, from data.economic_plan.solver ===========================

#: ``solver.bucket_kwh`` and ``solver.buckets``, with ``bucket_rule``
#: ``aligned_to_peak_power``. One bucket of *charge* is therefore
#: ``max_charge_kw * 0.25 / k`` AC with ``k = 9`` -- 0.2778 kWh at 1.111 kW, which
#: the document publishes verbatim three times as ``power_kw: 1.111``.
BUCKET_DC_KWH: Final = 0.263523
BUCKETS: Final = 82
BUCKET_DIVISOR: Final = 9
ONE_BUCKET_AC_KWH: Final = MAX_CHARGE_KW * 0.25 / BUCKET_DIVISOR
ONE_BUCKET_KW: Final = MAX_CHARGE_KW / BUCKET_DIVISOR

# == the measured instant, from data.normalized_flows_now =================

PV_KW: Final = 3.309
HOUSE_KW: Final = 0.792
BATTERY_CHARGE_KW: Final = 1.490
GRID_IMPORT_KW: Final = 0.0
GRID_EXPORT_KW: Final = 0.942
#: What was standing there free, and the bound on everything beta.40 may command.
SURPLUS_KW: Final = PV_KW - HOUSE_KW

#: ``battery_plan.state``: 41.6 % of 21.6 kWh, and the room above it.
SOC_PERCENT: Final = 41.6
STORED_DC_KWH: Final = 8.9856
HEADROOM_DC_KWH: Final = 12.6144

# == the frozen row, from execution.quarter and .admitted_plan.rows =======

#: The row covering the capture: 10:00-10:15Z, i.e. 12:00-12:15 local.
ROW_BATTERY_KWH: Final = 0.28
ROW_GRID_AUTHORISED_KWH: Final = 0.04
ROW_GRID_EXPORT_TARGET_KWH: Final = 0.0
#: ``required_average_battery_kw`` at 5.3 s elapsed: 0.28 / (894.7/3600).
ROW_OPENING_RATE_KW: Final = 1.127

#: What was left of the objective at the capture instant, and the time left.
#: ``battery_rate_kw`` is ``remaining / max(90/3600, 44/3600)``, and 1.490 kW is
#: what that gives for 0.0373 kWh -- which is how the measured pack power is
#: reconstructible from the frozen row rather than merely consistent with it.
ROW_REMAINING_AT_CAPTURE_KWH: Final = 0.0373
SECONDS_REMAINING_AT_CAPTURE: Final = 44.0

# == the run and campaign, from execution.target and .open_campaign ======

PLAN_ID: Final = "654f54aa978285be"
RUN_ID: Final = "a31693f13fbafec8"
CAMPAIGN_ID: Final = "f45d513a019342c9"
CAMPAIGN_INSTANCE_ID: Final = "89471c1ecf8abfad"

RUN_BATTERY_TARGET_KWH: Final = 13.06
RUN_EXPECTED_PV_PRODUCTION_KWH: Final = 10.66
RUN_EXPECTED_HOUSE_LOAD_KWH: Final = 6.46
RUN_EXPECTED_PV_TO_BATTERY_KWH: Final = 4.20
RUN_EXPECTED_GRID_TO_BATTERY_KWH: Final = 8.85
RUN_CHARGE_SOURCE: Final = "mixed"
#: Both ``null`` on the live run, so the headroom clamp was inert in this capture
#: -- nothing else would have stopped the absorption beta.40 adds.
RUN_MAX_END_ENERGY_KWH: Final = None
RUN_REQUIRED_HEADROOM_KWH: Final = None

#: ``execution.power``: the ceiling, what had been bought, and what remained.
RUN_GRID_CAP_KWH: Final = 8.85
RUN_GRID_CHARGED_KWH: Final = 0.323
RUN_GRID_REMAINING_KWH: Final = 8.527

CAMPAIGN_FROZEN_TARGET_KWH: Final = 13.1
CAMPAIGN_REALIZED_KWH: Final = 0.923
CAMPAIGN_QUARTERS_ADMITTED: Final = 4

# == the prices and the dual, from data.economic_value ===================

IMPORT_PRICE_EUR_KWH: Final = 0.22966
EXPORT_PRICE_EUR_KWH: Final = 0.10134
#: ``stored_energy_marginal_value_eur_kwh``, basis
#: ``downward_difference_retention``, reported not kinked.
MARGINAL_VALUE_EUR_KWH: Final = 0.2237
TERMINAL_EDGE_VALUE_EUR_KWH: Final = 0.1726
#: What the plan intended to pay for the energy it had not yet bought.
NEXT_PLANNED_CHARGE_PRICE_EUR_KWH: Final = 0.1745

# == what the tariff says about keeping one more free kWh ================

#: The gate, evaluated on the capture's own numbers:
#:
#:     0.90 * 0.2237 = 0.20133  >  0.10134     -> keep, by 0.09999 EUR/kWh
#:
#: and the lower, displaced-purchase reading of the same decision:
#:
#:     0.90 * 0.1745 - 0.10134  =  0.05571 EUR/kWh
#:
#: Both are published; neither is averaged into the other, and neither is
#: extrapolated to a day.
RETENTION_VALUE_EUR_KWH: Final = ROUND_TRIP_EFFICIENCY * MARGINAL_VALUE_EUR_KWH
RETENTION_MARGIN_EUR_KWH: Final = RETENTION_VALUE_EUR_KWH - EXPORT_PRICE_EUR_KWH
DISPLACED_PURCHASE_MARGIN_EUR_KWH: Final = (
    ROUND_TRIP_EFFICIENCY * NEXT_PLANNED_CHARGE_PRICE_EUR_KWH - EXPORT_PRICE_EUR_KWH
)

#: ``reserve.headroom.surplus_beyond_headroom_kwh`` over 42 headroom-bound
#: intervals: the document's own statement that roughly four kilowatt-hours of this
#: horizon's forecast surplus **cannot be stored at all**. Some export on this day
#: is unavoidable, and a replay that claimed otherwise would be wrong.
SURPLUS_BEYOND_HEADROOM_KWH: Final = 4.01

# == the beta.39 accounting, from data.economic_value.today_accounting ===

REALISED_TODAY_EUR: Final = 0.8897
IN_PROGRESS_INTERVAL_EUR: Final = -0.0091
REMAINING_EXPECTED_TODAY_EUR: Final = 0.1366
FORECAST_REVALUATION_EUR: Final = -0.4585
TOTAL_ECONOMIC_VALUE_TODAY_EUR: Final = 0.5587
RECONCILIATION_ERROR_EUR: Final = 0.0

# == the published schedule, verbatim ====================================

#: ``economic_plan.execution_targets[0].quarter_schedule``, as
#: ``(import_price_eur_kwh, battery_kwh, grid_authorised_kwh)`` per interval from
#: 49 to 66. The three 2.50 rows are the three cheapest prices in the window, which
#: is the evidence that the optimiser already concentrates its purchase; the fifteen
#: 0.28 rows are one bucket each against a small forecast surplus.
SCHEDULE: Final = (
    (0.19588, 0.28, 0.06),
    (0.18059, 0.28, 0.01),
    (0.17176, 0.28, 0.03),
    (0.18196, 0.28, 0.05),
    (0.17552, 0.28, 0.06),
    (0.16551, 0.28, 0.05),
    (0.15285, 2.50, 2.27),
    (0.17048, 0.28, 0.06),
    (0.15925, 2.50, 2.32),
    (0.16220, 0.28, 0.13),
    (0.16220, 0.28, 0.14),
    (0.16038, 2.50, 2.46),
    (0.16258, 0.28, 0.14),
    (0.17526, 0.28, 0.13),
    (0.17067, 0.28, 0.09),
    (0.18737, 0.28, 0.12),
    (0.18984, 0.28, 0.14),
    (0.21596, 0.28, 0.15),
)

#: What ``decide_charge`` published for itself at the 12:00:05 refresh, which is the
#: capture's *other* direction and the reason both are asserted: there the grid
#: ceiling did bind, and the beta.36 arithmetic is reproducible to the watt.
#:
#: house 0.462, pv 1.031, grid_remaining 0.04 kWh over 894.7 s:
#:     grid_rate     = 0.04 / 0.24853  = 0.161
#:     battery_cap   = 0.569 + 0.161   = 0.730
#:     required      = 0.28 / 0.24853  = 1.127
#:     applied       = min(1.127, 0.730) -> quantised 0.700
#:     desired_grid  = 0.462 - 1.031 + 0.730 = 0.161
#:     achievable    = 0.462 - 1.031 + 0.700 = 0.131
REFRESH_HOUSE_KW: Final = 0.462
REFRESH_PV_KW: Final = 1.031
REFRESH_APPLIED_KW: Final = -0.7
REFRESH_DESIRED_GRID_KW: Final = 0.161
REFRESH_ACHIEVABLE_GRID_KW: Final = 0.131
REFRESH_LIMITED_BY: Final = "remaining_grid_energy"

#: What the controller predicted it would export at the capture instant, from the
#: canonical identity with the objective rate in it:
#:
#:     desired_grid = house - pv + applied = 0.792 - 3.309 + 1.490 = -1.027
#:
#: The meter measured 0.942 kW. The 0.085 kW difference is conversion loss and
#: source skew, inside the document's own published tolerance
#: (``40 + 0.05*dc + 0.03*ac`` W, about 0.30 kW here) -- and the capture's
#: ``energy_balance.last_sample`` passes.
BETA39_PREDICTED_EXPORT_KW: Final = -1.027
BALANCE_TOLERANCE_KW: Final = 0.30
